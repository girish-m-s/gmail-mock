"""Load fixture data (users, labels, messages, drafts, contacts, filters) into a Store.

See ``examples/seed.json`` for the format.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path

from . import mime
from .handlers.people import create_contact, new_other_contact
from .store import RECEIVED_LABELS, Mailbox, Store
from .util import now_ms


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _date_ms(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10**11 else value * 1000)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)


def _attachments(items) -> list[tuple[str, str, bytes]]:
    out = []
    for a in items or []:
        data = base64.b64decode(a["data"]) if "data" in a else str(a.get("content", "")).encode()
        out.append((a["filename"], a.get("mimeType", "application/octet-stream"), data))
    return out


def resolve_labels(box: Mailbox, names) -> list[str]:
    ids = []
    for name in names:
        if name in box.labels:
            ids.append(name)
            continue
        match = next((lid for lid, label in box.labels.items() if label["name"].lower() == str(name).lower()), None)
        ids.append(match or box.create_label({"name": name})["id"])
    return ids


def compose_message(box: Mailbox, spec: dict, parent=None) -> bytes:
    if spec.get("raw"):
        return base64.urlsafe_b64decode(spec["raw"] + "=" * (-len(spec["raw"]) % 4))
    subject = spec.get("subject")
    in_reply_to = references = None
    if parent is not None:
        parent_subject = mime.header(parent.parsed, "Subject")
        subject = subject or (parent_subject if parent_subject.lower().startswith("re:") else f"Re: {parent_subject}")
        in_reply_to = mime.header(parent.parsed, "Message-ID")
        references = (mime.header(parent.parsed, "References") + " " + in_reply_to).strip()
    return mime.compose(
        sender=spec.get("from") or f"{box.display_name} <{box.email}>",
        to=_as_list(spec.get("to")) or ([box.email] if spec.get("from") else []),
        cc=_as_list(spec.get("cc")),
        bcc=_as_list(spec.get("bcc")),
        subject=subject or "",
        text=spec.get("text"),
        html_body=spec.get("html"),
        attachments=_attachments(spec.get("attachments")),
        in_reply_to=in_reply_to,
        references=references,
        date=_date_ms(spec.get("date")),
        extra_headers=spec.get("headers"),
    )


def load_user(store: Store, spec: dict) -> Mailbox:
    box = store.ensure_mailbox(spec["email"], spec.get("displayName"))
    if spec.get("displayName"):
        box.display_name = spec["displayName"]
        box.primary_send_as["displayName"] = spec["displayName"]
    for label in spec.get("labels", []):
        if not any(lbl["name"].lower() == label["name"].lower() for lbl in box.labels.values()):
            box.create_label(label)
    created = []
    for item in spec.get("messages", []):
        parent = created[item["replyTo"]] if isinstance(item.get("replyTo"), int) else None
        raw = compose_message(box, item, parent)
        labels = resolve_labels(box, item.get("labels", RECEIVED_LABELS if item.get("from") else ["SENT"]))
        date = _date_ms(item.get("date")) or mime.date_ms(mime.parse(raw)) or now_ms()
        created.append(box.insert(raw, labels, internal_date=date))
    for item in spec.get("drafts", []):
        parent = created[item["replyTo"]] if isinstance(item.get("replyTo"), int) else None
        box.create_draft(compose_message(box, item, parent), parent.thread_id if parent else None)
    for c in spec.get("contacts", []):
        person = (
            c
            if any(k in c for k in ("names", "emailAddresses"))
            else {
                "names": [
                    {
                        "displayName": c.get("name", ""),
                        "givenName": c.get("name", "").split(" ")[0],
                        "familyName": " ".join(c.get("name", "").split(" ")[1:]),
                    }
                ],
                "emailAddresses": [{"value": e} for e in _as_list(c.get("email"))],
                **({"phoneNumbers": [{"value": p} for p in _as_list(c.get("phone"))]} if c.get("phone") else {}),
                **({"organizations": [{"name": c["organization"]}]} if c.get("organization") else {}),
            }
        )
        create_contact(box, person)
    for c in spec.get("otherContacts", []):
        new_other_contact(box, c.get("name", ""), c["email"].lower())
    for f in spec.get("filters", []):
        action = dict(f.get("action", {}))
        for key in ("addLabelIds", "removeLabelIds"):
            if key in action:
                action[key] = resolve_labels(box, action[key])
        fid = store.ids.filter()
        box.filters[fid] = {"id": fid, "criteria": f.get("criteria", {}), "action": action}
    for key, value in (spec.get("settings") or {}).items():
        box.settings[key] = value
    # Seeding is setup, not activity: start the history log after it.
    box.history.clear()
    box.min_history_id = box.history_id
    return box


def load(store: Store, data: dict) -> list[Mailbox]:
    with store.lock:
        return [load_user(store, spec) for spec in data.get("users", [])]


def load_file(store: Store, path: str | Path) -> list[Mailbox]:
    return load(store, json.loads(Path(path).read_text()))
