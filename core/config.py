"""Đọc cấu hình từ biến môi trường / file .env (không cần python-dotenv)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_MODELS = ["auto", "gpt-4o", "gpt-4o-mini", "o3-mini", "o4-mini"]


def load_dotenv(path: str | Path = ".env") -> None:
    """Nạp KEY=VALUE từ file .env vào os.environ (không ghi đè biến đã có)."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    cookie: str = ""
    default_model: str = "auto"
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    user_agent: str = DEFAULT_USER_AGENT
    proxy_api_key: str = ""          # khóa bảo vệ server proxy (để trống = không kiểm tra)
    db_path: str = "data/chats.db"
    temporary_chat: bool = True      # proxy dùng chat tạm, không làm rác lịch sử ChatGPT
    max_retries: int = 3
    request_timeout: int = 180


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)
    env = os.environ
    models = [m.strip() for m in env.get("CHATGPT_MODELS", "").split(",") if m.strip()]
    return Settings(
        cookie=env.get("CHATGPT_COOKIE", "").strip(),
        default_model=env.get("CHATGPT_DEFAULT_MODEL", "auto").strip() or "auto",
        models=models or list(DEFAULT_MODELS),
        user_agent=env.get("CHATGPT_USER_AGENT", DEFAULT_USER_AGENT),
        proxy_api_key=env.get("PROXY_API_KEY", "").strip(),
        db_path=env.get("CHATGPT_DB_PATH", "data/chats.db"),
        temporary_chat=env.get("CHATGPT_TEMPORARY_CHAT", "1").lower() not in ("0", "false", "no"),
        max_retries=int(env.get("CHATGPT_MAX_RETRIES", "3")),
        request_timeout=int(env.get("CHATGPT_TIMEOUT", "180")),
    )
