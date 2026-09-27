"""A practical subset of Gmail's search syntax (the ``q`` parameter).

Supported: free text and "quoted phrases", ``-negation``, ``OR``, ``( )`` and ``{ }``
groups, and the operators from/to/cc/bcc/deliveredto/subject/label/in/is/has/
category/filename/list/rfc822msgid/after/before/older/newer/older_than/
newer_than/larger/smaller/size. Operator values may be quoted or grouped,
e.g. ``subject:(dinner movie)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .util import now_ms

if TYPE_CHECKING:
    from .store import Mailbox, Message

Predicate = Callable[["Message"], bool]

_UNITS = {"d": 86_400_000, "m": 30 * 86_400_000, "y": 365 * 86_400_000, "h": 3_600_000}
_CATEGORIES = {
    "primary": "CATEGORY_PERSONAL",
    "personal": "CATEGORY_PERSONAL",
    "social": "CATEGORY_SOCIAL",
    "promotions": "CATEGORY_PROMOTIONS",
    "updates": "CATEGORY_UPDATES",
    "forums": "CATEGORY_FORUMS",
}
_IN = {
    "inbox": "INBOX",
    "sent": "SENT",
    "draft": "DRAFT",
    "drafts": "DRAFT",
    "trash": "TRASH",
    "spam": "SPAM",
    "starred": "STARRED",
    "important": "IMPORTANT",
    "unread": "UNREAD",
    "chats": "CHAT",
    "chat": "CHAT",
}


def _norm_label(name: str) -> str:
    return re.sub(r"[\s/\-_]+", "-", name.strip().lower())


def _parse_date(value: str) -> int | None:
    if value.isdigit() and len(value) > 8:
        return int(value) * 1000
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _parse_size(value: str) -> int | None:
    m = re.fullmatch(r"(\d+)([kKmM]?)", value)
    if not m:
        return None
    n = int(m.group(1))
    return n * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]


def _tokenize(q: str) -> list:
    tokens: list = []
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
        elif c in "(){}":
            tokens.append(c)
            i += 1
        elif c == "-" and i + 1 < n and not q[i + 1].isspace():
            tokens.append("-")
            i += 1
        elif c == '"':
            j = q.find('"', i + 1)
            j = n if j == -1 else j
            tokens.append(("PHRASE", q[i + 1 : j]))
            i = j + 1
        else:
            j = i
            while j < n and not q[j].isspace() and q[j] not in '(){}"':
                j += 1
            word = q[i:j]
            if word.endswith(":") and len(word) > 1 and j < n and q[j] == '"':
                k = q.find('"', j + 1)
                k = n if k == -1 else k
                tokens.append(("OP", word[:-1].lower(), q[j + 1 : k]))
                i = k + 1
                continue
            if word.endswith(":") and len(word) > 1 and j < n and q[j] in "({":
                close = ")" if q[j] == "(" else "}"
                k = q.find(close, j + 1)
                k = n if k == -1 else k
                op = word[:-1].lower()
                inner = []
                for tok in _tokenize(q[j + 1 : k]):
                    if isinstance(tok, tuple) and tok[0] in ("WORD", "PHRASE"):
                        inner.append(("OP", op, tok[1]))
                    else:
                        inner.append(tok)
                tokens.extend(["(" if close == ")" else "{", *inner, ")" if close == ")" else "}"])
                i = k + 1
                continue
            if word in ("OR", "AND"):
                tokens.append(word)
            elif ":" in word and not word.startswith(":"):
                op, value = word.split(":", 1)
                tokens.append(("OP", op.lower(), value))
            else:
                tokens.append(("WORD", word))
            i = j
    return tokens


class Query:
    """Compile a Gmail search string against one mailbox."""

    def __init__(self, q: str | None, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.now = now_ms()
        self.includes_spam_trash = False
        self._tokens = _tokenize(q or "")
        self._i = 0
        self._pred = self._or(False) if self._tokens else (lambda m: True)

    def matches(self, message: Message) -> bool:
        return self._pred(message)

    def required_labels(self) -> set[str] | None:
        """If the query is only positive label terms (``is:unread in:inbox label:work``),
        the label ids it requires, so the caller can use the label index instead of scanning."""
        wanted: set[str] = set()
        for tok in self._tokens:
            if not isinstance(tok, tuple) or tok[0] != "OP":
                return None
            op, value = tok[1], tok[2].strip().lower()
            if op == "is" and value in ("unread", "starred", "important"):
                wanted.add(value.upper())
            elif op == "in" and value in _IN:
                wanted.add(_IN[value])
            elif op == "category" and value in _CATEGORIES:
                wanted.add(_CATEGORIES[value])
            elif op == "label":
                ids = self._label_ids(value)
                if len(ids) != 1:
                    return None
                wanted |= ids
            else:
                return None
        return wanted or None

    # --- recursive descent -------------------------------------------------

    def _peek(self):
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _next(self):
        tok = self._peek()
        self._i += 1
        return tok

    def _or(self, negated: bool) -> Predicate:
        parts = [self._and(negated)]
        while self._peek() == "OR":
            self._i += 1
            parts.append(self._and(negated))
        return parts[0] if len(parts) == 1 else (lambda m: any(p(m) for p in parts))

    def _and(self, negated: bool) -> Predicate:
        parts: list[Predicate] = []
        while (tok := self._peek()) is not None and tok not in (")", "}", "OR"):
            if tok == "AND":
                self._i += 1
                continue
            parts.append(self._unary(negated))
        if not parts:
            return lambda m: True
        return parts[0] if len(parts) == 1 else (lambda m: all(p(m) for p in parts))

    def _unary(self, negated: bool) -> Predicate:
        if self._peek() == "-":
            self._i += 1
            inner = self._unary(not negated)
            return lambda m: not inner(m)
        return self._atom(negated)

    def _atom(self, negated: bool) -> Predicate:
        tok = self._next()
        if tok == "(":
            pred = self._or(negated)
            if self._peek() == ")":
                self._i += 1
            return pred
        if tok == "{":
            parts = []
            while self._peek() not in ("}", None):
                parts.append(self._unary(negated))
            if self._peek() == "}":
                self._i += 1
            return (lambda m: any(p(m) for p in parts)) if parts else (lambda m: True)
        if not isinstance(tok, tuple):
            return lambda m: True
        if tok[0] in ("WORD", "PHRASE"):
            return self._text(tok[1])
        return self._operator(tok[1], tok[2], negated)

    # --- terms -------------------------------------------------------------

    def _text(self, value: str) -> Predicate:
        needle = value.lower()
        if not needle:
            return lambda m: True
        return lambda m: needle in m.search_fields()["all"]

    def _field(self, key: str, value: str) -> Predicate:
        needle = value.lower()
        if needle == "me" and key in ("from", "to", "cc", "bcc", "deliveredto"):
            needle = self.mailbox.email.lower()
        return lambda m: needle in m.search_fields()[key]

    def _label_ids(self, name: str) -> set[str]:
        wanted = _norm_label(name)
        return {lid for lid, label in self.mailbox.labels.items() if _norm_label(label["name"]) == wanted or lid.lower() == name.lower()}

    def _operator(self, op: str, value: str, negated: bool) -> Predicate:
        v = value.strip()
        lv = v.lower()
        if op in ("from", "to", "cc", "bcc", "deliveredto", "subject", "list"):
            return self._field(op, v)
        if op == "label":
            ids = self._label_ids(v)
            if not negated and ids & {"TRASH", "SPAM"}:
                self.includes_spam_trash = True
            return lambda m: bool(ids & set(m.label_ids))
        if op == "in":
            if lv in ("anywhere", "all"):
                if not negated:
                    self.includes_spam_trash = True
                return lambda m: True
            if lv == "snoozed":
                return lambda m: False
            label = _IN.get(lv)
            ids = {label} if label else self._label_ids(v)
            if not negated and ids & {"TRASH", "SPAM"}:
                self.includes_spam_trash = True
            return lambda m: bool(ids & set(m.label_ids))
        if op == "is":
            if lv == "read":
                return lambda m: "UNREAD" not in m.label_ids
            if lv in ("unread", "starred", "important"):
                label = lv.upper()
                return lambda m: label in m.label_ids
            return lambda m: False
        if op == "has":
            if lv == "attachment":
                return lambda m: m.search_fields()["has_attachment"]
            if lv in ("yellow-star", "star"):
                return lambda m: "STARRED" in m.label_ids
            if lv == "userlabels":
                return lambda m: any(lid in self.mailbox.labels and self.mailbox.labels[lid]["type"] == "user" for lid in m.label_ids)
            if lv == "nouserlabels":
                return lambda m: not any(lid in self.mailbox.labels and self.mailbox.labels[lid]["type"] == "user" for lid in m.label_ids)
            return lambda m: False
        if op == "category":
            label = _CATEGORIES.get(lv)
            return (lambda m: label in m.label_ids) if label else (lambda m: False)
        if op == "filename":
            return lambda m: any(lv == f.lower() or f.lower().endswith("." + lv) or lv in f.lower() for f in m.search_fields()["filenames"])
        if op == "rfc822msgid":
            want = lv.strip("<>")
            return lambda m: m.search_fields()["msgid"] == want
        if op in ("after", "newer", "before", "older"):
            ts = _parse_date(v)
            if ts is None:
                return lambda m: False
            if op in ("after", "newer"):
                return lambda m: m.internal_date >= ts
            return lambda m: m.internal_date < ts
        if op in ("older_than", "newer_than"):
            match = re.fullmatch(r"(\d+)([dmyh])", lv)
            if not match:
                return lambda m: False
            cutoff = self.now - int(match.group(1)) * _UNITS[match.group(2)]
            if op == "newer_than":
                return lambda m: m.internal_date > cutoff
            return lambda m: m.internal_date < cutoff
        if op in ("larger", "size", "smaller"):
            size = _parse_size(lv)
            if size is None:
                return lambda m: False
            if op == "smaller":
                return lambda m: m.size_estimate < size
            return lambda m: m.size_estimate > size
        # Unknown operator: Gmail treats it as plain text.
        return self._text(f"{op}:{v}")
