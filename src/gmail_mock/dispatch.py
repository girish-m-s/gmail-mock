"""Framework-agnostic request pipeline: route -> auth -> fault injection -> validate -> handle."""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import parse_qsl, urlsplit

from . import fields as partial
from . import multipart
from .errors import ApiError, unauthenticated
from .generate import generate
from .handlers import REGISTRY, Ctx, load_all
from .spec import Catalog
from .store import Store
from .validate import validate_body, validate_query

log = logging.getLogger("gmail_mock")
load_all()

_NOT_FOUND_HTML = (
    "<!DOCTYPE html>\n<html lang=en>\n  <meta charset=utf-8>\n  <title>Error 404 (Not Found)!!1</title>\n"
    "  <p><b>404.</b> <ins>That’s an error.</ins>\n"
    "  <p>The requested URL <code>{path}</code> was not found on this server.  <ins>That’s all we know.</ins>\n"
)


@dataclass
class RawRequest:
    method: str
    path: str  # percent-encoded path, no query string
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""


@dataclass
class RawResponse:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=UTF-8"
    headers: dict[str, str] = field(default_factory=dict)


class Dispatcher:
    def __init__(self, store: Store, catalog: Catalog | None = None, require_auth: bool = True) -> None:
        self.store = store
        self.catalog = catalog or Catalog()
        self.require_auth = require_auth

    # --- entry points ------------------------------------------------------------------

    def handle(self, req: RawRequest) -> RawResponse:
        started = time.perf_counter()
        method_id, body_for_log = None, None
        try:
            match = self.catalog.match(req.method, req.path)
            if match is None:
                return RawResponse(404, _NOT_FOUND_HTML.format(path=req.path).encode(), "text/html; charset=UTF-8")
            method, path_params, is_upload = match
            method_id = method.id
            auth_email = self._authenticate(req)
            self._inject_fault(method.id)
            query = validate_query(method, req.query)
            body, media = self._read_body(method, req, query, is_upload)
            body_for_log = body
            with self.store.lock:
                ctx = Ctx(self.store, method, auth_email, path_params, query, body, media)
                fn = REGISTRY.get(method.id)
                result = fn(ctx) if fn else generate(method.api, method.response_ref, body)
                # Serialise while holding the lock: results may share dicts with live state.
                response = self._render(result, query)
        except ApiError as err:
            response = RawResponse(err.code, json.dumps(err.to_dict(), indent=2).encode())
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error")
            err = ApiError(500, f"Internal error in gmail-mock: {exc}")
            response = RawResponse(500, json.dumps(err.to_dict(), indent=2).encode())
        self._log(req, method_id, body_for_log, response, started)
        return response

    def handle_batch(self, req: RawRequest) -> RawResponse:
        try:
            parts = multipart.split(req.body, req.headers.get("content-type", ""))
            if len(parts) > 100:
                raise ApiError(400, "Too many requests in batch (max 100)")
        except ApiError as err:
            return RawResponse(err.code, json.dumps(err.to_dict(), indent=2).encode())
        boundary = "batch_" + secrets.token_hex(12)
        chunks = []
        for headers, data in parts:
            method, target, inner_headers, inner_body = multipart.parse_http_request(data)
            if "authorization" not in inner_headers and "authorization" in req.headers:
                inner_headers["authorization"] = req.headers["authorization"]
            url = urlsplit(target)
            resp = self.handle(RawRequest(method, url.path, parse_qsl(url.query, keep_blank_values=True), inner_headers, inner_body))
            content_id = headers.get("content-id", "").strip("<>")
            reason = HTTPStatus(resp.status).phrase if resp.status in HTTPStatus._value2member_map_ else ""
            head = (
                f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <response-{content_id}>\r\n\r\n"
                f"HTTP/1.1 {resp.status} {reason}\r\nContent-Type: {resp.content_type}\r\nContent-Length: {len(resp.body)}\r\n\r\n"
            )
            chunks.append(head.encode() + resp.body + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return RawResponse(200, b"".join(chunks), f"multipart/mixed; boundary={boundary}")

    # --- pipeline steps ---------------------------------------------------------------

    def _authenticate(self, req: RawRequest) -> str:
        token = None
        header = req.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        else:
            token = next((v for k, v in req.query if k in ("access_token", "oauth_token")), None)
        if not token:
            if self.require_auth:
                raise unauthenticated()
            return self.store.default_email
        # Any token is accepted. A token that is an email address selects that mailbox.
        return token.lower() if "@" in token else self.store.default_email

    def _inject_fault(self, method_id: str) -> None:
        with self.store.lock:
            for fault in self.store.faults:
                if fault["methodId"] in (method_id, "*") and fault["remaining"] != 0:
                    fault["remaining"] -= 1
                    raise ApiError(fault["status"], fault.get("message") or HTTPStatus(fault["status"]).phrase, reason=fault.get("reason"))

    def _read_body(self, method, req: RawRequest, query: dict, is_upload: bool):
        ctype = req.headers.get("content-type", "")
        if is_upload:
            upload_type = query.get("uploadType") or ("multipart" if ctype.startswith("multipart/") else "media")
            if upload_type == "resumable" or "/resumable/" in req.path:
                raise ApiError(501, "Resumable uploads are not supported by gmail-mock; use uploadType=multipart or media")
            meta: dict = {}
            if ctype.startswith("multipart/"):
                parts = multipart.split(req.body, ctype)
                if len(parts) < 2:
                    raise ApiError(400, "Multipart upload requires metadata and media parts")
                if parts[0][1].strip():
                    meta = self._json(parts[0][1])
                media = parts[1][1]
            else:
                media = req.body
            body = validate_body(method.api, method.request_ref, meta) if method.request_ref else meta
            return body, media
        if not req.body.strip():
            return ({} if method.request_ref else None), None
        data = self._json(req.body)
        if method.request_ref is None:
            return None, None
        return validate_body(method.api, method.request_ref, data), None

    @staticmethod
    def _json(raw: bytes):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"Invalid JSON payload received. {exc}") from None

    def _render(self, result, query: dict) -> RawResponse:
        if result is None:
            return RawResponse(204, b"")
        if query.get("fields"):
            result = partial.apply(result, partial.parse(query["fields"]))
        # Compact by default (the C encoder is far faster); prettyPrint=true indents like Google.
        if query.get("prettyPrint") is True:
            return RawResponse(200, json.dumps(result, indent=2, ensure_ascii=False).encode())
        return RawResponse(200, json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())

    def _log(self, req: RawRequest, method_id, body, resp: RawResponse, started: float) -> None:
        self.store.requests.append(
            {
                "time": time.time(),
                "method": req.method,
                "path": req.path,
                "methodId": method_id,
                "query": [list(item) for item in req.query],
                "body": _abbreviate(body),
                "status": resp.status,
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
            }
        )


def _abbreviate(value, limit: int = 2048):
    """Keep the request log small: long strings (e.g. base64 ``raw``) become a placeholder."""
    if isinstance(value, str) and len(value) > limit:
        return f"<{len(value)} chars>"
    if isinstance(value, dict):
        return {k: _abbreviate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_abbreviate(v, limit) for v in value]
    return value
