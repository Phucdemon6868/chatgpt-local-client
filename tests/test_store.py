from core.store import Store


def test_conversation_roundtrip(tmp_path):
    s = Store(tmp_path / "x.db")
    c = s.create_conversation("Xin chào", "auto")
    s.add_message(c.id, "user", "hi")
    s.add_message(c.id, "assistant", "hello")
    s.update_thread(c.id, remote_id="r1", parent_message_id="m1", model="gpt-4o")
    got = s.get_conversation(c.id)
    assert (got.remote_id, got.parent_message_id, got.model) == ("r1", "m1", "gpt-4o")
    assert s.get_messages(c.id) == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    # None không ghi đè giá trị cũ
    s.update_thread(c.id, remote_id=None, parent_message_id="m2")
    assert s.get_conversation(c.id).remote_id == "r1"
    s.delete_conversation(c.id)
    assert s.get_conversation(c.id) is None and s.get_messages(c.id) == []


def test_proxy_threads(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.get_proxy_thread("h") is None
    s.save_proxy_thread("h", "r", "p")
    assert s.get_proxy_thread("h") == ("r", "p")
    s.delete_proxy_thread("h")
    assert s.get_proxy_thread("h") is None
