"""Edge cases: encodings, malformed and huge messages, limits, concurrency."""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import httpx
import pytest
from conftest import ME

from gmail_mock import mime


def b64(data: bytes, urlsafe=True) -> str:
    return (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(data).decode()


def send_raw(http, data: bytes, path="/gmail/v1/users/me/messages/send", **body):
    return http.post(path, json={"raw": b64(data), **body})


def get(http, mid, fmt="full"):
    return http.get(f"/gmail/v1/users/me/messages/{mid}", params={"format": fmt}).json()


def header(msg, name):
    return next((h["value"] for h in msg["payload"]["headers"] if h["name"].lower() == name.lower()), None)


# --- encodings --------------------------------------------------------------------------


def test_rfc2047_headers_are_decoded_and_searchable(http):
    msg = EmailMessage()
    msg["From"] = "Zoë Ångström <zoe@example.org>"
    msg["To"] = ME
    msg["Subject"] = "Café ☕ – 会議のお知らせ"
    msg.set_content("Grüße aus München 🍺")
    m = http.post(f"/_mock/users/{ME}/messages", json={"raw": b64(msg.as_bytes())}).json()
    full = get(http, m["id"])
    assert header(full, "Subject") == "Café ☕ – 会議のお知らせ"
    assert "Zoë" in header(full, "From")
    assert full["snippet"] == "Grüße aus München 🍺"
    for q in ("会議", "subject:café", "münchen", "from:zoë"):
        assert http.get("/gmail/v1/users/me/messages", params={"q": q}).json()["resultSizeEstimate"] == 1, q


def test_lf_only_and_standard_base64_raw(http):
    raw = b"To: a@example.org\nSubject: LF only\n\nline one\nline two\n"
    sent = http.post("/gmail/v1/users/me/messages/send", json={"raw": b64(raw, urlsafe=False)})
    assert sent.status_code == 200
    assert header(get(http, sent.json()["id"]), "Subject") == "LF only"


def test_unpadded_base64url_raw(http):
    data = mime.compose(sender=ME, to=["a@example.org"], subject="nopad?>", text="x" * 7)
    resp = http.post("/gmail/v1/users/me/messages/send", json={"raw": b64(data).rstrip("=")})
    assert resp.status_code == 200


def test_invalid_base64_is_400(http):
    resp = http.post("/gmail/v1/users/me/messages/send", json={"raw": "!!!not base64!!!"})
    assert resp.status_code == 400 and "Base64" in resp.json()["error"]["message"]


def test_snippet_escapes_html_and_strips_markup(http):
    m = http.post(f"/_mock/users/{ME}/messages", json={"html": "<style>p{}</style><p>Tom &amp; Jerry's <b>show</b></p>"}).json()
    assert get(http, m["id"], "minimal")["snippet"] == "Tom &amp; Jerry&#39;s show"


# --- malformed and unusual MIME -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\xff\xfe garbage \x80\x81",
        b"no headers at all, just text",
        b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n--x\r\nbroken',
        b"Subject: =?utf-8?b?!!!invalid!!!?=\r\nContent-Type: text/plain; charset=unknown-8bit\r\n\r\n\xe9\xe8",
        b"Content-Type: multipart/alternative\r\n\r\nmissing boundary",
    ],
)
def test_malformed_messages_never_500(http, raw):
    resp = http.post("/gmail/v1/users/me/messages", json={"raw": b64(raw) or "", "labelIds": ["INBOX"]})
    assert resp.status_code in (200, 400), resp.text
    if resp.status_code == 200:
        mid = resp.json()["id"]
        for fmt in ("full", "metadata", "minimal", "raw"):
            assert http.get(f"/gmail/v1/users/me/messages/{mid}", params={"format": fmt}).status_code == 200
        assert http.get("/gmail/v1/users/me/messages", params={"q": "anything"}).status_code == 200


def test_nested_multipart_and_forwarded_message(http):
    inner = mime.compose(sender="orig@example.org", to=[ME], subject="Original", text="original body")
    outer = EmailMessage()
    outer["To"] = ME
    outer["Subject"] = "Fwd: Original"
    outer.set_content("see below")
    outer.add_alternative("<p>see below</p>", subtype="html")
    outer.add_attachment(mime.parse(inner), filename="original.eml")
    outer.add_attachment(b"\x89PNG....", maintype="image", subtype="png", filename="pic.png")
    m = http.post(f"/_mock/users/{ME}/messages", json={"raw": b64(outer.as_bytes())}).json()
    full = get(http, m["id"])
    parts = full["payload"]["parts"]
    assert [p["mimeType"] for p in parts] == ["multipart/alternative", "message/rfc822", "image/png"]
    eml = parts[1]
    att = http.get(f"/gmail/v1/users/me/messages/{m['id']}/attachments/{eml['body']['attachmentId']}").json()
    assert b"Subject: Original" in base64.urlsafe_b64decode(att["data"])
    assert http.get("/gmail/v1/users/me/messages", params={"q": "filename:png"}).json()["resultSizeEstimate"] == 1


def test_large_attachment_round_trip(http):
    blob = bytes(range(256)) * (10 * 1024 * 1024 // 256)  # 10 MiB
    data = mime.compose(
        sender=ME, to=["big@example.org"], subject="big", text="see file", attachments=[("big.bin", "application/octet-stream", blob)]
    )
    sent = send_raw(http, data)
    assert sent.status_code == 200
    full = get(http, sent.json()["id"])
    part = full["payload"]["parts"][1]
    assert part["body"]["size"] == len(blob)
    assert full["sizeEstimate"] > len(blob)
    att = http.get(f"/gmail/v1/users/me/messages/{sent.json()['id']}/attachments/{part['body']['attachmentId']}").json()
    assert base64.urlsafe_b64decode(att["data"]) == blob
    assert http.get("/gmail/v1/users/me/messages", params={"q": "larger:5M"}).json()["resultSizeEstimate"] == 1


# --- limits and parameters ---------------------------------------------------------------


def test_max_results_bounds_and_page_tokens(http):
    for i in range(12):
        http.post(f"/_mock/users/{ME}/messages", json={"subject": f"m{i}"})
    lst = lambda **p: http.get("/gmail/v1/users/me/messages", params=p)  # noqa: E731
    assert len(lst(maxResults=0).json()["messages"]) == 12  # 0 means default
    assert len(lst(maxResults=100000).json()["messages"]) == 12  # capped, not rejected
    assert lst(maxResults=-1).status_code == 400
    assert lst(pageToken="garbage!").status_code == 400
    seen, token = [], None
    while True:
        body = lst(maxResults=5, **({"pageToken": token} if token else {})).json()
        seen += [m["id"] for m in body["messages"]]
        token = body.get("nextPageToken")
        if not token:
            break
    assert len(seen) == len(set(seen)) == 12


def test_batch_ids_limit(http):
    resp = http.post("/gmail/v1/users/me/messages/batchModify", json={"ids": ["a"] * 1001, "addLabelIds": ["STARRED"]})
    assert resp.status_code == 400


def test_fields_syntax_errors(http):
    assert http.get("/gmail/v1/users/me/profile", params={"fields": "emailAddress("}).status_code == 400
    ok = http.get("/gmail/v1/users/me/labels", params={"fields": "labels/id"}).json()
    assert set(ok["labels"][0]) == {"id"}


def test_batch_limits_and_bad_parts(http):
    def batch(parts):
        body = "".join(f"--B\r\nContent-Type: application/http\r\nContent-ID: <{i}>\r\n\r\n{p}\r\n" for i, p in enumerate(parts)) + "--B--"
        return http.post("/batch/gmail/v1", content=body.encode(), headers={"Content-Type": "multipart/mixed; boundary=B"})

    too_many = batch(["GET /gmail/v1/users/me/profile HTTP/1.1\r\n"] * 101)
    assert too_many.status_code == 400
    mixed = batch(["GET /gmail/v1/users/me/profile HTTP/1.1\r\n", "GET /nope HTTP/1.1\r\n"])
    assert mixed.status_code == 200
    assert "HTTP/1.1 200" in mixed.text and "HTTP/1.1 404" in mixed.text


def test_deep_thread(http):
    first = http.post(f"/_mock/users/{ME}/messages", json={"subject": "Long thread"}).json()
    last = first
    for _ in range(60):
        last = http.post(f"/_mock/users/{ME}/messages", json={"subject": "Re: Long thread", "inReplyTo": last["id"]}).json()
        assert last["threadId"] == first["threadId"]
    thread = http.get(f"/gmail/v1/users/me/threads/{first['threadId']}").json()
    assert len(thread["messages"]) == 61
    assert http.get("/gmail/v1/users/me/threads").json()["resultSizeEstimate"] == 1


def test_expired_watch_does_not_publish(http, store):
    http.post("/gmail/v1/users/me/watch", json={"topicName": "projects/p/topics/t"})
    store.mailboxes[ME].watch["expiration"] = 0
    http.post(f"/_mock/users/{ME}/messages", json={"subject": "late"})
    assert http.get("/_mock/pubsub/published").json()["published"] == []


# --- concurrency -------------------------------------------------------------------------


def test_concurrent_mixed_writes_stay_consistent(base_url, store):
    workers, per_worker = 16, 30
    errors: list[str] = []

    def work(n: int) -> list[str]:
        ids = []
        with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {ME}"}) as c:
            for i in range(per_worker):
                raw = b64(mime.compose(sender=ME, to=["peer@example.org"], subject=f"w{n}-{i}", text="x"))
                r = c.post("/gmail/v1/users/me/messages/send", json={"raw": raw})
                if r.status_code != 200:
                    errors.append(r.text)
                    continue
                mid = r.json()["id"]
                ids.append(mid)
                r = c.post(f"/gmail/v1/users/me/messages/{mid}/modify", json={"addLabelIds": ["STARRED"]})
                if r.status_code != 200:
                    errors.append(r.text)
                c.get("/gmail/v1/users/me/messages", params={"q": "is:starred", "maxResults": 5})
        return ids

    with ThreadPoolExecutor(workers) as pool:
        all_ids = [mid for ids in pool.map(work, range(workers)) for mid in ids]

    assert errors == []
    assert len(all_ids) == len(set(all_ids)) == workers * per_worker
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {ME}"}) as c:
        profile = c.get("/gmail/v1/users/me/profile").json()
        assert profile["messagesTotal"] == workers * per_worker
        starred = c.get("/gmail/v1/users/me/labels/STARRED").json()
        assert starred["messagesTotal"] == workers * per_worker
        history = c.get("/gmail/v1/users/me/history", params={"startHistoryId": "100000", "maxResults": 500})
        ids, token = [], None
        while True:
            params = {"startHistoryId": "100000", "maxResults": 500, **({"pageToken": token} if token else {})}
            body = c.get("/gmail/v1/users/me/history", params=params).json()
            ids += [int(h["id"]) for h in body.get("history", [])]
            token = body.get("nextPageToken")
            if not token:
                break
        assert history.status_code == 200
        assert ids == sorted(set(ids)) and len(ids) == 2 * workers * per_worker


def test_concurrent_notifications_all_delivered(http, base_url, push_receiver):
    url, received = push_receiver
    http.post(
        "/_mock/pubsub/subscriptions", json={"name": "projects/p/subscriptions/s", "topic": "projects/p/topics/t", "pushEndpoint": url}
    )
    http.post("/gmail/v1/users/me/watch", json={"topicName": "projects/p/topics/t"})
    count = 200
    local = threading.local()

    def deliver(i):
        if not hasattr(local, "client"):
            local.client = httpx.Client(base_url=base_url)
        assert local.client.post(f"/_mock/users/{ME}/messages", json={"subject": f"n{i}"}).status_code == 201

    with ThreadPoolExecutor(10) as pool:
        list(pool.map(deliver, range(count)))
    deliveries = http.get("/_mock/pubsub/deliveries", timeout=30).json()["deliveries"]
    assert len(deliveries) == count and all(d["status"] == 204 for d in deliveries)
    history_ids = [json.loads(base64.b64decode(r["message"]["data"]))["historyId"] for r in received]
    assert len(history_ids) == len(set(history_ids)) == count
