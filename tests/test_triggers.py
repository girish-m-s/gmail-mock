"""The two Gmail triggers (new message received, email sent), done the way Google does it: watch -> Pub/Sub -> history.list."""

import base64
import json
import time

import pytest
from conftest import ME, raw_message
from googleapiclient.errors import HttpError

TOPIC = "projects/demo/topics/gmail"


def wait_for(items, n, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(items) < n and time.monotonic() < deadline:
        time.sleep(0.02)
    return items


def decode(push):
    return json.loads(base64.b64decode(push["message"]["data"]))


def test_watch_requires_valid_topic(gmail):
    with pytest.raises(HttpError) as err:
        gmail.users().watch(userId="me", body={"topicName": "gmail"}).execute()
    assert err.value.resp.status == 400


def test_new_message_received_and_email_sent_triggers(http, gmail, push_receiver):
    url, received = push_receiver
    http.post("/_mock/pubsub/subscriptions", json={"name": "projects/demo/subscriptions/push", "topic": TOPIC, "pushEndpoint": url})
    watch = gmail.users().watch(userId="me", body={"topicName": TOPIC}).execute()
    assert int(watch["expiration"]) > time.time() * 1000
    start = watch["historyId"]

    # Trigger 1: new message received
    incoming = http.post(f"/_mock/users/{ME}/messages", json={"from": "carol@example.org", "subject": "Ping"}).json()
    wait_for(received, 1)
    note = decode(received[0])
    assert note["emailAddress"] == ME and note["historyId"] > int(start)
    assert received[0]["subscription"] == "projects/demo/subscriptions/push"
    assert received[0]["message"]["messageId"]

    history = gmail.users().history().list(userId="me", startHistoryId=start, historyTypes=["messageAdded"]).execute()
    added = [h["messagesAdded"][0]["message"] for h in history["history"]]
    assert added[0]["id"] == incoming["id"] and "INBOX" in added[0]["labelIds"]
    assert history["historyId"] == str(note["historyId"])

    # Trigger 2: email sent
    sent = gmail.users().messages().send(userId="me", body={"raw": raw_message()}).execute()
    wait_for(received, 2)
    history = gmail.users().history().list(userId="me", startHistoryId=str(note["historyId"])).execute()
    added = history["history"][0]["messagesAdded"][0]["message"]
    assert added["id"] == sent["id"] and added["labelIds"] == ["SENT"]


def test_label_filtered_watch_only_notifies_matching_changes(http, gmail, push_receiver):
    url, received = push_receiver
    http.post("/_mock/pubsub/subscriptions", json={"name": "projects/demo/subscriptions/inbox", "topic": TOPIC, "pushEndpoint": url})
    gmail.users().watch(userId="me", body={"topicName": TOPIC, "labelIds": ["INBOX"], "labelFilterBehavior": "include"}).execute()
    gmail.users().messages().send(userId="me", body={"raw": raw_message()}).execute()  # SENT only
    http.post(f"/_mock/users/{ME}/messages", json={"subject": "hi"})
    http.get("/_mock/pubsub/deliveries")  # waits for the push queue to drain
    assert len(received) == 1


def test_stop_ends_notifications(http, gmail):
    gmail.users().watch(userId="me", body={"topicName": TOPIC}).execute()
    gmail.users().stop(userId="me").execute()
    http.post(f"/_mock/users/{ME}/messages", json={"subject": "hi"})
    assert http.get("/_mock/pubsub/published").json()["published"] == []


def test_pull_subscription_via_pubsub_rest(http, gmail):
    assert http.put("/v1/projects/demo/subscriptions/pull", json={"topic": TOPIC}).status_code == 200
    gmail.users().watch(userId="me", body={"topicName": TOPIC}).execute()
    http.post(f"/_mock/users/{ME}/messages", json={"subject": "hi"})
    pulled = http.post("/v1/projects/demo/subscriptions/pull:pull", json={"maxMessages": 5}).json()
    [msg] = pulled["receivedMessages"]
    assert json.loads(base64.b64decode(msg["message"]["data"]))["emailAddress"] == ME
    http.post("/v1/projects/demo/subscriptions/pull:acknowledge", json={"ackIds": [msg["ackId"]]})
    assert http.post("/v1/projects/demo/subscriptions/pull:pull", json={}).json() == {}


def test_history_filters_and_expiry(http, gmail):
    start = gmail.users().getProfile(userId="me").execute()["historyId"]
    m = http.post(f"/_mock/users/{ME}/messages", json={"subject": "hi"}).json()
    gmail.users().messages().modify(userId="me", id=m["id"], body={"removeLabelIds": ["UNREAD"]}).execute()
    only_removed = gmail.users().history().list(userId="me", startHistoryId=start, historyTypes=["labelRemoved"]).execute()
    assert only_removed["history"][0]["labelsRemoved"][0]["labelIds"] == ["UNREAD"]
    by_label = gmail.users().history().list(userId="me", startHistoryId=start, labelId="SENT").execute()
    assert "history" not in by_label
    with pytest.raises(HttpError) as err:
        gmail.users().history().list(userId="me", startHistoryId="1").execute()
    assert err.value.resp.status == 404
