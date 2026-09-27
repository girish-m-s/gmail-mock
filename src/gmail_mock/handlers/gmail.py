"""users, messages, threads, drafts, labels and history."""

from __future__ import annotations

from .. import mime
from ..errors import bad_request, not_found
from ..util import compact, decode_page_token, encode_page_token, now_ms, paginate
from . import Ctx, handler

MAX_PAGE = 500


def _page_size(ctx: Ctx, default: int = 100, maximum: int = MAX_PAGE) -> int:
    size = ctx.q("maxResults", default)
    if size < 0:
        raise bad_request("Invalid maxResults")
    return min(size or default, maximum)


# --- users -------------------------------------------------------------------------


@handler("gmail.users.getProfile")
def get_profile(ctx: Ctx):
    box = ctx.mailbox
    return {
        "emailAddress": box.email,
        "messagesTotal": len(box.messages),
        "threadsTotal": len(box.threads),
        "historyId": str(box.history_id),
    }


@handler("gmail.users.watch")
def watch(ctx: Ctx):
    return ctx.mailbox.start_watch(ctx.data)


@handler("gmail.users.stop")
def stop(ctx: Ctx):
    ctx.mailbox.watch = None


@handler("gmail.users.history.list")
def list_history(ctx: Ctx):
    box = ctx.mailbox
    start = ctx.q("startHistoryId")
    if not start:
        raise bad_request("Missing startHistoryId", reason="required")
    if not str(start).isdigit():
        raise bad_request("Invalid startHistoryId")
    records = box.list_history(int(start), ctx.q("labelId"), ctx.q("historyTypes"))
    page, token = paginate(records, ctx.q("pageToken"), _page_size(ctx))
    return compact({"history": page, "nextPageToken": token, "historyId": str(box.history_id)})


# --- messages -----------------------------------------------------------------------


@handler("gmail.users.messages.list")
def list_messages(ctx: Ctx):
    offset, size = decode_page_token(ctx.q("pageToken")), _page_size(ctx)
    page, total = ctx.mailbox.search_page(ctx.q("q"), ctx.q("labelIds"), ctx.q("includeSpamTrash", False), offset, size)
    token = encode_page_token(offset + size) if offset + size < total else None
    return compact({"messages": [m.ref() for m in page], "nextPageToken": token, "resultSizeEstimate": total})


@handler("gmail.users.messages.get")
def get_message(ctx: Ctx):
    return ctx.mailbox.message(ctx.path["id"]).resource(ctx.q("format", "full"), ctx.q("metadataHeaders"))


@handler("gmail.users.messages.attachments.get")
def get_attachment(ctx: Ctx):
    msg = ctx.mailbox.message(ctx.path["messageId"])
    data = msg.attachments.get(ctx.path["id"])
    if data is None:
        raise bad_request("Invalid attachment token")
    from ..util import b64url

    return {"attachmentId": ctx.path["id"], "size": len(data), "data": b64url(data)}


@handler("gmail.users.messages.send")
def send_message(ctx: Ctx):
    raw = ctx.raw_from(ctx.data)
    return ctx.mailbox.send(raw, ctx.data.get("threadId")).labelled_ref()


def _internal_date(ctx: Ctx, raw: bytes, default_source: str) -> int:
    if ctx.q("internalDateSource", default_source) == "dateHeader":
        return mime.date_ms(mime.parse(raw)) or now_ms()
    return now_ms()


@handler("gmail.users.messages.insert")
def insert_message(ctx: Ctx):
    raw = ctx.raw_from(ctx.data)
    box = ctx.mailbox
    labels = ctx.data.get("labelIds") or []
    box.check_labels(labels)
    msg = box.insert(raw, labels, internal_date=_internal_date(ctx, raw, "receivedTime"), thread_id=ctx.data.get("threadId"))
    return msg.labelled_ref()


@handler("gmail.users.messages.import")
def import_message(ctx: Ctx):
    raw = ctx.raw_from(ctx.data)
    box = ctx.mailbox
    labels = ctx.data.get("labelIds")
    if labels is not None:
        box.check_labels(labels)
    msg = box.receive(raw, labels if labels is not None else [], internal_date=_internal_date(ctx, raw, "dateHeader"))
    return msg.labelled_ref()


@handler("gmail.users.messages.modify")
def modify_message(ctx: Ctx):
    box = ctx.mailbox
    msg = box.message(ctx.path["id"])
    return box.modify(msg, ctx.data.get("addLabelIds") or [], ctx.data.get("removeLabelIds") or []).labelled_ref()


def _batch_ids(ctx: Ctx) -> list[str]:
    ids = ctx.data.get("ids") or []
    if len(ids) > 1000:
        raise bad_request("Too many ids (max 1000)")
    return ids


@handler("gmail.users.messages.batchModify")
def batch_modify(ctx: Ctx):
    box = ctx.mailbox
    add, remove = ctx.data.get("addLabelIds") or [], ctx.data.get("removeLabelIds") or []
    box.check_labels(add + remove)
    for mid in _batch_ids(ctx):
        msg = box.messages.get(mid)
        if msg is not None:
            box.modify(msg, add, remove)


@handler("gmail.users.messages.batchDelete")
def batch_delete(ctx: Ctx):
    box = ctx.mailbox
    for mid in _batch_ids(ctx):
        msg = box.messages.get(mid)
        if msg is not None:
            box.delete_message(msg)


@handler("gmail.users.messages.delete")
def delete_message(ctx: Ctx):
    box = ctx.mailbox
    box.delete_message(box.message(ctx.path["id"]))


@handler("gmail.users.messages.trash")
def trash_message(ctx: Ctx):
    box = ctx.mailbox
    return box.trash(box.message(ctx.path["id"])).labelled_ref()


@handler("gmail.users.messages.untrash")
def untrash_message(ctx: Ctx):
    box = ctx.mailbox
    return box.untrash(box.message(ctx.path["id"])).labelled_ref()


# --- threads --------------------------------------------------------------------------


def _thread_summary(box, thread_id: str) -> dict:
    messages = box.thread(thread_id)
    return {"id": thread_id, "snippet": messages[-1].snippet, "historyId": str(max(m.history_id for m in messages))}


@handler("gmail.users.threads.list")
def list_threads(ctx: Ctx):
    box = ctx.mailbox
    matched = box.search(ctx.q("q"), ctx.q("labelIds"), ctx.q("includeSpamTrash", False))
    seen, thread_ids = set(), []
    for msg in matched:  # newest first
        if msg.thread_id not in seen:
            seen.add(msg.thread_id)
            thread_ids.append(msg.thread_id)
    page, token = paginate(thread_ids, ctx.q("pageToken"), _page_size(ctx))
    return compact({"threads": [_thread_summary(box, t) for t in page], "nextPageToken": token, "resultSizeEstimate": len(thread_ids)})


def _thread_resource(box, thread_id: str, fmt: str = "full", headers=None) -> dict:
    messages = box.thread(thread_id)
    return {
        "id": thread_id,
        "historyId": str(max(m.history_id for m in messages)),
        "messages": [m.resource(fmt, headers) for m in messages],
    }


@handler("gmail.users.threads.get")
def get_thread(ctx: Ctx):
    return _thread_resource(ctx.mailbox, ctx.path["id"], ctx.q("format", "full"), ctx.q("metadataHeaders"))


def _thread_mutation(ctx: Ctx, apply) -> dict:
    box = ctx.mailbox
    messages = box.thread(ctx.path["id"])
    for msg in messages:
        apply(box, msg)
    return {"id": ctx.path["id"], "messages": [m.labelled_ref() for m in messages]}


@handler("gmail.users.threads.modify")
def modify_thread(ctx: Ctx):
    add, remove = ctx.data.get("addLabelIds") or [], ctx.data.get("removeLabelIds") or []
    ctx.mailbox.check_labels(add + remove)
    return _thread_mutation(ctx, lambda box, m: box.modify(m, add, remove))


@handler("gmail.users.threads.trash")
def trash_thread(ctx: Ctx):
    return _thread_mutation(ctx, lambda box, m: box.trash(m))


@handler("gmail.users.threads.untrash")
def untrash_thread(ctx: Ctx):
    return _thread_mutation(ctx, lambda box, m: box.untrash(m))


@handler("gmail.users.threads.delete")
def delete_thread(ctx: Ctx):
    box = ctx.mailbox
    for msg in box.thread(ctx.path["id"]):
        box.delete_message(msg)


# --- drafts ---------------------------------------------------------------------------


def _draft(draft_id: str, msg, fmt: str | None = None) -> dict:
    return {"id": draft_id, "message": msg.resource(fmt) if fmt else msg.labelled_ref()}


@handler("gmail.users.drafts.list")
def list_drafts(ctx: Ctx):
    box = ctx.mailbox
    by_message = {mid: did for did, mid in box.drafts.items()}
    pool = [box.messages[mid] for mid in by_message]
    found = box.search(ctx.q("q"), None, ctx.q("includeSpamTrash", False), messages=pool)
    page, token = paginate(found, ctx.q("pageToken"), _page_size(ctx))
    return compact(
        {
            "drafts": [{"id": by_message[m.id], "message": m.ref()} for m in page],
            "nextPageToken": token,
            "resultSizeEstimate": len(found),
        }
    )


@handler("gmail.users.drafts.get")
def get_draft(ctx: Ctx):
    return _draft(ctx.path["id"], ctx.mailbox.draft_message(ctx.path["id"]), ctx.q("format", "full"))


@handler("gmail.users.drafts.create")
def create_draft(ctx: Ctx):
    message = ctx.data.get("message") or {}
    draft_id, msg = ctx.mailbox.create_draft(ctx.raw_from(message), message.get("threadId"))
    return _draft(draft_id, msg)


@handler("gmail.users.drafts.update")
def update_draft(ctx: Ctx):
    draft_id = ctx.path["id"]
    if ctx.data.get("id") and ctx.data["id"] != draft_id:
        raise bad_request("Draft id in body does not match the URL")
    message = ctx.data.get("message") or {}
    msg = ctx.mailbox.update_draft(draft_id, ctx.raw_from(message), message.get("threadId"))
    return _draft(draft_id, msg)


@handler("gmail.users.drafts.send")
def send_draft(ctx: Ctx):
    draft_id = ctx.data.get("id")
    if not draft_id:
        raise bad_request("Missing draft id", reason="required")
    if draft_id not in ctx.mailbox.drafts:
        raise not_found()
    message = ctx.data.get("message") or {}
    raw = ctx.raw_from(message) if (ctx.media is not None or message.get("raw")) else None
    return ctx.mailbox.send_draft(draft_id, raw, message.get("threadId")).labelled_ref()


@handler("gmail.users.drafts.delete")
def delete_draft(ctx: Ctx):
    box = ctx.mailbox
    box.delete_message(box.draft_message(ctx.path["id"]))


# --- labels ----------------------------------------------------------------------------


@handler("gmail.users.labels.list")
def list_labels(ctx: Ctx):
    box = ctx.mailbox
    return {"labels": [box.label_resource(lid, counts=False) for lid in box.labels]}


@handler("gmail.users.labels.get")
def get_label(ctx: Ctx):
    return ctx.mailbox.label_resource(ctx.path["id"])


@handler("gmail.users.labels.create")
def create_label(ctx: Ctx):
    return ctx.mailbox.create_label(ctx.data)


@handler("gmail.users.labels.update")
def update_label(ctx: Ctx):
    return ctx.mailbox.update_label(ctx.path["id"], ctx.data, patch=False)


@handler("gmail.users.labels.patch")
def patch_label(ctx: Ctx):
    return ctx.mailbox.update_label(ctx.path["id"], ctx.data, patch=True)


@handler("gmail.users.labels.delete")
def delete_label(ctx: Ctx):
    ctx.mailbox.delete_label(ctx.path["id"])
