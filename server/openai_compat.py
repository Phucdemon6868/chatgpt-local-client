"""
Server tương thích OpenAI API (/v1/chat/completions, /v1/models).

API OpenAI là stateless (client gửi lại toàn bộ lịch sử mỗi lượt), còn ChatGPT
web là stateful (conversation_id + parent_message_id). Cầu nối:
  - Hash lịch sử trước tin nhắn cuối → nếu đã thấy, nối tiếp thread cũ và chỉ
    gửi tin nhắn mới.
  - Nếu chưa thấy (hoặc thread cũ hỏng), gộp cả lịch sử thành một prompt và mở
    thread mới.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.client import ChatGPTClient, ChatGPTError, StreamEvent
from core.config import Settings, load_settings
from core.store import Store

log = logging.getLogger(__name__)


# ─────────────────────────── Schema ───────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Any = None

    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False

    model_config = {"extra": "allow"}   # temperature, tools, ... được bỏ qua


# ─────────────────────────── Chuyển đổi tin nhắn ─────────────────

def content_to_text(content: Any) -> str:
    """content của OpenAI có thể là str hoặc list các part {"type": "text", ...}."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text"):
                    texts.append(part.get("text", ""))
                elif part.get("type") in ("image_url", "input_image"):
                    texts.append("[ảnh bị bỏ qua]")
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    return str(content)


def normalize(messages: list[ChatMessage]) -> list[dict[str, str]]:
    out = []
    for m in messages:
        role = "system" if m.role == "developer" else m.role
        out.append({"role": role, "content": content_to_text(m.content).strip()})
    return out


def history_hash(messages: list[dict[str, str]]) -> str:
    data = json.dumps([[m["role"], m["content"]] for m in messages], ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


_ROLE_LABELS = {"system": "System instructions", "user": "User", "assistant": "Assistant", "tool": "Tool result"}


def flatten(messages: list[dict[str, str]]) -> str:
    """Gộp lịch sử thành một prompt duy nhất."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return messages[0]["content"]
    blocks = [
        "Below is a conversation transcript. Follow the system instructions (if any) "
        "and reply as the assistant to the last User message. Reply with the answer only."
    ]
    for m in messages:
        label = _ROLE_LABELS.get(m["role"], m["role"].capitalize())
        blocks.append(f"[{label}]\n{m['content']}")
    return "\n\n".join(blocks)


# ─────────────────────────── App ──────────────────────────────────

def create_app(settings: Settings | None = None, client: ChatGPTClient | None = None,
               store: Store | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    _client: list[ChatGPTClient | None] = [client]

    def get_client() -> ChatGPTClient:
        if _client[0] is None:
            _client[0] = ChatGPTClient(settings)
        return _client[0]

    app = FastAPI(title="ChatGPT Local Proxy", version="0.1.0")

    @app.exception_handler(HTTPException)
    async def openai_error(_, exc: HTTPException):
        # Client OpenAI mong {"error": {...}} ở cấp cao nhất, không phải {"detail": ...}
        body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail \
            else {"error": {"message": str(exc.detail), "type": "invalid_request_error"}}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    def check_key(authorization: str | None = Header(default=None)) -> None:
        if not settings.proxy_api_key:
            return
        if authorization != f"Bearer {settings.proxy_api_key}":
            raise HTTPException(401, detail={"error": {"message": "Sai API key", "type": "invalid_request_error"}})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/models", dependencies=[Depends(check_key)])
    def list_models():
        now = int(time.time())
        return {
            "object": "list",
            "data": [{"id": m, "object": "model", "created": now, "owned_by": "chatgpt-web"}
                     for m in get_client().list_models()],
        }

    def start(messages: list[dict[str, str]], model: str) -> tuple[StreamEvent, Iterator[StreamEvent]]:
        """Mở stream, trả về (event đầu tiên, phần còn lại). Lỗi được ném ra trước khi gửi header."""
        cli = get_client()
        prefix = messages[:-1]
        cached = store.get_proxy_thread(history_hash(prefix)) if prefix else None

        if cached:
            remote_id, parent_id = cached
            gen = cli.stream(messages[-1]["content"], model=model, conversation_id=remote_id,
                             parent_message_id=parent_id, temporary=settings.temporary_chat)
            try:
                return next(gen), gen
            except ChatGPTError as e:
                if e.status is None or e.status >= 500:
                    raise
                log.info("Không nối tiếp được thread %s (%s), gửi lại toàn bộ lịch sử.", remote_id, e)
                store.delete_proxy_thread(history_hash(prefix))

        gen = cli.stream(flatten(messages), model=model, temporary=settings.temporary_chat)
        return next(gen), gen

    def run(messages: list[dict[str, str]], model: str) -> Iterator[StreamEvent]:
        try:
            first, rest = start(messages, model)
        except ChatGPTError as e:
            status = e.status if e.status and 400 <= e.status < 600 else 502
            raise HTTPException(status, detail={"error": {"message": str(e), "type": "upstream_error"}})

        def events() -> Iterator[StreamEvent]:
            yield first
            yield from rest

        def remember() -> Iterator[StreamEvent]:
            for ev in events():
                if ev.type == "done" and ev.conversation_id and ev.message_id:
                    full = messages + [{"role": "assistant", "content": ev.text.strip()}]
                    store.save_proxy_thread(history_hash(full), ev.conversation_id, ev.message_id)
                yield ev

        return remember()

    @app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
    def chat_completions(req: ChatCompletionRequest):
        messages = normalize(req.messages)
        if not messages or messages[-1]["role"] not in ("user", "tool"):
            raise HTTPException(400, detail={"error": {"message": "Tin nhắn cuối phải là của user."}})
        model = req.model or settings.default_model
        events = run(messages, model)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if not req.stream:
            final = next(ev for ev in events if ev.type == "done")
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": final.text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        def chunk(delta: dict, finish: str | None = None) -> str:
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        def sse() -> Iterator[str]:
            yield chunk({"role": "assistant", "content": ""})
            try:
                for ev in events:
                    if ev.type == "delta" and ev.text:
                        yield chunk({"content": ev.text})
            except ChatGPTError as e:
                err = {"error": {"message": str(e), "type": "upstream_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
