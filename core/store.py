"""Lưu hội thoại vào SQLite để chat tiếp được sau khi khởi động lại."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    model             TEXT NOT NULL,
    remote_id         TEXT,            -- conversation_id phía ChatGPT
    parent_message_id TEXT,            -- message cuối của assistant
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
-- Proxy: ánh xạ hash lịch sử tin nhắn → thread ChatGPT để không gửi lại cả lịch sử
CREATE TABLE IF NOT EXISTS proxy_threads (
    history_hash      TEXT PRIMARY KEY,
    remote_id         TEXT NOT NULL,
    parent_message_id TEXT NOT NULL,
    created_at        REAL NOT NULL
);
"""


@dataclass
class Conversation:
    id: str
    title: str
    model: str
    remote_id: str | None
    parent_message_id: str | None
    created_at: float
    updated_at: float


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Hội thoại ───────────────────────────────────────────────

    def create_conversation(self, title: str, model: str) -> Conversation:
        now = time.time()
        conv = Conversation(str(uuid.uuid4()), title[:80] or "Hội thoại mới", model, None, None, now, now)
        with self._conn() as c:
            c.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                (conv.id, conv.title, conv.model, None, None, now, now),
            )
        return conv

    def get_conversation(self, conv_id: str) -> Conversation | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return Conversation(**dict(row)) if row else None

    def list_conversations(self, limit: int = 100) -> list[Conversation]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def update_thread(self, conv_id: str, *, remote_id: str | None, parent_message_id: str | None,
                      model: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE conversations SET remote_id = COALESCE(?, remote_id), "
                "parent_message_id = COALESCE(?, parent_message_id), "
                "model = COALESCE(?, model), updated_at = ? WHERE id = ?",
                (remote_id, parent_message_id, model, time.time(), conv_id),
            )

    def delete_conversation(self, conv_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))

    def add_message(self, conv_id: str, role: str, content: str) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                (conv_id, role, content, now),
            )
            c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))

    def get_messages(self, conv_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Proxy thread cache ──────────────────────────────────────

    def get_proxy_thread(self, history_hash: str) -> tuple[str, str] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT remote_id, parent_message_id FROM proxy_threads WHERE history_hash = ?",
                (history_hash,),
            ).fetchone()
        return (row["remote_id"], row["parent_message_id"]) if row else None

    def save_proxy_thread(self, history_hash: str, remote_id: str, parent_message_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO proxy_threads VALUES (?,?,?,?)",
                (history_hash, remote_id, parent_message_id, time.time()),
            )

    def delete_proxy_thread(self, history_hash: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM proxy_threads WHERE history_hash = ?", (history_hash,))
