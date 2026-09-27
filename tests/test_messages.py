import base64
import io

import pytest
from conftest import ME, raw_message
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload


def receive(http, **kw):
    kw.setdefault("from", "Alice <alice@example.org>")
    resp = http.post(f"/_mock/users/{ME}/messages", json=kw)
    assert resp.status_code == 201, resp.text
    return resp.json()


def ids(result):
    return [m["id"] for m in result.get("messages", [])]


def test_send_returns_sent_ref_and_sets_from(gmail):
    sent = gmail.users().messages().send(userId="me", body={"raw": raw_message(subject="Hi")}).execute()
    assert set(sent) == {"id", "threadId", "labelIds"}
    assert sent["labelIds"] == ["SENT"]
    msg = gmail.users().messages().get(userId="me", id=sent["id"], format="metadata", metadataHeaders=["From"]).execute()
    assert msg["payload"]["headers"] == [{"name": "From", "value": "Mock User <me@example.com>"}]


def test_send_requires_raw_and_recipient(gmail):
    with pytest.raises(HttpError) as err:
        gmail.users().messages().send(userId="me", body={}).execute()
    assert err.value.resp.status == 400
    with pytest.raises(HttpError) as err:
        gmail.users().messages().send(userId="me", body={"raw": raw_message(to="")}).execute()
    assert "Recipient address required" in str(err.value)


def test_send_delivers_to_other_local_mailbox_without_bcc(store, gmail, gmail_for):
    store.ensure_mailbox("bob@example.com", "Bob")
    raw = raw_message(to="bob@example.com", subject="Lunch?", Bcc="secret@example.com")
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    bob = gmail_for("bob@example.com")
    listed = bob.users().messages().list(userId="me").execute()
    assert listed["resultSizeEstimate"] == 1
    msg = bob.users().messages().get(userId="me", id=listed["messages"][0]["id"]).execute()
    assert set(msg["labelIds"]) == {"INBOX", "UNREAD", "CATEGORY_PERSONAL"}
    assert not any(h["name"] == "Bcc" for h in msg["payload"]["headers"])


def test_media_upload_send(gmail):
    raw = base64.urlsafe_b64decode(raw_message(subject="Uploaded"))
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="message/rfc822")
    sent = gmail.users().messages().send(userId="me", body={}, media_body=media).execute()
    got = gmail.users().messages().get(userId="me", id=sent["id"], format="metadata", metadataHeaders=["Subject"]).execute()
    assert got["payload"]["headers"][0]["value"] == "Uploaded"


def test_search_operators(http, gmail):
    a = receive(
        http,
        subject="Invoice March",
        text="Please pay",
        attachments=[{"filename": "invoice.pdf", "mimeType": "application/pdf", "content": "%PDF"}],
    )
    b = receive(http, **{"from": "news@shop.example"}, subject="Weekly deals", text="Big sale")
    gmail.users().messages().modify(userId="me", id=b["id"], body={"removeLabelIds": ["UNREAD"]}).execute()
    c = gmail.users().messages().send(userId="me", body={"raw": raw_message(subject="Re: plans", body="see you")}).execute()

    def q(query):
        return set(ids(gmail.users().messages().list(userId="me", q=query).execute()))

    assert q("from:alice") == {a["id"]}
    assert q("has:attachment") == {a["id"]}
    assert q("filename:pdf") == {a["id"]}
    assert q("is:unread") == {a["id"]}
    assert q("in:sent") == {c["id"]}
    assert q("subject:(weekly deals)") == {b["id"]}
    assert q("sale OR pay") == {a["id"], b["id"]}
    assert q("-in:sent is:read") == {b["id"]}
    assert q("{from:news from:alice}") == {a["id"], b["id"]}
    assert q('"big sale"') == {b["id"]}
    assert q("newer_than:1d") == {a["id"], b["id"], c["id"]}
    assert q("older_than:1d") == set()
    assert q("to:me") == {a["id"], b["id"]}
    assert q("from:me") == {c["id"]}


def test_list_label_filter_and_pagination(http, gmail):
    made = [receive(http, subject=f"n{i}")["id"] for i in range(5)]
    page1 = gmail.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=2).execute()
    assert ids(page1) == made[::-1][:2]
    page2 = gmail.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=2, pageToken=page1["nextPageToken"]).execute()
    assert ids(page2) == made[::-1][2:4]
    assert gmail.users().messages().list(userId="me", labelIds=["SPAM"]).execute() == {"resultSizeEstimate": 0}


def test_get_formats_and_attachment(http, gmail):
    m = receive(
        http,
        subject="Files",
        text="body text",
        html="<p>body <b>html</b></p>",
        attachments=[{"filename": "a.txt", "mimeType": "text/plain", "content": "attachment!"}],
    )
    full = gmail.users().messages().get(userId="me", id=m["id"]).execute()
    payload = full["payload"]
    assert payload["mimeType"] == "multipart/mixed"
    assert [p["partId"] for p in payload["parts"]] == ["0", "1"]
    alt = payload["parts"][0]
    assert alt["mimeType"] == "multipart/alternative"
    assert [p["partId"] for p in alt["parts"]] == ["0.0", "0.1"]
    assert base64.urlsafe_b64decode(alt["parts"][0]["body"]["data"]).strip() == b"body text"
    att_part = payload["parts"][1]
    assert att_part["filename"] == "a.txt" and "data" not in att_part["body"]
    att = gmail.users().messages().attachments().get(userId="me", messageId=m["id"], id=att_part["body"]["attachmentId"]).execute()
    assert base64.urlsafe_b64decode(att["data"]) == b"attachment!"
    assert att["size"] == len(b"attachment!")

    minimal = gmail.users().messages().get(userId="me", id=m["id"], format="minimal").execute()
    assert "payload" not in minimal and minimal["snippet"] == "body text"
    raw = gmail.users().messages().get(userId="me", id=m["id"], format="raw").execute()
    assert b"Subject: Files" in base64.urlsafe_b64decode(raw["raw"])


def test_ids_are_validated(gmail):
    with pytest.raises(HttpError) as err:
        gmail.users().messages().get(userId="me", id="not-hex!").execute()
    assert err.value.resp.status == 400
    with pytest.raises(HttpError) as err:
        gmail.users().messages().get(userId="me", id="abcdef0123456789").execute()
    assert err.value.resp.status == 404


def test_modify_batch_modify_and_invalid_label(http, gmail):
    a, b = receive(http)["id"], receive(http)["id"]
    out = gmail.users().messages().modify(userId="me", id=a, body={"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]}).execute()
    assert "STARRED" in out["labelIds"] and "UNREAD" not in out["labelIds"]
    gmail.users().messages().batchModify(userId="me", body={"ids": [a, b], "removeLabelIds": ["INBOX"]}).execute()
    assert gmail.users().messages().list(userId="me", labelIds=["INBOX"]).execute()["resultSizeEstimate"] == 0
    with pytest.raises(HttpError) as err:
        gmail.users().messages().modify(userId="me", id=a, body={"addLabelIds": ["Label_nope"]}).execute()
    assert "Invalid label" in str(err.value)


def test_trash_untrash_delete(http, gmail):
    m = receive(http)["id"]
    trashed = gmail.users().messages().trash(userId="me", id=m).execute()
    assert "TRASH" in trashed["labelIds"] and "INBOX" not in trashed["labelIds"]
    assert gmail.users().messages().list(userId="me").execute()["resultSizeEstimate"] == 0
    assert gmail.users().messages().list(userId="me", includeSpamTrash=True).execute()["resultSizeEstimate"] == 1
    assert ids(gmail.users().messages().list(userId="me", q="in:trash").execute()) == [m]
    restored = gmail.users().messages().untrash(userId="me", id=m).execute()
    assert "INBOX" in restored["labelIds"] and "TRASH" not in restored["labelIds"]
    gmail.users().messages().delete(userId="me", id=m).execute()
    with pytest.raises(HttpError):
        gmail.users().messages().get(userId="me", id=m).execute()


def test_batch_delete(http, gmail):
    made = [receive(http)["id"] for _ in range(3)]
    gmail.users().messages().batchDelete(userId="me", body={"ids": made[:2]}).execute()
    assert ids(gmail.users().messages().list(userId="me").execute()) == [made[2]]


def test_insert_and_import(gmail):
    raw = raw_message(sender="Old <old@example.org>", to=ME, subject="Archived", Date="Mon, 01 Jan 2024 10:00:00 +0000")
    inserted = gmail.users().messages().insert(userId="me", body={"raw": raw, "labelIds": ["INBOX"]}).execute()
    imported = gmail.users().messages().import_(userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).execute()
    a = gmail.users().messages().get(userId="me", id=inserted["id"], format="minimal").execute()
    b = gmail.users().messages().get(userId="me", id=imported["id"], format="minimal").execute()
    assert b["internalDate"] == "1704103200000"  # import defaults to the Date header
    assert int(a["internalDate"]) > 1704103200000  # insert defaults to receivedTime
    assert b["labelIds"] == ["INBOX", "UNREAD"]


def test_profile_counts(http, gmail):
    receive(http)
    profile = gmail.users().getProfile(userId="me").execute()
    assert profile["emailAddress"] == ME
    assert profile["messagesTotal"] == 1 and profile["threadsTotal"] == 1
