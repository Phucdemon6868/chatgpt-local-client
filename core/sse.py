"""
Parser SSE của /backend-api/conversation.

Hỗ trợ cả 2 định dạng server có thể trả về:
  - Cũ: mỗi dòng data là toàn bộ {"message": {...}, "conversation_id": ...}
  - Delta v1: {"p": "", "o": "add", "v": {...}} rồi các op append/replace/patch
    theo JSON pointer, hoặc {"v": "..."} (lặp lại path/op trước đó).

Cả hai đều được dựng lại thành một trạng thái "root" rồi so với lượng text đã
phát ra, nên chỉ phần mới của câu trả lời được trả về.
"""

from __future__ import annotations

import json
from typing import Any

_TEXT_CONTENT_TYPES = ("text", "multimodal_text")


class SSEError(RuntimeError):
    pass


def _resolve(container: Any, token: str):
    if isinstance(container, list):
        return int(token)
    return token


def _apply_op(root: Any, path: str, op: str, value: Any) -> Any:
    """Áp dụng một op lên root, trả về root mới (root có thể bị thay thế)."""
    if op == "patch":
        for sub in value or []:
            root = _apply_op(root, sub.get("p", ""), sub.get("o", "replace"), sub.get("v"))
        return root

    tokens = [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]] if path else []
    if not tokens:
        if op == "append" and isinstance(root, str) and isinstance(value, str):
            return root + value
        if op == "append" and isinstance(root, list):
            return root + list(value)
        return value  # add / replace toàn bộ root

    parent = root
    for tok in tokens[:-1]:
        key = _resolve(parent, tok)
        if isinstance(parent, dict) and key not in parent:
            parent[key] = {}
        parent = parent[key]
    key = _resolve(parent, tokens[-1])

    if op == "append":
        if isinstance(parent, list) and key == len(parent):
            parent.append(value)
            return root
        cur = parent.get(key) if isinstance(parent, dict) else parent[key]
        if cur is None:
            new = value
        elif isinstance(cur, str):
            new = cur + (value if isinstance(value, str) else str(value))
        elif isinstance(cur, list):
            new = cur + (value if isinstance(value, list) else [value])
        else:
            new = value
        parent[key] = new
    elif op == "truncate":
        cur = parent[key]
        parent[key] = cur[: int(value)]
    elif op == "remove":
        del parent[key]
    else:  # add / replace
        if isinstance(parent, list) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
    return root


def _message_text(msg: dict) -> str | None:
    """Text của message nếu đó là câu trả lời hiển thị của assistant, ngược lại None."""
    if (msg.get("author") or {}).get("role") != "assistant":
        return None
    if msg.get("recipient") not in (None, "all"):
        return None  # tool call
    content = msg.get("content") or {}
    if content.get("content_type") not in _TEXT_CONTENT_TYPES:
        return None  # thoughts, code, ...
    parts = content.get("parts") or []
    return "".join(p for p in parts if isinstance(p, str))


class SSEParser:
    def __init__(self) -> None:
        self.root: Any = None
        self.conversation_id: str | None = None
        self.message_id: str | None = None
        self.model: str | None = None
        self.done = False
        self._last_p: str | None = None
        self._last_o: str | None = None
        self._emitted: dict[str, int] = {}
        self._segments: list[str] = []   # mỗi message assistant là một đoạn
        self._seg_index: dict[str, int] = {}

    @property
    def text(self) -> str:
        return "\n\n".join(s for s in self._segments if s)

    def feed_line(self, line: str) -> list[str]:
        """Nhận một dòng SSE thô, trả về các mẩu text mới."""
        if not line or not line.startswith("data:"):
            return []
        return self.feed_data(line[5:].strip())

    def feed_data(self, data_str: str) -> list[str]:
        if data_str == "[DONE]":
            self.done = True
            return []
        try:
            obj = json.loads(data_str)
        except json.JSONDecodeError:
            return []
        if not isinstance(obj, dict):
            return []  # ví dụ "v1" sau event: delta_encoding

        if obj.get("error"):
            raise SSEError(str(obj["error"]))
        if obj.get("type") == "error":
            raise SSEError(str(obj.get("message") or obj))
        if obj.get("conversation_id"):
            self.conversation_id = obj["conversation_id"]

        if "message" in obj and "o" not in obj and "v" not in obj:
            self.root = obj                     # định dạng cũ
        elif "v" in obj or "o" in obj:
            if "p" in obj:
                path, op = obj["p"], obj.get("o", "replace")
            else:
                path, op = self._last_p or "", obj.get("o", self._last_o or "append")
            self.root = _apply_op(self.root, path, op, obj.get("v"))
            if op == "patch" and obj.get("v"):
                last = obj["v"][-1]
                self._last_p, self._last_o = last.get("p", ""), last.get("o", "replace")
            else:
                self._last_p, self._last_o = path, op
        else:
            return []   # metadata: title_generation, message_stream_complete, ...

        return self._collect()

    def _collect(self) -> list[str]:
        if not isinstance(self.root, dict):
            return []
        if self.root.get("conversation_id"):
            self.conversation_id = self.root["conversation_id"]
        msg = self.root.get("message")
        if not isinstance(msg, dict):
            return []
        text = _message_text(msg)
        if text is None:
            return []
        msg_id = msg.get("id") or "_"
        self.message_id = msg_id
        slug = (msg.get("metadata") or {}).get("model_slug")
        if slug:
            self.model = slug

        if msg_id not in self._seg_index:
            self._seg_index[msg_id] = len(self._segments)
            self._segments.append("")
        idx = self._seg_index[msg_id]

        out: list[str] = []
        prev = self._emitted.get(msg_id, 0)
        if len(text) > prev:
            if prev == 0 and any(s for i, s in enumerate(self._segments) if i != idx):
                out.append("\n\n")
            out.append(text[prev:])
            self._emitted[msg_id] = len(text)
        self._segments[idx] = text
        return out
