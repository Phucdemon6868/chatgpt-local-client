"""Client ChatGPT dùng cookie: tự refresh token, retry/backoff, streaming."""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import requests

from .auth import AuthError, TokenManager
from .config import Settings
from .sentinel import SentinelError, fetch_sentinel_tokens
from .sse import SSEError, SSEParser

log = logging.getLogger(__name__)

CONVERSATION_URL = "https://chatgpt.com/backend-api/conversation"
MODELS_URL = "https://chatgpt.com/backend-api/models"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class ChatGPTError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class StreamEvent:
    type: str                         # "delta" | "done"
    text: str = ""                    # delta: phần mới; done: toàn bộ câu trả lời
    conversation_id: str | None = None
    message_id: str | None = None     # dùng làm parent_message_id cho lượt sau
    model: str | None = None


def _timezone_offset_min() -> int:
    """Giống Date.getTimezoneOffset() của JS: UTC+7 → -420."""
    offset = datetime.now().astimezone().utcoffset()
    return -int(offset.total_seconds() // 60) if offset else 0


class ChatGPTClient:
    def __init__(self, settings: Settings, token_manager: TokenManager | None = None):
        self.settings = settings
        self.tokens = token_manager or TokenManager(settings.cookie, settings.user_agent)
        self.device_id = str(uuid.uuid4())
        self._local = threading.local()   # requests.Session không thread-safe

    # ── HTTP ────────────────────────────────────────────────────

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
                "user-agent": self.settings.user_agent,
                "oai-device-id": self.device_id,
                "oai-language": "en-US",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            })
            self._local.session = s
        return s

    def _authorize(self) -> requests.Session:
        s = self.session
        s.headers["authorization"] = f"Bearer {self.tokens.get()}"
        return s

    @staticmethod
    def _backoff(attempt: int, resp: requests.Response | None = None) -> float:
        if resp is not None:
            ra = resp.headers.get("retry-after")
            if ra and ra.isdigit():
                return min(float(ra), 60.0)
        return min(2 ** attempt, 30) + random.random()

    def _post_conversation(self, payload: dict) -> requests.Response:
        """POST có retry. Chỉ retry trước khi stream bắt đầu."""
        max_retries = self.settings.max_retries
        refreshed = False
        last_err = "không rõ"
        for attempt in range(max_retries + 1):
            try:
                session = self._authorize()
                sentinel = fetch_sentinel_tokens(session, self.settings.user_agent)
                resp = session.post(
                    CONVERSATION_URL, json=payload, headers=sentinel.headers(),
                    stream=True, timeout=self.settings.request_timeout,
                )
            except AuthError as e:
                raise ChatGPTError(str(e), e.status) from e
            except SentinelError as e:
                if e.status == 401 and not refreshed:
                    self.tokens.invalidate()
                    refreshed = True
                    continue
                last_err = str(e)
                if e.status is not None and e.status not in _RETRY_STATUSES:
                    raise ChatGPTError(last_err, e.status) from e
                time.sleep(self._backoff(attempt))
                continue
            except requests.RequestException as e:
                last_err = f"Lỗi mạng: {e}"
                time.sleep(self._backoff(attempt))
                continue

            if resp.status_code == 200:
                return resp

            body = resp.text[:300]
            resp.close()
            if resp.status_code == 401 and not refreshed:
                log.info("Token hết hạn, đang lấy token mới...")
                self.tokens.invalidate()
                refreshed = True
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
                wait = self._backoff(attempt, resp)
                log.warning("HTTP %s, thử lại sau %.1fs", resp.status_code, wait)
                time.sleep(wait)
                last_err = f"HTTP {resp.status_code}: {body}"
                continue
            raise ChatGPTError(f"HTTP {resp.status_code}: {body}", resp.status_code)

        raise ChatGPTError(f"Thất bại sau {max_retries + 1} lần thử. Lỗi cuối: {last_err}")

    # ── API ─────────────────────────────────────────────────────

    def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        conversation_id: str | None = None,
        parent_message_id: str | None = None,
        temporary: bool = False,
    ) -> Iterator[StreamEvent]:
        """Gửi prompt, yield StreamEvent("delta") cho từng mẩu text và một StreamEvent("done") ở cuối."""
        payload = {
            "action": "next",
            "model": model or self.settings.default_model,
            "timezone_offset_min": _timezone_offset_min(),
            "suggestions": [],
            "history_and_training_disabled": temporary,
            "conversation_mode": {"kind": "primary_assistant"},
            "parent_message_id": parent_message_id or str(uuid.uuid4()),
            "messages": [{
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
                "metadata": {},
            }],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        resp = self._post_conversation(payload)
        parser = SSEParser()
        try:
            for line in resp.iter_lines(decode_unicode=True):
                for piece in parser.feed_line(line):
                    yield StreamEvent("delta", piece)
                if parser.done:
                    break
        except SSEError as e:
            raise ChatGPTError(f"Server báo lỗi: {e}") from e
        except requests.RequestException as e:
            raise ChatGPTError(f"Mất kết nối khi đang stream: {e}") from e
        finally:
            resp.close()

        yield StreamEvent(
            "done", parser.text,
            conversation_id=parser.conversation_id or conversation_id,
            message_id=parser.message_id,
            model=parser.model or payload["model"],
        )

    def ask(self, prompt: str, **kwargs) -> StreamEvent:
        """Phiên bản không stream: trả về StreamEvent("done")."""
        final = None
        for ev in self.stream(prompt, **kwargs):
            if ev.type == "done":
                final = ev
        assert final is not None
        return final

    def list_models(self) -> list[str]:
        """Lấy danh sách model từ tài khoản; lỗi thì dùng danh sách trong cấu hình."""
        try:
            resp = self._authorize().get(MODELS_URL, timeout=15)
            if resp.status_code == 200:
                slugs = [m["slug"] for m in resp.json().get("models", []) if m.get("slug")]
                if slugs:
                    return slugs
        except (requests.RequestException, AuthError, ValueError, KeyError) as e:
            log.debug("Không lấy được danh sách model: %s", e)
        return list(self.settings.models)
