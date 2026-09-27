"""Google front-end behaviour: auth, validation, partial responses, batch, faults, request log."""

import json

import pytest
from conftest import ME, raw_message
from googleapiclient.errors import HttpError


def test_missing_auth_is_401(base_url):
    import httpx

    resp = httpx.get(f"{base_url}/gmail/v1/users/me/profile")
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["status"] == "UNAUTHENTICATED" and err["errors"][0]["reason"] == "required"


def test_token_email_selects_mailbox_and_delegation_is_checked(http, store, gmail_for):
    assert gmail_for("zoe@example.com").users().getProfile(userId="me").execute()["emailAddress"] == "zoe@example.com"
    resp = http.get("/gmail/v1/users/nobody@example.com/profile")
    assert resp.status_code == 403
    assert http.get("/gmail/v1/users/zoe@example.com/profile").status_code == 200  # existing mailbox: delegated


def test_unknown_query_param_and_body_field(http):
    resp = http.get("/gmail/v1/users/me/messages", params={"bogus": "1"})
    assert resp.status_code == 400 and "Cannot bind query parameter" in resp.json()["error"]["message"]
    resp = http.post("/gmail/v1/users/me/labels", json={"name": "x", "colour": {}})
    assert resp.status_code == 400 and 'Unknown name "colour"' in resp.json()["error"]["message"]
    resp = http.get("/gmail/v1/users/me/messages", params={"maxResults": "lots"})
    assert resp.status_code == 400
    resp = http.post("/gmail/v1/users/me/messages/send", content=b"{not json")
    assert resp.status_code == 400


def test_snake_case_body_is_accepted(http):
    resp = http.post("/gmail/v1/users/me/labels", json={"name": "Snake", "label_list_visibility": "labelHide"})
    assert resp.status_code == 200 and resp.json()["labelListVisibility"] == "labelHide"


def test_fields_partial_response(http, gmail):
    gmail.users().messages().send(userId="me", body={"raw": raw_message()}).execute()
    resp = http.get("/gmail/v1/users/me/messages", params={"fields": "messages(id),resultSizeEstimate"})
    body = resp.json()
    assert set(body) == {"messages", "resultSizeEstimate"} and set(body["messages"][0]) == {"id"}
    compact = http.get("/gmail/v1/users/me/profile", params={"prettyPrint": "false"})
    assert b"\n" not in compact.content


def test_batch_requests(gmail):
    ids = [gmail.users().messages().send(userId="me", body={"raw": raw_message(subject=f"s{i}")}).execute()["id"] for i in range(3)]
    results = {}

    def collect(request_id, response, exception):
        results[request_id] = exception or response

    batch = gmail.new_batch_http_request(callback=collect)
    for mid in ids:
        batch.add(gmail.users().messages().get(userId="me", id=mid, format="minimal"), request_id=mid)
    batch.add(gmail.users().messages().get(userId="me", id="ffffffffffffffff"), request_id="missing")
    batch.execute()
    assert all(results[mid]["id"] == mid for mid in ids)
    assert isinstance(results["missing"], HttpError) and results["missing"].resp.status == 404


def test_fault_injection(http, gmail):
    http.post("/_mock/faults", json={"methodId": "gmail.users.messages.send", "status": 429, "reason": "rateLimitExceeded", "count": 1})
    with pytest.raises(HttpError) as err:
        gmail.users().messages().send(userId="me", body={"raw": raw_message()}).execute()
    assert err.value.resp.status == 429
    assert gmail.users().messages().send(userId="me", body={"raw": raw_message()}).execute()["labelIds"] == ["SENT"]


def test_request_log(http, gmail):
    gmail.users().labels().list(userId="me").execute()
    log = http.get("/_mock/requests", params={"methodId": "gmail.users.labels.list"}).json()["requests"]
    assert log[-1]["status"] == 200 and log[-1]["path"] == "/gmail/v1/users/me/labels"


def test_multipart_upload_raw_http(http):
    boundary = "===b==="
    raw = raw_message(subject="multipart")
    import base64

    body = (
        (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps({'labelIds': ['INBOX']})}\r\n"
            f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n"
        ).encode()
        + base64.urlsafe_b64decode(raw)
        + f"\r\n--{boundary}--".encode()
    )
    resp = http.post(
        "/upload/gmail/v1/users/me/messages",
        params={"uploadType": "multipart"},
        content=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    assert resp.status_code == 200 and resp.json()["labelIds"] == ["INBOX"]
    resp = http.post("/resumable/upload/gmail/v1/users/me/messages/send", params={"uploadType": "resumable"})
    assert resp.status_code == 501


def test_seed_example_file(http, store):
    from pathlib import Path

    from gmail_mock import seed

    seed.load_file(store, Path(__file__).resolve().parents[1] / "examples" / "seed.json")
    users = {u["email"]: u for u in http.get("/_mock/users").json()["users"]}
    assert users[ME]["messages"] > 0
