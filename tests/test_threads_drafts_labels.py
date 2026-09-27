import pytest
from conftest import ME, raw_message
from googleapiclient.errors import HttpError


def receive(http, **kw):
    kw.setdefault("from", "Alice <alice@example.org>")
    return http.post(f"/_mock/users/{ME}/messages", json=kw).json()


# --- threads --------------------------------------------------------------------------


def test_reply_with_thread_id_and_matching_subject_joins_thread(http, gmail):
    first = receive(http, subject="Project kickoff", text="Agenda attached")
    headers = {h["name"]: h["value"] for h in gmail.users().messages().get(userId="me", id=first["id"]).execute()["payload"]["headers"]}
    reply = raw_message(
        to="alice@example.org", subject="Re: Project kickoff", In_Reply_To=headers["Message-ID"], References=headers["Message-ID"]
    )
    sent = gmail.users().messages().send(userId="me", body={"raw": reply, "threadId": first["threadId"]}).execute()
    assert sent["threadId"] == first["threadId"]
    thread = gmail.users().threads().get(userId="me", id=first["threadId"]).execute()
    assert [m["id"] for m in thread["messages"]] == [first["id"], sent["id"]]


def test_reply_with_different_subject_starts_new_thread(http, gmail):
    first = receive(http, subject="Project kickoff")
    sent = (
        gmail.users()
        .messages()
        .send(userId="me", body={"raw": raw_message(subject="Something else"), "threadId": first["threadId"]})
        .execute()
    )
    assert sent["threadId"] != first["threadId"]


def test_incoming_reply_threads_by_references(http, gmail):
    sent = gmail.users().messages().send(userId="me", body={"raw": raw_message(to="alice@example.org", subject="Question")}).execute()
    reply = receive(http, subject="Re: Question", inReplyTo=sent["id"])
    assert reply["threadId"] == sent["threadId"]


def test_thread_list_modify_trash_delete(http, gmail):
    a = receive(http, subject="T1", text="first thread")
    receive(http, subject="T2")
    listed = gmail.users().threads().list(userId="me").execute()
    assert listed["resultSizeEstimate"] == 2 and listed["threads"][1]["snippet"] == "first thread"
    modified = gmail.users().threads().modify(userId="me", id=a["threadId"], body={"addLabelIds": ["IMPORTANT"]}).execute()
    assert "IMPORTANT" in modified["messages"][0]["labelIds"]
    gmail.users().threads().trash(userId="me", id=a["threadId"]).execute()
    assert gmail.users().threads().list(userId="me").execute()["resultSizeEstimate"] == 1
    gmail.users().threads().untrash(userId="me", id=a["threadId"]).execute()
    gmail.users().threads().delete(userId="me", id=a["threadId"]).execute()
    with pytest.raises(HttpError) as err:
        gmail.users().threads().get(userId="me", id=a["threadId"]).execute()
    assert err.value.resp.status == 404


# --- drafts ---------------------------------------------------------------------------


def test_draft_lifecycle(gmail):
    drafts = gmail.users().drafts()
    created = drafts.create(userId="me", body={"message": {"raw": raw_message(subject="Draft v1")}}).execute()
    assert created["id"].startswith("r") and created["message"]["labelIds"] == ["DRAFT"]
    assert drafts.list(userId="me").execute()["drafts"][0]["id"] == created["id"]

    updated = drafts.update(userId="me", id=created["id"], body={"message": {"raw": raw_message(subject="Draft v2")}}).execute()
    assert updated["id"] == created["id"] and updated["message"]["id"] != created["message"]["id"]
    got = drafts.get(userId="me", id=created["id"], format="metadata").execute()
    subject = next(h["value"] for h in got["message"]["payload"]["headers"] if h["name"] == "Subject")
    assert subject == "Draft v2"

    sent = drafts.send(userId="me", body={"id": created["id"]}).execute()
    assert sent["labelIds"] == ["SENT"]
    assert drafts.list(userId="me").execute() == {"resultSizeEstimate": 0}
    with pytest.raises(HttpError):
        drafts.get(userId="me", id=created["id"]).execute()


def test_draft_delete_and_search(gmail):
    drafts = gmail.users().drafts()
    a = drafts.create(userId="me", body={"message": {"raw": raw_message(subject="budget numbers")}}).execute()
    drafts.create(userId="me", body={"message": {"raw": raw_message(subject="party")}}).execute()
    assert [d["id"] for d in drafts.list(userId="me", q="budget").execute()["drafts"]] == [a["id"]]
    drafts.delete(userId="me", id=a["id"]).execute()
    assert drafts.list(userId="me").execute()["resultSizeEstimate"] == 1


# --- labels ---------------------------------------------------------------------------


def test_label_crud_and_counts(http, gmail):
    labels = gmail.users().labels()
    system = {lbl["id"] for lbl in labels.list(userId="me").execute()["labels"]}
    assert {"INBOX", "SENT", "TRASH", "UNREAD", "CATEGORY_PERSONAL"} <= system

    work = labels.create(userId="me", body={"name": "Work", "color": {"textColor": "#ffffff", "backgroundColor": "#16a766"}}).execute()
    assert work["id"].startswith("Label_") and work["type"] == "user"
    with pytest.raises(HttpError) as err:
        labels.create(userId="me", body={"name": "work"}).execute()
    assert err.value.resp.status == 409
    with pytest.raises(HttpError) as err:
        labels.create(userId="me", body={"name": "Inbox"}).execute()
    assert err.value.resp.status == 400

    m = receive(http, labelIds=["INBOX", "UNREAD", "Work"])
    counts = labels.get(userId="me", id=work["id"]).execute()
    assert (counts["messagesTotal"], counts["messagesUnread"], counts["threadsTotal"]) == (1, 1, 1)
    assert gmail.users().messages().list(userId="me", q="label:work").execute()["messages"][0]["id"] == m["id"]

    patched = labels.patch(userId="me", id=work["id"], body={"name": "Work/Clients"}).execute()
    assert patched["name"] == "Work/Clients" and patched["color"]["textColor"] == "#ffffff"
    assert gmail.users().messages().list(userId="me", q="label:work-clients").execute()["resultSizeEstimate"] == 1
    updated = labels.update(userId="me", id=work["id"], body={"name": "Clients"}).execute()
    assert "color" not in updated

    labels.delete(userId="me", id=work["id"]).execute()
    msg = gmail.users().messages().get(userId="me", id=m["id"], format="minimal").execute()
    assert work["id"] not in msg["labelIds"]
    with pytest.raises(HttpError) as err:
        labels.delete(userId="me", id="INBOX").execute()
    assert err.value.resp.status == 400
