"""
CLI chat trong terminal (bản gốc chatgpt.py, viết lại trên core/).
Chạy: python chat.py
"""

from __future__ import annotations

import sys

from core.client import ChatGPTClient, ChatGPTError
from core.config import load_settings

HELP_TEXT = """
Lệnh đặc biệt:
  /new       - Bắt đầu cuộc trò chuyện mới
  /model     - Đổi model (ví dụ: /model gpt-4o-mini)
  /models    - Liệt kê model của tài khoản
  /quit      - Thoát
  /help      - Hiển thị trợ giúp này
"""


def read_cookie() -> str:
    print("Dán cookie ChatGPT vào đây (nhấn Enter 2 lần để kết thúc):")
    lines = []
    while True:
        line = input()
        if line == "" and lines:
            break
        lines.append(line)
    return " ".join(lines).strip()


def main() -> None:
    settings = load_settings()
    if not settings.cookie:
        settings.cookie = read_cookie()
    if not settings.cookie:
        print("Lỗi: Chưa nhập cookie.")
        sys.exit(1)

    client = ChatGPTClient(settings)
    model = settings.default_model
    conversation_id = parent_id = None

    print("=" * 55)
    print("  ChatGPT Client  |  model:", model)
    print("  Nhập /help để xem lệnh đặc biệt")
    print("=" * 55)

    while True:
        try:
            user_input = input("\nBạn: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nThoát.")
            break
        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd, _, arg = user_input[1:].partition(" ")
            cmd = cmd.lower()
            if cmd == "quit":
                print("Thoát.")
                break
            elif cmd == "new":
                conversation_id = parent_id = None
                print("✓ Đã bắt đầu cuộc trò chuyện mới.")
            elif cmd == "model":
                if arg.strip():
                    model = arg.strip()
                    print(f"✓ Đã đổi sang model: {model}")
                else:
                    print(f"Model hiện tại: {model}\nDùng: /model <tên model>")
            elif cmd == "models":
                for m in client.list_models():
                    print(f"  {m}{' ← hiện tại' if m == model else ''}")
            elif cmd == "help":
                print(HELP_TEXT)
            else:
                print(f"Lệnh không hợp lệ: /{cmd}  —  Nhập /help để xem danh sách.")
            continue

        print(f"\nChatGPT ({model}): ", end="", flush=True)
        try:
            for ev in client.stream(user_input, model=model,
                                    conversation_id=conversation_id, parent_message_id=parent_id):
                if ev.type == "delta":
                    print(ev.text, end="", flush=True)
                else:
                    conversation_id, parent_id = ev.conversation_id, ev.message_id or parent_id
            print()
        except ChatGPTError as e:
            print(f"\n✗ Lỗi: {e}")


if __name__ == "__main__":
    main()
