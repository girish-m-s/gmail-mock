"""Stateful handlers, keyed by discovery method id (e.g. ``gmail.users.messages.get``).

Methods without a handler fall back to schema-generated responses (see ``generate``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from ..errors import bad_request
from ..spec import Method
from ..store import Mailbox, Store
from ..util import b64decode_any

Handler = Callable[["Ctx"], Any]
REGISTRY: dict[str, Handler] = {}


def handler(*method_ids: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for mid in method_ids:
            REGISTRY[mid] = fn
        return fn

    return register


@dataclass
class Ctx:
    store: Store
    method: Method
    auth_email: str
    path: dict[str, str]
    query: dict[str, Any]
    body: Any
    media: bytes | None

    @cached_property
    def mailbox(self) -> Mailbox:
        if self.method.api.name == "gmail":
            return self.store.resolve_user(self.path.get("userId", "me"), self.auth_email)
        return self.store.ensure_mailbox(self.auth_email)

    @property
    def data(self) -> dict:
        return self.body if isinstance(self.body, dict) else {}

    def q(self, name: str, default: Any = None) -> Any:
        return self.query.get(name, default)

    def raw_from(self, message: dict | None) -> bytes:
        """The RFC 822 bytes from ``message.raw`` or an /upload/ media body."""
        if self.media is not None:
            return self.media
        raw = (message or {}).get("raw")
        if not raw:
            raise bad_request("'raw' RFC822 payload message string or uploading message via /upload/* URL required")
        return b64decode_any(raw)


def load_all() -> None:
    from . import gmail, people, settings  # noqa: F401  (registers handlers)
