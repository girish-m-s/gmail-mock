"""Minimal multipart handling for media uploads and batch requests.

The stdlib email parser re-serialises ``message/rfc822`` parts, which would change
the uploaded bytes, so parts are split by boundary instead.
"""

from __future__ import annotations

import re

from .errors import bad_request


def _headers(block: bytes) -> dict[str, str]:
    out = {}
    for line in block.decode("latin-1").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _split_head(chunk: bytes) -> tuple[bytes, bytes]:
    m = re.search(rb"\r?\n\r?\n", chunk)
    if not m:
        return chunk, b""
    return chunk[: m.start()], chunk[m.end() :]


def split(body: bytes, content_type: str) -> list[tuple[dict[str, str], bytes]]:
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise bad_request("Missing multipart boundary")
    boundary = b"--" + m.group(1).encode()
    parts = []
    for chunk in body.split(boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, data = _split_head(chunk)
        parts.append((_headers(head), data))
    return parts


def parse_http_request(data: bytes) -> tuple[str, str, dict[str, str], bytes]:
    """Parse an ``application/http`` batch part into (method, target, headers, body)."""
    head, body = _split_head(data.lstrip(b"\r\n"))
    lines = head.decode("latin-1").splitlines()
    if not lines:
        raise bad_request("Empty batch part")
    request_line = lines[0].split()
    if len(request_line) < 2:
        raise bad_request("Invalid batch request line")
    return request_line[0].upper(), request_line[1], _headers("\n".join(lines[1:]).encode("latin-1")), body
