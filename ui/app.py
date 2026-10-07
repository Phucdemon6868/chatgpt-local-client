"""
Giao diện web local (Gradio): lịch sử hội thoại lưu SQLite, đổi model, streaming.
Chạy: python -m ui.app [--port 7860]
"""

from __future__ import annotations

import argparse
import logging

import gradio as gr

from core.client import ChatGPTClient, ChatGPTError
from core.config import Settings, load_settings
from core.store import Store

log = logging.getLogger(__name__)


def build_ui(settings: Settings, store: Store, client: ChatGPTClient | None = None) -> gr.Blocks:
    _client: list[ChatGPTClient | None] = [client]

    def get_client() -> ChatGPTClient:
        if _client[0] is None:
            _client[0] = ChatGPTClient(settings)
        return _client[0]

    def conv_choices() -> list[tuple[str, str]]:
        return [(c.title, c.id) for c in store.list_conversations()]

    def load_conversation(conv_id: str | None):
        if not conv_id:
            return [], None
        conv = store.get_conversation(conv_id)
        model = conv.model if conv else gr.update()
        return store.get_messages(conv_id), model

    def new_chat():
        return [], None, gr.update(choices=conv_choices(), value=None)

    def delete_chat(conv_id: str | None):
        if conv_id:
            store.delete_conversation(conv_id)
        return [], None, gr.update(choices=conv_choices(), value=None)

    def send(text: str, history: list, conv_id: str | None, model: str):
        text = (text or "").strip()
        model = model or settings.default_model
        if not text:
            yield history, "", conv_id, gr.update()
            return

        if not conv_id:
            conv_id = store.create_conversation(text.splitlines()[0][:60], model).id
        conv = store.get_conversation(conv_id)
        store.add_message(conv_id, "user", text)

        history = list(history or []) + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": ""},
        ]
        yield history, "", conv_id, gr.update(choices=conv_choices(), value=conv_id)

        reply = ""
        try:
            for ev in get_client().stream(
                text, model=model,
                conversation_id=conv.remote_id if conv else None,
                parent_message_id=conv.parent_message_id if conv else None,
            ):
                if ev.type == "delta":
                    reply += ev.text
                    history[-1]["content"] = reply
                    yield history, "", conv_id, gr.update()
                else:
                    reply = ev.text or reply
                    store.update_thread(conv_id, remote_id=ev.conversation_id,
                                        parent_message_id=ev.message_id, model=model)
        except ChatGPTError as e:
            history[-1]["content"] = (reply + f"\n\n⚠️ Lỗi: {e}").strip()
            yield history, text, conv_id, gr.update()   # trả lại text để gửi lại
            return

        history[-1]["content"] = reply
        store.add_message(conv_id, "assistant", reply)
        yield history, "", conv_id, gr.update(choices=conv_choices(), value=conv_id)

    with gr.Blocks(title="ChatGPT Local") as demo:
        conv_state = gr.State(None)
        with gr.Row():
            with gr.Column(scale=1, min_width=240):
                model = gr.Dropdown(settings.models, value=settings.default_model,
                                    label="Model", allow_custom_value=True)
                new_btn = gr.Button("＋ Hội thoại mới", variant="primary")
                conv_list = gr.Radio(conv_choices(), label="Lịch sử", value=None)
                del_btn = gr.Button("🗑 Xóa hội thoại đang chọn", variant="stop")
            with gr.Column(scale=4):
                chatbot = gr.Chatbot(height=620, label="ChatGPT")
                with gr.Row():
                    box = gr.Textbox(placeholder="Nhập tin nhắn… (Enter để gửi, Shift+Enter xuống dòng)",
                                     show_label=False, scale=8, lines=2)
                    send_btn = gr.Button("Gửi", variant="primary", scale=1)

        outputs = [chatbot, box, conv_state, conv_list]
        box.submit(send, [box, chatbot, conv_state, model], outputs)
        send_btn.click(send, [box, chatbot, conv_state, model], outputs)
        new_btn.click(new_chat, None, [chatbot, conv_state, conv_list])
        del_btn.click(delete_chat, conv_state, [chatbot, conv_state, conv_list])
        conv_list.input(lambda cid: (*load_conversation(cid), cid), conv_list, [chatbot, model, conv_state])

    return demo


def main() -> None:
    ap = argparse.ArgumentParser(description="Web UI local cho ChatGPT")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    build_ui(settings, Store(settings.db_path)).queue().launch(server_name=args.host, server_port=args.port)


if __name__ == "__main__":
    main()
