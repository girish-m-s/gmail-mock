"""In-process Cloud Pub/Sub stand-in for Gmail push notifications.

Real Gmail publishes ``{"emailAddress": ..., "historyId": ...}`` to the Pub/Sub
topic named in ``users.watch``. Pub/Sub then either pushes it to a subscriber's
HTTPS endpoint or keeps it for pull. This module does both:

* push subscriptions get an HTTP POST in the Pub/Sub push envelope format;
* pull subscriptions queue messages for ``subscriptions:pull``;
* with ``emulator_host`` set, every message is also published to a real
  Pub/Sub emulator (``gcloud beta emulators pubsub start``).
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .errors import ApiError, not_found
from .util import rfc3339

log = logging.getLogger("gmail_mock.pubsub")


@dataclass
class Subscription:
    name: str
    topic: str
    push_endpoint: str | None = None
    attributes: dict = field(default_factory=dict)
    pending: deque = field(default_factory=deque)
    outstanding: dict = field(default_factory=dict)

    def resource(self) -> dict:
        out = {"name": self.name, "topic": self.topic, "ackDeadlineSeconds": 10}
        out["pushConfig"] = {"pushEndpoint": self.push_endpoint, "attributes": self.attributes} if self.push_endpoint else {}
        return out


class PubSub:
    def __init__(self, emulator_host: str | None = None, push_retries: int = 3) -> None:
        self.emulator_host = emulator_host
        self.push_retries = push_retries
        self.topics: set[str] = set()
        self.subscriptions: dict[str, Subscription] = {}
        self.published: list[dict] = []
        self.deliveries: list[dict] = []
        self._lock = threading.RLock()
        self._counter = 0
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="pubsub-push", daemon=True)
        self._worker.start()

    # --- admin ---------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.topics.clear()
            self.subscriptions.clear()
            self.published.clear()
            self.deliveries.clear()

    def create_topic(self, name: str, *, exist_ok: bool = True) -> dict:
        with self._lock:
            if name in self.topics and not exist_ok:
                raise ApiError(409, "Resource already exists in the project (resource={}).".format(name.rsplit("/", 1)[-1]))
            self.topics.add(name)
        return {"name": name}

    def delete_topic(self, name: str) -> None:
        with self._lock:
            if name not in self.topics:
                raise not_found("Resource not found (resource={}).".format(name.rsplit("/", 1)[-1]))
            self.topics.discard(name)

    def create_subscription(self, name: str, topic: str, push_endpoint: str | None = None, attributes: dict | None = None) -> Subscription:
        with self._lock:
            if name in self.subscriptions:
                raise ApiError(409, "Resource already exists in the project (resource={}).".format(name.rsplit("/", 1)[-1]))
            self.topics.add(topic)
            sub = Subscription(name, topic, push_endpoint, attributes or {})
            self.subscriptions[name] = sub
            return sub

    def get_subscription(self, name: str) -> Subscription:
        sub = self.subscriptions.get(name)
        if sub is None:
            raise not_found("Resource not found (resource={}).".format(name.rsplit("/", 1)[-1]))
        return sub

    def delete_subscription(self, name: str) -> None:
        with self._lock:
            self.get_subscription(name)
            del self.subscriptions[name]

    # --- data plane ---------------------------------------------------------------

    def publish(self, topic: str, data: bytes, attributes: dict | None = None) -> str:
        with self._lock:
            self._counter += 1
            message_id = str(10_000_000_000_000 + self._counter)
            message = {
                "data": base64.b64encode(data).decode(),
                "messageId": message_id,
                "message_id": message_id,
                "publishTime": rfc3339(),
                "publish_time": rfc3339(),
            }
            if attributes:
                message["attributes"] = attributes
            self.published.append({"topic": topic, "message": message, "decoded": _decode(data)})
            for sub in self.subscriptions.values():
                if sub.topic != topic:
                    continue
                if sub.push_endpoint:
                    self._queue.put((sub, message))
                else:
                    sub.pending.append(message)
        if self.emulator_host:
            self._queue.put((None, (topic, data, attributes or {})))
        return message_id

    def pull(self, name: str, max_messages: int) -> list[dict]:
        with self._lock:
            sub = self.get_subscription(name)
            out = []
            while sub.pending and len(out) < max(1, max_messages):
                message = sub.pending.popleft()
                ack_id = f"{name}:{message['messageId']}"
                sub.outstanding[ack_id] = message
                out.append({"ackId": ack_id, "message": message})
            return out

    def acknowledge(self, name: str, ack_ids: list[str]) -> None:
        with self._lock:
            sub = self.get_subscription(name)
            for ack_id in ack_ids:
                sub.outstanding.pop(ack_id, None)

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Block until queued push deliveries finish (handy in tests)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    # --- delivery worker ----------------------------------------------------------

    def _run(self) -> None:
        with httpx.Client(timeout=5.0) as client:
            while True:
                sub, payload = self._queue.get()
                try:
                    if sub is None:
                        self._forward_to_emulator(client, *payload)
                    else:
                        self._push(client, sub, payload)
                except Exception:  # noqa: BLE001 - the worker must never die
                    log.exception("pubsub delivery failed")
                finally:
                    self._queue.task_done()

    def _push(self, client: httpx.Client, sub: Subscription, message: dict) -> None:
        envelope = {"message": message, "subscription": sub.name}
        status = None
        for attempt in range(self.push_retries):
            try:
                resp = client.post(
                    sub.push_endpoint,
                    json=envelope,
                    headers={"User-Agent": "APIs-Google; (+https://developers.google.com/webmasters/APIs-Google.html)"},
                )
                status = resp.status_code
                if 200 <= status < 300 or status == 102:
                    break
            except httpx.HTTPError as exc:
                status = f"error: {exc}"
            time.sleep(0.1 * (2**attempt))
        with self._lock:
            self.deliveries.append(
                {"subscription": sub.name, "endpoint": sub.push_endpoint, "messageId": message["messageId"], "status": status}
            )

    def _forward_to_emulator(self, client: httpx.Client, topic: str, data: bytes, attributes: dict) -> None:
        base = self.emulator_host if self.emulator_host.startswith("http") else f"http://{self.emulator_host}"
        client.put(f"{base}/v1/{topic}")  # create if missing; 409 is fine
        body = {"messages": [{"data": base64.b64encode(data).decode(), "attributes": attributes}]}
        client.post(f"{base}/v1/{topic}:publish", json=body).raise_for_status()


def _decode(data: bytes):
    try:
        return json.loads(data)
    except ValueError:
        return None
