"""RFC 822 parsing and composition, and the Gmail ``MessagePart`` projection."""

from __future__ import annotations

import email
import hashlib
import html
import re
from collections.abc import Iterable
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from .util import b64url

SMTP_POLICY = policy.default.clone(linesep="\r\n")


def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


def parse_headers(raw: bytes) -> EmailMessage:
    """Parse only the header block; the body is left unparsed."""
    head, sep, _ = split_head(raw)
    return BytesHeaderParser(policy=policy.default).parsebytes(head + sep)  # type: ignore[return-value]


def split_head(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """(header block, blank-line separator, body). Headers-only input yields an empty separator."""
    m = re.search(rb"\r?\n\r?\n", raw)
    if not m:
        return raw, b"", b""
    return raw[: m.start()], raw[m.start() : m.end()], raw[m.end() :]


def edit_headers(raw: bytes, *, remove=(), add=()) -> bytes:
    """Drop headers by name and prepend new ones, leaving the body bytes untouched."""
    head, sep, body = split_head(raw)
    newline = b"\r\n" if b"\r\n" in (sep or head[:2000]) else b"\n"
    for name in remove:
        head = re.sub(rb"(?im)^" + re.escape(name.encode()) + rb":[^\n]*\n?(?:[ \t][^\n]*\n?)*", b"", head + newline).rstrip(b"\r\n")
    added = b"".join(f"{name}: {value}".encode() + newline for name, value in add)
    return added + head + (sep or newline * 2) + body


def serialize(msg: EmailMessage) -> bytes:
    return msg.as_bytes(policy=SMTP_POLICY)


def header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name)
    return str(value) if value is not None else ""


def addresses(msg: EmailMessage, *names: str) -> list[tuple[str, str]]:
    values = [str(v) for n in names for v in (msg.get_all(n) or [])]
    return [(name, addr.lower()) for name, addr in getaddresses(values) if addr]


def date_ms(msg: EmailMessage) -> int | None:
    value = msg.get("Date")
    if not value:
        return None
    try:
        return int(parsedate_to_datetime(str(value)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def normalize_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|fwd?|aw|wg|sv)\s*:\s*)+", "", subject, flags=re.I).strip().lower()


def is_attachment(part: EmailMessage) -> bool:
    return bool(part.get_filename()) or part.get_content_disposition() == "attachment"


def _is_container(part: EmailMessage) -> bool:
    return part.get_content_maintype() == "multipart" and part.is_multipart()


def _leaf_bytes(part: EmailMessage) -> bytes:
    if part.get_content_maintype() == "message":
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            return inner[0].as_bytes()
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def attachment_id(message_id: str, part_id: str) -> str:
    digest = hashlib.sha256(f"{message_id}:{part_id}".encode()).digest()
    return "ANGjdJ" + b64url(digest + hashlib.sha256(digest).digest()).rstrip("=")


def build_payload(msg: EmailMessage, message_id: str) -> tuple[dict, dict[str, bytes]]:
    """Project a parsed message onto Gmail's MessagePart tree.

    Leaf parts with a filename (or disposition attachment) get ``body.attachmentId``;
    other leaves carry their content inline in ``body.data``.
    """
    attachments: dict[str, bytes] = {}

    def walk(part: EmailMessage, part_id: str) -> dict:
        out = {
            "partId": part_id,
            "mimeType": part.get_content_type(),
            "filename": part.get_filename() or "",
            "headers": [{"name": k, "value": str(v)} for k, v in part.items()],
        }
        if _is_container(part):
            out["body"] = {"size": 0}
            out["parts"] = [walk(sub, f"{part_id}.{i}" if part_id else str(i)) for i, sub in enumerate(part.iter_parts())]
            return out
        data = _leaf_bytes(part)
        if is_attachment(part):
            aid = attachment_id(message_id, part_id)
            attachments[aid] = data
            out["body"] = {"attachmentId": aid, "size": len(data)}
        elif data:
            out["body"] = {"size": len(data), "data": b64url(data)}
        else:
            out["body"] = {"size": 0}
        return out

    return walk(msg, ""), attachments


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except (LookupError, KeyError, ValueError):
        data = part.get_payload(decode=True) or b""
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else ""


def strip_html(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def text_content(msg: EmailMessage) -> str:
    plain, rich = [], []
    for part in msg.walk():
        if part.is_multipart() or is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            rich.append(strip_html(_part_text(part)))
    return "\n".join(plain) if plain else "\n".join(rich)


def filenames(msg: EmailMessage) -> list[str]:
    return [p.get_filename() for p in msg.walk() if not p.is_multipart() and p.get_filename()]


def snippet(msg: EmailMessage) -> str:
    text = re.sub(r"\s+", " ", text_content(msg)).strip()[:200]
    return html.escape(text, quote=True).replace("&#x27;", "&#39;")


def compose(
    *,
    sender: str,
    to: Iterable[str] = (),
    cc: Iterable[str] = (),
    bcc: Iterable[str] = (),
    subject: str = "",
    text: str | None = None,
    html_body: str | None = None,
    attachments: Iterable[tuple[str, str, bytes]] = (),
    in_reply_to: str | None = None,
    references: str | None = None,
    date: int | None = None,
    message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Build an RFC 822 message. ``attachments`` is ``(filename, mime_type, data)``."""
    msg = EmailMessage()
    msg["From"] = sender
    for name, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        values = [v for v in values if v]
        if values:
            msg[name] = ", ".join(values)
    msg["Subject"] = subject
    msg["Date"] = formatdate((date / 1000) if date else None, usegmt=True)
    msg["Message-ID"] = message_id or make_msgid(domain="mail.gmail.com")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    if text is None and html_body is None:
        text = ""
    if text is not None:
        msg.set_content(text)
        if html_body is not None:
            msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(html_body, subtype="html")
    for filename, mime_type, data in attachments:
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)
    return serialize(msg)
