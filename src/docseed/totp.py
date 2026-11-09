"""確認コード（TOTP, RFC 6238）と復旧コード。docs/design/extensions.md 5.3。

標準ライブラリだけで書く（30 秒、6 桁、HMAC-SHA1）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6
WINDOW = 1          # 前後 1 つずつの時間のずれを許す
RECOVERY_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_secret() -> str:
    """20 バイトの秘密の値（base32、= の詰め物なし）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def step_at(t: float | None = None) -> int:
    return int((time.time() if t is None else t) // STEP)


def code_at(secret: str, t: float | None = None) -> str:
    return hotp(_key(secret), step_at(t))


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


def match(secret: str, code: str, *, last_step: int | None, t: float | None = None) -> int | None:
    """コードが合えば、その時刻の番号を返す。last_step 以前の番号（使用済み）は受け付けない。"""
    code = normalize_code(code)
    if len(code) != DIGITS:
        return None
    key = _key(secret)
    now = step_at(t)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "docseed") -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer})


def qr_data_url(text: str) -> str:
    """QR コードの SVG を data: URL で返す（CSP の img-src data: で表示できる）。"""
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=5, border=2)


def format_secret(secret: str) -> str:
    """手で入力しやすいよう 4 文字ごとに区切る。"""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------------------------------------------------------------------------
# 復旧コード（xxxx-xxxx-xxxx。DB には SHA-256 を保存する）
# ---------------------------------------------------------------------------

def new_recovery_codes(n: int = RECOVERY_COUNT) -> list[str]:
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        out.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return out


def normalize_recovery(code: str) -> str:
    raw = "".join(ch for ch in code.lower() if ch in _RECOVERY_ALPHABET)
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}" if len(raw) == 12 else ""


def recovery_hash(code: str) -> str:
    return hashlib.sha256(normalize_recovery(code).encode()).hexdigest()
