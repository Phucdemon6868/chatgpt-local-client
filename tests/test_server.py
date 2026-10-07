import json

import pytest
from fastapi.testclient import TestClient

from core.client import ChatGPTError, StreamEvent
from core.config import Settings
from core.store import Store
from server.openai_compat import create_app, flatten


class FakeClient:
    def __init__(self, fail_continue_status=None):
        self.calls = []
        self.n = 0
        self.fail_continue_status = fail_continue_status

    def list_models(self):
        return ["auto", "gpt-4o"]

    def stream(self, prompt, *, model=None, conversation_id=None, parent_message_id=None, temporary=False):
        self.calls.append({"prompt": prompt, "conv": conversation_id, "parent": parent_message_id,
                           "model": model, "temporary": temporary})
        if conversation_id and self.fail_continue_status:
            raise ChatGPTError("gone", self.fail_continue_status)
        self.n += 1
        reply = f"reply{self.n}"
        yield StreamEvent("delta", reply[:3])
        yield StreamEvent("delta", reply[3:])
        yield StreamEvent("done", reply, conversation_id=conversation_id or "conv1", message_id=f"m{self.n}")


@pytest.fixture
def setup(tmp_path):
    def _make(key="", **kw):
        fake = FakeClient(**kw)
        app = create_app(Settings(cookie="x", proxy_api_key=key), client=fake, store=Store(tmp_path / "t.db"))
        return TestClient(app), fake
    return _make


def test_models(setup):
    http, _ = setup()
    ids = [m["id"] for m in http.get("/v1/models").json()["data"]]
    assert ids == ["auto", "gpt-4o"]


def test_api_key_required(setup):
    http, _ = setup(key="secret")
    assert http.get("/v1/models").status_code == 401
    assert http.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_non_stream_and_thread_continuation(setup):
    http, fake = setup()
    msgs = [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hi"}]
    r = http.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs}).json()
    assert r["choices"][0]["message"]["content"] == "reply1"
    assert fake.calls[0]["conv"] is None and "[System instructions]\nBe brief" in fake.calls[0]["prompt"]
    assert fake.calls[0]["temporary"] is True

    # Lượt 2: lịch sử khớp → chỉ gửi tin nhắn mới vào thread cũ
    msgs += [{"role": "assistant", "content": "reply1"},
             {"role": "user", "content": [{"type": "text", "text": "More"}]}]
    r = http.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs}).json()
    assert r["choices"][0]["message"]["content"] == "reply2"
    assert fake.calls[1] == {"prompt": "More", "conv": "conv1", "parent": "m1", "model": "gpt-4o",
                             "temporary": True}

    # Lịch sử bị sửa → không khớp → gửi lại toàn bộ
    msgs[2]["content"] = "edited"
    http.post("/v1/chat/completions", json={"messages": msgs})
    assert fake.calls[2]["conv"] is None and "[Assistant]\nedited" in fake.calls[2]["prompt"]
    assert fake.calls[2]["model"] == "auto"


def test_continuation_failure_falls_back(setup):
    http, fake = setup(fail_continue_status=404)
    msgs = [{"role": "user", "content": "Hi"}]
    http.post("/v1/chat/completions", json={"messages": msgs})
    msgs += [{"role": "assistant", "content": "reply1"}, {"role": "user", "content": "Again"}]
    r = http.post("/v1/chat/completions", json={"messages": msgs}).json()
    assert r["choices"][0]["message"]["content"] == "reply2"
    assert fake.calls[1]["conv"] == "conv1" and fake.calls[2]["conv"] is None


def test_streaming_format(setup):
    http, _ = setup()
    with http.stream("POST", "/v1/chat/completions",
                     json={"stream": True, "messages": [{"role": "user", "content": "Hi"}]}) as r:
        lines = [l for l in r.iter_lines() if l]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "reply1"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_upstream_error_maps_status(setup):
    http, fake = setup()

    def boom(*a, **k):
        raise ChatGPTError("rate limited", 429)
        yield

    fake.stream = boom
    r = http.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 429
    assert r.json()["error"]["message"] == "rate limited"


def test_flatten_single_user_is_raw():
    assert flatten([{"role": "user", "content": "hello"}]) == "hello"
