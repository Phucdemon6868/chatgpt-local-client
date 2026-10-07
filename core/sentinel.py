"""
Sentinel token (chat-requirements + proof-of-work).

chat-requirements là POST: gửi 1 lời giải PoW ban đầu (p), server trả về token
dùng cho header openai-sentinel-chat-requirements-token. Nếu server yêu cầu PoW
thì giải thêm và gửi qua header openai-sentinel-proof-token.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pybase64
import requests

SENTINEL_REQ_URL = "https://chatgpt.com/backend-api/sentinel/chat-requirements"
MAX_ITERATION_POW = 500_000

_DEFAULT_CORES = [2, 4, 8, 12, 16]
_DEFAULT_CACHED_SCRIPTS = ["https://cdn.oaistatic.com/_next/static/chunks/webpack.js", ""]
_DEFAULT_CACHED_DPL = ["some_dpl_value_1", ""]
_DEFAULT_NAV_KEYS = ["webdriver", "languages", "userAgent", "platform", "doNotTrack", "appName"]
_DEFAULT_DOC_KEYS = ["readyState", "referrer", "visibilityState", "documentElement", "title", "URL"]
_DEFAULT_WIN_KEYS = ["innerWidth", "innerHeight", "screenX", "screenY", "devicePixelRatio"]


class SentinelError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class SentinelTokens:
    chat_token: str
    proof_token: str | None = None

    def headers(self) -> dict[str, str]:
        h = {"openai-sentinel-chat-requirements-token": self.chat_token}
        if self.proof_token:
            h["openai-sentinel-proof-token"] = self.proof_token
        return h


def _get_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def _build_pow_config(user_agent: str) -> list:
    screen_sum = random.choice([1920 + 1080, 2560 + 1440, 1920 + 1200])
    return [
        screen_sum, _get_parse_time(), 4294705152, 0, user_agent,
        random.choice(_DEFAULT_CACHED_SCRIPTS), random.choice(_DEFAULT_CACHED_DPL),
        "en-US", "en-US,es-US,en,es", 0,
        random.choice(_DEFAULT_NAV_KEYS),
        random.choice(_DEFAULT_DOC_KEYS),
        random.choice(_DEFAULT_WIN_KEYS),
        time.perf_counter() * 1000,
        str(uuid.uuid4()), "",
        random.choice(_DEFAULT_CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
    ]


def generate_proof_token(seed: str, difficulty: str, user_agent: str) -> str:
    """
    Giải PoW: tìm config sao cho tiền tố hex của SHA3-512(seed + base64(config))
    <= difficulty. Kết quả có tiền tố gAAAAAB.
    """
    config = _build_pow_config(user_agent)
    diff_len = len(difficulty)
    seed_enc = seed.encode()

    for i in range(MAX_ITERATION_POW):
        config[3] = i
        config[9] = i >> 1
        json_data = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
        base = pybase64.b64encode(json_data.encode()).decode()
        h = hashlib.sha3_512(seed_enc + base.encode()).hexdigest()
        if h[:diff_len] <= difficulty:
            return "gAAAAAB" + base

    # fallback nếu không tìm được đáp án trong giới hạn vòng lặp
    return "gAAAAABwQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + pybase64.b64encode(f'"{seed}"'.encode()).decode()


def fetch_sentinel_tokens(session: requests.Session, user_agent: str, timeout: int = 15) -> SentinelTokens:
    """POST chat-requirements → SentinelTokens. Ném SentinelError nếu thất bại."""
    try:
        resp = session.post(
            SENTINEL_REQ_URL,
            data=json.dumps({"p": generate_proof_token("", "0", user_agent)}),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise SentinelError(f"Lỗi mạng khi lấy sentinel: {e}") from e

    if resp.status_code != 200:
        raise SentinelError(f"chat-requirements HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)

    data = resp.json()
    pow_d = data.get("proofofwork") or {}
    proof = None
    if pow_d.get("required"):
        proof = generate_proof_token(pow_d.get("seed", ""), pow_d.get("difficulty", ""), user_agent)
    return SentinelTokens(chat_token=data.get("token", ""), proof_token=proof)
