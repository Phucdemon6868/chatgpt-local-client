import json

import pytest

import core.client as client_mod
from core.client import ChatGPTClient, ChatGPTError
from core.config import Settings
from core.sentinel import SentinelTokens


class FakeTokens:
    def __init__(self):
        self.fetches = 0
        self.token = None

    def get(self):
        if self.token is None:
            self.fetches += 1
            self.token = f"tok{self.fetches}"
        return self.token

    def invalidate(self):
        self.token = None


class FakeResp:
    def __init__(self, status, lines=(), headers=None):
        self.status_code = status
        self._lines = lines
        self.headers = headers or {}
        self.text = "body"

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        pass


def make_client(responses, monkeypatch):
    monkeypatch.setattr(client_mod, "fetch_sentinel_tokens", lambda s, ua: SentinelTokens("chat"))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    c = ChatGPTClient(Settings(cookie="x", max_retries=2), token_manager=FakeTokens())
    calls = []

    def post(url, json=None, headers=None, **kw):
        calls.append({"payload": json, "auth": c.session.headers["authorization"], "headers": headers})
        return responses.pop(0)

    monkeypatch.setattr(c.session, "post", post)
    return c, calls


def ok(text="Hi"):
    msg = {"conversation_id": "c9", "message": {
        "id": "m9", "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [text]}, "metadata": {}}}
    return FakeResp(200, [f"data: {json.dumps(msg)}", "data: [DONE]"])


def test_stream_success(monkeypatch):
    c, calls = make_client([ok("Xin chào")], monkeypatch)
    events = list(c.stream("hi", model="gpt-4o", conversation_id="c9", parent_message_id="p"))
    assert [e.text for e in events if e.type == "delta"] == ["Xin chào"]
    done = events[-1]
    assert (done.type, done.text, done.conversation_id, done.message_id) == ("done", "Xin chào", "c9", "m9")
    p = calls[0]["payload"]
    assert p["model"] == "gpt-4o" and p["conversation_id"] == "c9" and p["parent_message_id"] == "p"
    assert calls[0]["headers"]["openai-sentinel-chat-requirements-token"] == "chat"


def test_refreshes_token_on_401(monkeypatch):
    c, calls = make_client([FakeResp(401), ok()], monkeypatch)
    assert c.ask("hi").text == "Hi"
    assert [x["auth"] for x in calls] == ["Bearer tok1", "Bearer tok2"]


def test_retries_on_429_then_gives_up(monkeypatch):
    c, calls = make_client([FakeResp(429), FakeResp(503), ok()], monkeypatch)
    assert c.ask("hi").text == "Hi"
    c, calls = make_client([FakeResp(429)] * 3, monkeypatch)
    with pytest.raises(ChatGPTError) as ei:
        c.ask("hi")
    assert ei.value.status == 429 and len(calls) == 3


def test_client_error_not_retried(monkeypatch):
    c, calls = make_client([FakeResp(404)], monkeypatch)
    with pytest.raises(ChatGPTError) as ei:
        c.ask("hi")
    assert ei.value.status == 404 and len(calls) == 1
