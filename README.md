# chatgpt-local-client

Dùng tài khoản ChatGPT web (qua cookie) như một "backend" local:

- **Proxy tương thích OpenAI API**: `http://localhost:8000/v1`. Cursor, Continue.dev, Aider, Open WebUI, LangChain, SDK `openai`… đều dùng được.
- **Web UI local** (Gradio): lịch sử lưu SQLite, đổi model, streaming.
- **CLI chat** trong terminal.

> ⚠️ Đây là API **không chính thức**. Cách dùng này vi phạm điều khoản của OpenAI, tài khoản có thể bị giới hạn hoặc khóa. Chỉ dùng tài khoản của chính bạn, cho mục đích cá nhân. Cookie bằng với quyền đăng nhập: **không commit `.env`, không chia sẻ cookie**. Đừng mở proxy ra Internet; nếu cần thì đặt `PROXY_API_KEY`.

## Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env    # điền CHATGPT_COOKIE
```

Lấy cookie: mở https://chatgpt.com và đăng nhập → F12 → Network → chọn request bất kỳ tới chatgpt.com → copy giá trị header `cookie`.

## 1. Proxy OpenAI-compatible

```bash
python -m server --port 8000
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="PROXY_API_KEY-hoặc-gì-cũng-được")
r = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Xin chào"}])
print(r.choices[0].message.content)
```

```bash
curl http://localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

| Công cụ | Cấu hình |
|---|---|
| Continue.dev / Cursor | provider `openai`, `apiBase: http://localhost:8000/v1` |
| Aider | `aider --openai-api-base http://localhost:8000/v1 --model openai/auto` |
| Open WebUI | Settings → Connections → OpenAI API → `http://localhost:8000/v1` |

**Cách nối lịch sử.** API OpenAI là stateless (gửi lại toàn bộ lịch sử mỗi lượt), còn ChatGPT web giữ trạng thái theo thread. Proxy hash phần lịch sử trước tin nhắn cuối:
- Nếu đã thấy hash đó: nối tiếp thread cũ và chỉ gửi tin nhắn mới.
- Nếu chưa thấy, lịch sử bị sửa, hoặc thread cũ không dùng được nữa: gộp cả lịch sử (kèm system prompt) thành một prompt và mở thread mới.

Mặc định proxy dùng **chat tạm** (`CHATGPT_TEMPORARY_CHAT=1`), nên không làm đầy lịch sử ChatGPT của bạn.

Giới hạn: không hỗ trợ function calling/`tools`, ảnh đầu vào, `temperature`… (các trường này bị bỏ qua); `usage` luôn trả về 0.

## 2. Web UI

```bash
python -m ui.app --port 7860   # mở http://127.0.0.1:7860
```

Hội thoại được lưu ở `data/chats.db` và nối tiếp đúng thread ChatGPT kể cả sau khi khởi động lại.

## 3. CLI

```bash
python chat.py      # /new, /model <tên>, /models, /help, /quit
```

## Cấu trúc

```
core/
  config.py    # đọc .env
  auth.py      # cookie → accessToken, cache + tự refresh
  sentinel.py  # chat-requirements + proof-of-work
  sse.py       # parser SSE (định dạng cũ + delta v1)
  client.py    # ChatGPTClient.stream()/ask()/list_models(), retry/backoff
  store.py     # SQLite: hội thoại UI + cache thread của proxy
server/        # FastAPI /v1/chat/completions, /v1/models
ui/            # Gradio
chat.py        # CLI
tests/         # pytest, không cần mạng/cookie
```

Client tự xử lý:
- **401**: lấy accessToken mới rồi thử lại.
- **429/5xx**: backoff theo `Retry-After` hoặc lũy thừa, tối đa `CHATGPT_MAX_RETRIES` lần.

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```
