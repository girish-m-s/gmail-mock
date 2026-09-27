"""Stateful in-memory mailboxes.

Each mailbox keeps messages, threads, labels, drafts, a history log, filters and
settings. Every mutation appends a history record and, when the mailbox is
watched, publishes a Gmail push notification to the configured Pub/Sub topic.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import deque
from email.utils import formataddr, formatdate, make_msgid
from functools import cached_property

from . import mime
from .errors import ApiError, bad_request, not_found
from .pubsub import PubSub
from .query import Query
from .util import IdGenerator, now_ms

# Order matches what users.labels.list returns for a fresh Gmail account.
SYSTEM_LABELS: dict[str, dict] = {
    "CHAT": {"messageListVisibility": "hide", "labelListVisibility": "labelHide"},
    "SENT": {"messageListVisibility": "hide", "labelListVisibility": "labelShow"},
    "INBOX": {"messageListVisibility": "hide", "labelListVisibility": "labelShow"},
    "IMPORTANT": {"messageListVisibility": "hide", "labelListVisibility": "labelShow"},
    "TRASH": {"messageListVisibility": "hide", "labelListVisibility": "labelHide"},
    "DRAFT": {"messageListVisibility": "hide", "labelListVisibility": "labelShow"},
    "SPAM": {"messageListVisibility": "hide", "labelListVisibility": "labelHide"},
    "CATEGORY_FORUMS": {},
    "CATEGORY_UPDATES": {},
    "CATEGORY_PERSONAL": {},
    "CATEGORY_PROMOTIONS": {},
    "CATEGORY_SOCIAL": {},
    "STARRED": {"messageListVisibility": "hide", "labelListVisibility": "labelShow"},
    "UNREAD": {},
}
RESERVED_LABEL_NAMES = {n.lower() for n in SYSTEM_LABELS} | {
    "inbox",
    "sent",
    "sent mail",
    "drafts",
    "draft",
    "spam",
    "trash",
    "bin",
    "starred",
    "important",
    "chats",
    "all mail",
    "scheduled",
    "snoozed",
}
RECEIVED_LABELS = ["INBOX", "UNREAD", "CATEGORY_PERSONAL"]
HISTORY_TYPES = {
    "messageAdded": "messagesAdded",
    "messageDeleted": "messagesDeleted",
    "labelAdded": "labelsAdded",
    "labelRemoved": "labelsRemoved",
}
WATCH_TTL_MS = 7 * 24 * 3600 * 1000
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _dedupe(items) -> list:
    return list(dict.fromkeys(items))


class Message:
    def __init__(self, id: str, thread_id: str, raw: bytes, label_ids: list[str], internal_date: int) -> None:
        self.id = id
        self.thread_id = thread_id
        self.raw = raw
        self.label_ids = _dedupe(label_ids)
        self.internal_date = internal_date
        self.history_id = 0
        self.labels_before_trash: list[str] | None = None

    @property
    def size_estimate(self) -> int:
        return len(self.raw)

    @cached_property
    def parsed(self):
        return mime.parse(self.raw)

    @cached_property
    def _payload(self) -> tuple[dict, dict[str, bytes]]:
        return mime.build_payload(self.parsed, self.id)

    @property
    def attachments(self) -> dict[str, bytes]:
        return self._payload[1]

    @cached_property
    def snippet(self) -> str:
        return mime.snippet(self.parsed)

    @cached_property
    def rfc822_id(self) -> str:
        return mime.header(self.parsed, "Message-ID").strip().strip("<>").lower()

    def search_fields(self) -> dict:
        return self._search_fields

    @cached_property
    def _search_fields(self) -> dict:
        p = self.parsed
        fields = {
            "from": mime.header(p, "From").lower(),
            "to": mime.header(p, "To").lower(),
            "cc": mime.header(p, "Cc").lower(),
            "bcc": mime.header(p, "Bcc").lower(),
            "deliveredto": (mime.header(p, "Delivered-To") + " " + mime.header(p, "To")).lower(),
            "subject": mime.header(p, "Subject").lower(),
            "list": (mime.header(p, "List-Id") + " " + mime.header(p, "List-Post")).lower(),
            "msgid": self.rfc822_id,
            "filenames": mime.filenames(p),
            "body": mime.text_content(p).lower(),
        }
        fields["has_attachment"] = bool(self.attachments)
        fields["all"] = " ".join(
            [fields["subject"], fields["body"], fields["from"], fields["to"], fields["cc"], " ".join(fields["filenames"]).lower()]
        )
        return fields

    def ref(self) -> dict:
        return {"id": self.id, "threadId": self.thread_id}

    def labelled_ref(self) -> dict:
        return {"id": self.id, "threadId": self.thread_id, "labelIds": list(self.label_ids)}

    def resource(self, fmt: str = "full", metadata_headers: list[str] | None = None) -> dict:
        out: dict = {"id": self.id, "threadId": self.thread_id}
        if self.label_ids:
            out["labelIds"] = list(self.label_ids)
        out["snippet"] = self.snippet
        if fmt == "raw":
            from .util import b64url

            out["raw"] = b64url(self.raw)
        elif fmt == "full":
            out["payload"] = self._payload[0]
        elif fmt == "metadata":
            full = self._payload[0]
            wanted = {h.lower() for h in metadata_headers or []}
            headers = [h for h in full["headers"] if not wanted or h["name"].lower() in wanted]
            out["payload"] = {
                "partId": "",
                "mimeType": full["mimeType"],
                "filename": full["filename"],
                "headers": headers,
                "body": {"size": 0},
            }
        out["sizeEstimate"] = self.size_estimate
        out["historyId"] = str(self.history_id)
        out["internalDate"] = str(self.internal_date)
        return out


class Mailbox:
    def __init__(self, store: Store, email: str, display_name: str | None = None) -> None:
        self.store = store
        self.email = email.lower()
        self.display_name = display_name or email.split("@")[0].replace(".", " ").title()
        self.history_id = 100_000
        self.min_history_id = self.history_id
        self.messages: dict[str, Message] = {}
        self.threads: dict[str, list[str]] = {}
        self._by_rfc822: dict[str, str] = {}
        self.labels: dict[str, dict] = {lid: {"id": lid, "name": lid, "type": "system", **extra} for lid, extra in SYSTEM_LABELS.items()}
        self.drafts: dict[str, str] = {}
        self.history: list[dict] = []
        self.watch: dict | None = None
        self.filters: dict[str, dict] = {}
        self.settings: dict[str, dict] = {
            "vacation": {"enableAutoReply": False},
            "pop": {"accessWindow": "disabled", "disposition": "leaveInInbox"},
            "imap": {"enabled": True, "autoExpunge": True, "expungeBehavior": "archive", "maxFolderSize": 0},
            "language": {"displayLanguage": "en"},
            "autoForwarding": {"enabled": False},
        }
        self.send_as: dict[str, dict] = {
            self.email: {
                "sendAsEmail": self.email,
                "displayName": self.display_name,
                "replyToAddress": "",
                "signature": "",
                "isPrimary": True,
                "isDefault": True,
            }
        }
        self.smime: dict[str, dict[str, dict]] = {}
        self.forwarding: dict[str, dict] = {}
        self.delegates: dict[str, dict] = {}
        self.cse_identities: dict[str, dict] = {}
        self.cse_keypairs: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.other_contacts: dict[str, dict] = {}
        self.contact_groups: dict[str, dict] = {}
        self.profile_resource = f"people/{int(hashlib.sha256(self.email.encode()).hexdigest(), 16) % 10**20:020d}"

    # --- lookups ---------------------------------------------------------------

    def message(self, message_id: str) -> Message:
        if not _HEX.match(message_id or ""):
            raise bad_request("Invalid id value")
        msg = self.messages.get(message_id)
        if msg is None:
            raise not_found()
        return msg

    def thread(self, thread_id: str) -> list[Message]:
        if not _HEX.match(thread_id or ""):
            raise bad_request("Invalid id value")
        ids = self.threads.get(thread_id)
        if not ids:
            raise not_found()
        return sorted((self.messages[i] for i in ids), key=lambda m: (m.internal_date, m.id))

    def draft_message(self, draft_id: str) -> Message:
        mid = self.drafts.get(draft_id)
        if mid is None:
            raise not_found()
        return self.messages[mid]

    def draft_id_for(self, message_id: str) -> str | None:
        return next((d for d, m in self.drafts.items() if m == message_id), None)

    @property
    def primary_send_as(self) -> dict:
        return next(s for s in self.send_as.values() if s.get("isPrimary"))

    # --- history & push -------------------------------------------------------------

    def _record(self, msg: Message, kind: str, labels: list[str] | None = None) -> None:
        self.history_id += 1
        hid = self.history_id
        ref = msg.ref()
        record: dict = {"id": str(hid), "messages": [ref]}
        if kind == "messagesDeleted":
            record[kind] = [{"message": ref}]
        elif kind == "messagesAdded":
            record[kind] = [{"message": msg.labelled_ref()}]
        else:
            record[kind] = [{"message": msg.labelled_ref(), "labelIds": list(labels or [])}]
        msg.history_id = hid
        touched = set(msg.label_ids) | set(labels or [])
        self.history.append({"id": hid, "type": kind, "labels": touched, "record": record})
        self._notify(touched, hid)

    def _notify(self, labels: set[str], history_id: int) -> None:
        watch = self.watch
        if not watch or watch["expiration"] < now_ms():
            return
        filter_ids = set(watch.get("labelIds") or [])
        if filter_ids:
            behavior = watch.get("labelFilterBehavior") or watch.get("labelFilterAction") or "include"
            if (behavior == "include") != bool(filter_ids & labels):
                return
        payload = json.dumps({"emailAddress": self.email, "historyId": history_id}).encode()
        self.store.pubsub.publish(watch["topicName"], payload)

    def start_watch(self, body: dict) -> dict:
        topic = body.get("topicName") or ""
        if not re.fullmatch(r"projects/[^/]+/topics/[^/]+", topic):
            raise bad_request(f"Invalid topicName does not match projects/*/topics/*: {topic!r}")
        self.store.pubsub.create_topic(topic)
        self.check_labels(body.get("labelIds") or [])
        expiration = now_ms() + WATCH_TTL_MS
        self.watch = {**body, "expiration": expiration}
        return {"historyId": str(self.history_id), "expiration": str(expiration)}

    def list_history(self, start: int, label_id: str | None, types: list[str] | None) -> list[dict]:
        if start < self.min_history_id:
            raise not_found()
        kinds = {HISTORY_TYPES[t] for t in types} if types else None
        out = []
        for entry in self.history:
            if entry["id"] <= start:
                continue
            if kinds and entry["type"] not in kinds:
                continue
            if label_id and label_id not in entry["labels"]:
                continue
            out.append(entry["record"])
        return out

    # --- labels ---------------------------------------------------------------

    def check_labels(self, label_ids, *, allow_draft: bool = False) -> None:
        for lid in label_ids:
            if lid not in self.labels or (lid == "DRAFT" and not allow_draft):
                raise bad_request(f"Invalid label: {lid}")

    def label_resource(self, label_id: str, counts: bool = True) -> dict:
        label = self.labels.get(label_id)
        if label is None:
            raise not_found()
        out = dict(label)
        if counts:
            msgs = [m for m in self.messages.values() if label_id in m.label_ids]
            unread = [m for m in msgs if "UNREAD" in m.label_ids]
            out["messagesTotal"] = len(msgs)
            out["messagesUnread"] = len(unread)
            out["threadsTotal"] = len({m.thread_id for m in msgs})
            out["threadsUnread"] = len({m.thread_id for m in unread})
        return out

    def _validate_label_body(self, body: dict, current_id: str | None = None) -> None:
        name = body.get("name")
        if name is not None:
            name = name.strip()
            if not name or name.lower() in RESERVED_LABEL_NAMES:
                raise bad_request("Invalid label name")
            for lid, label in self.labels.items():
                if lid != current_id and label["name"].lower() == name.lower():
                    raise ApiError(409, "Label name exists or conflicts", reason="duplicate")
        color = body.get("color")
        if color is not None:
            for key in ("textColor", "backgroundColor"):
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color.get(key) or ""):
                    raise bad_request(f"Invalid label color: {key}")

    def create_label(self, body: dict) -> dict:
        if not (body.get("name") or "").strip():
            raise bad_request("Invalid label name")
        self._validate_label_body(body)
        lid = self.store.ids.label()
        label = {
            "id": lid,
            "name": body["name"].strip(),
            "type": "user",
            "messageListVisibility": body.get("messageListVisibility", "show"),
            "labelListVisibility": body.get("labelListVisibility", "labelShow"),
        }
        if body.get("color"):
            label["color"] = body["color"]
        self.labels[lid] = label
        return self.label_resource(lid, counts=False)

    def update_label(self, label_id: str, body: dict, *, patch: bool) -> dict:
        label = self.labels.get(label_id)
        if label is None:
            raise not_found()
        if label["type"] == "system":
            raise bad_request(f"Invalid label: {label_id} (system labels cannot be modified)")
        if body.get("id") and body["id"] != label_id:
            raise bad_request("Label id in body does not match the URL")
        if not patch and not (body.get("name") or "").strip():
            raise bad_request("Invalid label name")
        self._validate_label_body(body, current_id=label_id)
        for key in ("name", "messageListVisibility", "labelListVisibility", "color"):
            if key in body:
                label[key] = body[key].strip() if key == "name" else body[key]
            elif not patch and key == "color":
                label.pop("color", None)
        return self.label_resource(label_id, counts=False)

    def delete_label(self, label_id: str) -> None:
        label = self.labels.get(label_id)
        if label is None:
            raise not_found()
        if label["type"] == "system":
            raise bad_request("Invalid delete request")
        for msg in list(self.messages.values()):
            if label_id in msg.label_ids:
                msg.label_ids.remove(label_id)
                self._record(msg, "labelsRemoved", [label_id])
        del self.labels[label_id]

    # --- messages ----------------------------------------------------------------

    def _thread_for(self, parsed, thread_id: str | None) -> str | None:
        """Pick an existing thread the way Gmail does: explicit threadId + matching subject,
        or References/In-Reply-To pointing at a message with the same subject."""
        subject = mime.normalize_subject(mime.header(parsed, "Subject"))
        if thread_id and thread_id in self.threads:
            first = self.thread(thread_id)[0]
            if mime.normalize_subject(mime.header(first.parsed, "Subject")) == subject:
                return thread_id
            return None
        refs = re.findall(r"<([^>]+)>", mime.header(parsed, "In-Reply-To") + " " + mime.header(parsed, "References"))
        for ref in reversed(refs):
            mid = self._by_rfc822.get(ref.lower())
            if mid and mid in self.messages:
                candidate = self.messages[mid]
                if mime.normalize_subject(mime.header(candidate.parsed, "Subject")) == subject:
                    return candidate.thread_id
        return None

    def insert(
        self,
        raw: bytes,
        label_ids: list[str],
        *,
        internal_date: int | None = None,
        thread_id: str | None = None,
        thread_must_exist: bool = True,
    ) -> Message:
        if thread_id and thread_must_exist and thread_id not in self.threads:
            raise not_found()
        parsed = mime.parse(raw)
        resolved = self._thread_for(parsed, thread_id)
        mid = self.store.ids.message()
        if resolved is None:
            resolved = thread_id if (thread_id and thread_id not in self.threads) else mid
        msg = Message(mid, resolved, raw, label_ids, internal_date or now_ms())
        self.messages[mid] = msg
        self.threads.setdefault(resolved, []).append(mid)
        if msg.rfc822_id:
            self._by_rfc822[msg.rfc822_id] = mid
        self._record(msg, "messagesAdded")
        return msg

    def delete_message(self, msg: Message) -> None:
        self.messages.pop(msg.id, None)
        ids = self.threads.get(msg.thread_id, [])
        if msg.id in ids:
            ids.remove(msg.id)
        if not ids:
            self.threads.pop(msg.thread_id, None)
        draft = self.draft_id_for(msg.id)
        if draft:
            del self.drafts[draft]
        self._record(msg, "messagesDeleted")

    def modify(self, msg: Message, add: list[str], remove: list[str]) -> Message:
        self.check_labels(list(add) + list(remove))
        added = [lid for lid in _dedupe(add) if lid not in msg.label_ids]
        removed = [lid for lid in _dedupe(remove) if lid in msg.label_ids and lid not in add]
        if added:
            msg.label_ids.extend(added)
            self._record(msg, "labelsAdded", added)
        if removed:
            msg.label_ids = [lid for lid in msg.label_ids if lid not in removed]
            self._record(msg, "labelsRemoved", removed)
        return msg

    def trash(self, msg: Message) -> Message:
        if "TRASH" in msg.label_ids:
            return msg
        msg.labels_before_trash = list(msg.label_ids)
        return self.modify(msg, ["TRASH"], [lid for lid in ("INBOX", "SPAM") if lid in msg.label_ids])

    def untrash(self, msg: Message) -> Message:
        if "TRASH" not in msg.label_ids:
            return msg
        restore = [lid for lid in ("INBOX",) if msg.labels_before_trash and lid in msg.labels_before_trash]
        msg.labels_before_trash = None
        return self.modify(msg, restore, ["TRASH"])

    def search(self, q: str | None, label_ids: list[str] | None, include_spam_trash: bool, messages=None) -> list[Message]:
        query = Query(q, self)
        include = include_spam_trash or query.includes_spam_trash or bool({"SPAM", "TRASH"} & set(label_ids or []))
        pool = self.messages.values() if messages is None else messages
        out = []
        for msg in pool:
            if not include and {"SPAM", "TRASH"} & set(msg.label_ids):
                continue
            if label_ids and not set(label_ids) <= set(msg.label_ids):
                continue
            if query.matches(msg):
                out.append(msg)
        out.sort(key=lambda m: (m.internal_date, m.id), reverse=True)
        return out

    # --- sending & receiving -------------------------------------------------------

    def _prepare_outgoing(self, raw: bytes) -> tuple[bytes, list[tuple[str, str]]]:
        parsed = mime.parse(raw)
        changed = False
        allowed = {e for e, s in self.send_as.items() if s.get("isPrimary") or s.get("verificationStatus") == "accepted"}
        from_addrs = mime.addresses(parsed, "From")
        if not from_addrs or from_addrs[0][1] not in allowed:
            default = next((s for s in self.send_as.values() if s.get("isDefault")), self.primary_send_as)
            del parsed["From"]
            parsed["From"] = formataddr((default.get("displayName") or "", default["sendAsEmail"]))
            changed = True
        if not parsed.get("Date"):
            parsed["Date"] = formatdate(usegmt=True)
            changed = True
        if not parsed.get("Message-ID"):
            parsed["Message-ID"] = make_msgid(domain="mail.gmail.com")
            changed = True
        recipients = mime.addresses(parsed, "To", "Cc", "Bcc")
        if not recipients:
            raise bad_request("Recipient address required")
        return (mime.serialize(parsed) if changed else raw), recipients

    def send(self, raw: bytes, thread_id: str | None = None, *, thread_must_exist: bool = True) -> Message:
        raw, recipients = self._prepare_outgoing(raw)
        emails = _dedupe(addr for _, addr in recipients)
        labels = ["SENT"] + (["INBOX"] if self.email in emails else [])
        msg = self.insert(raw, labels, thread_id=thread_id, thread_must_exist=thread_must_exist)
        delivered = mime.parse(raw)
        del delivered["Bcc"]
        delivered_raw = mime.serialize(delivered)
        for addr in emails:
            box = self.store.mailboxes.get(addr)
            if box is not None and box is not self:
                box.receive(delivered_raw)
        self.remember_correspondents(recipients)
        return msg

    def receive(
        self, raw: bytes, label_ids: list[str] | None = None, internal_date: int | None = None, apply_filters: bool = True
    ) -> Message:
        labels = list(label_ids) if label_ids is not None else list(RECEIVED_LABELS)
        if apply_filters and self.filters:
            labels = self._apply_filters(raw, labels)
        self.check_labels(labels)
        return self.insert(raw, labels, internal_date=internal_date)

    def _apply_filters(self, raw: bytes, labels: list[str]) -> list[str]:
        probe = Message("0", "0", raw, labels, now_ms())
        for f in self.filters.values():
            if Query(filter_query(f.get("criteria", {})), self).matches(probe):
                action = f.get("action", {})
                labels = [lid for lid in labels if lid not in action.get("removeLabelIds", [])]
                labels = _dedupe(labels + [lid for lid in action.get("addLabelIds", []) if lid in self.labels])
                probe.label_ids = labels
        return labels

    # --- drafts ----------------------------------------------------------------

    def create_draft(self, raw: bytes, thread_id: str | None = None) -> tuple[str, Message]:
        msg = self.insert(raw, ["DRAFT"], thread_id=thread_id)
        draft_id = self.store.ids.draft()
        self.drafts[draft_id] = msg.id
        return draft_id, msg

    def update_draft(self, draft_id: str, raw: bytes, thread_id: str | None = None) -> Message:
        old = self.draft_message(draft_id)
        keep_thread = thread_id or old.thread_id
        self.delete_message(old)
        msg = self.insert(raw, ["DRAFT"], thread_id=keep_thread, thread_must_exist=False)
        self.drafts[draft_id] = msg.id
        return msg

    def send_draft(self, draft_id: str, raw: bytes | None = None, thread_id: str | None = None) -> Message:
        old = self.draft_message(draft_id)
        raw = raw or old.raw
        keep_thread = thread_id or old.thread_id
        self._prepare_outgoing(raw)  # validate before destroying the draft
        self.delete_message(old)
        return self.send(raw, keep_thread, thread_must_exist=False)

    # --- people ------------------------------------------------------------------

    def remember_correspondents(self, recipients: list[tuple[str, str]]) -> None:
        known = {
            e["value"].lower()
            for book in (self.contacts, self.other_contacts)
            for person in book.values()
            for e in person.get("emailAddresses", [])
        }
        for name, addr in recipients:
            if addr in known or addr == self.email:
                continue
            known.add(addr)
            from .handlers.people import new_other_contact

            new_other_contact(self, name, addr)


def filter_query(criteria: dict) -> str:
    parts = []
    for key in ("from", "to", "subject"):
        if criteria.get(key):
            parts.append(f"{key}:({criteria[key]})")
    if criteria.get("query"):
        parts.append(f"({criteria['query']})")
    if criteria.get("negatedQuery"):
        parts.append(f"-({criteria['negatedQuery']})")
    if criteria.get("hasAttachment"):
        parts.append("has:attachment")
    if criteria.get("size"):
        op = "smaller" if criteria.get("sizeComparison") == "smaller" else "larger"
        parts.append(f"{op}:{criteria['size']}")
    return " ".join(parts)


class Store:
    def __init__(
        self,
        default_email: str = "me@example.com",
        default_name: str | None = "Mock User",
        pubsub: PubSub | None = None,
        request_log_size: int = 500,
    ) -> None:
        self.lock = threading.RLock()
        self.ids = IdGenerator()
        self.pubsub = pubsub or PubSub()
        self.default_email = default_email.lower()
        self.default_name = default_name
        self.mailboxes: dict[str, Mailbox] = {}
        self.faults: list[dict] = []
        self.requests: deque = deque(maxlen=request_log_size)
        self.ensure_mailbox(self.default_email, default_name)

    def ensure_mailbox(self, email: str, display_name: str | None = None) -> Mailbox:
        email = email.lower()
        with self.lock:
            box = self.mailboxes.get(email)
            if box is None:
                box = Mailbox(self, email, display_name)
                self.mailboxes[email] = box
            return box

    def reset(self) -> None:
        with self.lock:
            self.mailboxes.clear()
            self.faults.clear()
            self.requests.clear()
            self.pubsub.reset()
            self.ensure_mailbox(self.default_email, self.default_name)

    def resolve_user(self, user_id: str, auth_email: str) -> Mailbox:
        email = auth_email if user_id in ("me", "", None) else user_id.lower()
        if email == auth_email:
            return self.ensure_mailbox(email)
        box = self.mailboxes.get(email)
        if box is None:
            raise ApiError(403, f"Delegation denied for {auth_email}", reason="forbidden")
        return box
