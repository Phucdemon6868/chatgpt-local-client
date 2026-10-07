import json

import pytest

from core.sse import SSEError, SSEParser


def feed(parser, objs):
    out = []
    for o in objs:
        data = o if isinstance(o, str) else json.dumps(o)
        out += parser.feed_line(f"data: {data}")
    return "".join(out)


def legacy(text, msg_id="a1", role="assistant", ctype="text", recipient="all"):
    return {
        "conversation_id": "c1",
        "message": {
            "id": msg_id, "author": {"role": role}, "recipient": recipient,
            "content": {"content_type": ctype, "parts": [text]},
            "metadata": {"model_slug": "gpt-4o"},
        },
    }


def test_legacy_format_streams_only_new_text():
    p = SSEParser()
    streamed = feed(p, [legacy("Hel"), legacy("Hello"), legacy("Hello world"), "[DONE]"])
    assert streamed == "Hello world"
    assert p.text == "Hello world"
    assert p.conversation_id == "c1" and p.message_id == "a1" and p.model == "gpt-4o"
    assert p.done


def test_legacy_ignores_user_echo_tools_and_thoughts():
    p = SSEParser()
    streamed = feed(p, [
        legacy("câu hỏi", msg_id="u1", role="user"),
        legacy("search(...)", msg_id="t1", recipient="browser"),
        legacy("thinking", msg_id="th", ctype="thoughts"),
        legacy("Trả lời", msg_id="a1"),
    ])
    assert streamed == "Trả lời"


def test_delta_v1_format():
    p = SSEParser()
    add = legacy("")
    streamed = feed(p, [
        '"v1"',
        {"p": "", "o": "add", "v": add, "c": 0},
        {"p": "/message/content/parts/0", "o": "append", "v": "Xin"},
        {"v": " chào"},
        {"p": "", "o": "patch", "v": [
            {"p": "/message/content/parts/0", "o": "append", "v": "!"},
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
        ]},
        {"type": "message_stream_complete", "conversation_id": "c1"},
        "[DONE]",
    ])
    assert streamed == "Xin chào!"
    assert p.text == "Xin chào!"
    assert p.conversation_id == "c1"


def test_multiple_assistant_messages_are_joined():
    p = SSEParser()
    streamed = feed(p, [legacy("Một", msg_id="a1"), legacy("", msg_id="a2"), legacy("Hai", msg_id="a2")])
    assert streamed == "Một\n\nHai"
    assert p.text == "Một\n\nHai"
    assert p.message_id == "a2"


def test_error_chunk_raises():
    p = SSEParser()
    with pytest.raises(SSEError):
        feed(p, [{"error": "Too many requests"}])
