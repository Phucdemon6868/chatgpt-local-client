"""Đổi cookie → accessToken, cache và tự làm mới khi hết hạn."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import requests

SESSION_URL = "https://chatgpt.com/api/auth/session"
# Làm mới sớm hơn hạn thật một chút để tránh token chết giữa request
_REFRESH_MARGIN = 300
# Nếu server không trả "expires", giữ token tối đa ngần này giây
_DEFAULT_TTL = 3600


class AuthError(RuntimeError):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _parse_expires(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class TokenManager:
    """Giữ accessToken; gọi get() để lấy token còn hạn, invalidate() khi gặp 401."""

    def __init__(self, cookie: str, user_agent: str, timeout: int = 15):
        if not cookie:
            raise AuthError("Chưa cấu hình cookie (CHATGPT_COOKIE).")
        self.cookie = cookie
        self.user_agent = user_agent
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def fetch(self) -> tuple[str, float]:
        headers = {
            "accept": "*/*",
            "user-agent": self.user_agent,
            "cookie": self.cookie,
            "referer": "https://chatgpt.com/",
        }
        try:
            resp = requests.get(SESSION_URL, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise AuthError(f"Lỗi mạng khi xác thực: {e}", 502) from e
        if resp.status_code != 200:
            raise AuthError(f"/api/auth/session HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        try:
            data = resp.json()
        except ValueError as e:
            raise AuthError("Phản hồi auth không phải JSON (có thể bị Cloudflare chặn).", 502) from e
        token = data.get("accessToken")
        if not token:
            raise AuthError("Không tìm thấy accessToken. Cookie có thể đã hết hạn.")
        expires = _parse_expires(data.get("expires")) or (time.time() + _DEFAULT_TTL)
        # "expires" là hạn của phiên; accessToken thường sống ngắn hơn → giới hạn TTL
        expires = min(expires, time.time() + _DEFAULT_TTL)
        return token, expires

    def get(self) -> str:
        with self._lock:
            if not self._token or time.time() >= self._expires_at - _REFRESH_MARGIN:
                self._token, self._expires_at = self.fetch()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
