"""End-to-end demo of the two Gmail triggers against a running gmail-mock.

    gmail-mock &                      # HTTP on :12411
    uv run python examples/triggers_demo.py

It starts a local push endpoint, subscribes it to a Pub/Sub topic, calls
users.watch, then:
  1. simulates an incoming email  -> "New Gmail Message Received"
  2. sends an email               -> "Email Sent"
For each Pub/Sub push it calls history.list, just as a production integration would.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from gmail_mock.client import build_service

BASE = os.environ.get("GMAIL_MOCK_URL", "http://127.0.0.1:12411")
ME = "me@example.com"
TOPIC = "projects/demo/topics/gmail"
events: queue.Queue = queue.Queue()


class Push(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        events.put(json.loads(base64.b64decode(body["message"]["data"])))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Push)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    push_url = f"http://127.0.0.1:{server.server_address[1]}/"

    control = httpx.Client(base_url=BASE)
    control.post("/_mock/reset")
    control.post("/_mock/pubsub/subscriptions", json={"name": "projects/demo/subscriptions/demo", "topic": TOPIC, "pushEndpoint": push_url})

    gmail = build_service("gmail", BASE, ME)
    history_id = gmail.users().watch(userId="me", body={"topicName": TOPIC}).execute()["historyId"]
    print(f"watching {ME} from historyId {history_id}")

    def drain(label: str) -> None:
        nonlocal history_id
        note = events.get(timeout=5)
        changes = gmail.users().history().list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"]).execute()
        history_id = str(note["historyId"])
        for record in changes.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = (
                    gmail.users()
                    .messages()
                    .get(userId="me", id=added["message"]["id"], format="metadata", metadataHeaders=["Subject"])
                    .execute()
                )
                subject = msg["payload"]["headers"][0]["value"] if msg["payload"]["headers"] else ""
                print(f"[{label}] {msg['id']} labels={msg['labelIds']} subject={subject!r}")

    control.post(f"/_mock/users/{ME}/messages", json={"from": "Carol <carol@example.org>", "subject": "Are we still on for lunch?"})
    drain("New Gmail Message Received")

    email = EmailMessage()
    email["To"] = "carol@example.org"
    email["Subject"] = "Yes, 12:30 works"
    email.set_content("See you there.")
    gmail.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(email.as_bytes()).decode()}).execute()
    drain("Email Sent")
    server.shutdown()


if __name__ == "__main__":
    main()
