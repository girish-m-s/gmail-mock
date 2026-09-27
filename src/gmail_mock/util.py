"""Small helpers shared across the mock."""

from __future__ import annotations

import base64
import binascii
import itertools
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .errors import ApiError


def now_ms() -> int:
    return int(time.time() * 1000)


def rfc3339(ms: int | None = None) -> str:
    ts = (ms if ms is not None else now_ms()) / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def b64url(data: bytes) -> str:
    """Gmail encodes bytes fields (raw, body.data) as padded base64url."""
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64decode_any(value: str) -> bytes:
    """Decode standard or URL-safe base64, with or without padding."""
    s = re.sub(r"\s+", "", value).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def compact(d: dict) -> dict:
    """Drop empty lists/dicts/None, the way proto3 JSON omits unset repeated fields."""
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


# --- pagination -------------------------------------------------------------


def encode_page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")


def decode_page_token(token: str | None) -> int:
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        prefix, offset = raw.split(":", 1)
        if prefix != "offset":
            raise ValueError
        return max(0, int(offset))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise ApiError(400, "Invalid pageToken") from None


def paginate(items: list, page_token: str | None, page_size: int) -> tuple[list, str | None]:
    start = decode_page_token(page_token)
    end = start + page_size
    return items[start:end], (encode_page_token(end) if end < len(items) else None)


# --- ids --------------------------------------------------------------------


class IdGenerator:
    """Gmail-shaped identifiers. Message ids are increasing 16-char hex strings."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def _next(self) -> int:
        with self._lock:
            return next(self._counter)

    def message(self) -> str:
        return f"{0x1920000000000000 + (self._next() << 12) + secrets.randbelow(4096):016x}"

    def draft(self) -> str:
        return f"r{secrets.randbelow(10**18) + 10**18}"

    def label(self) -> str:
        return f"Label_{self._next()}"

    def filter(self) -> str:
        return "ANe1Bm" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:28]

    def opaque(self, prefix: str = "") -> str:
        return prefix + secrets.token_hex(8)

    def contact(self, prefix: str = "c") -> str:
        return f"{prefix}{secrets.randbelow(10**18) + 10**18}"
