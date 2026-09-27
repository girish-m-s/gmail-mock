from __future__ import annotations

import base64
import json
import socket
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn
from contract import check_response

from gmail_mock.app import create_app
from gmail_mock.client import build_service
from gmail_mock.store import Store

ME = "me@example.com"
VIOLATIONS: list[str] = []
CALLED: set[str] = set()


def _instrument(dispatcher) -> None:
    """Validate every JSON response against the discovery schema and record which methods ran."""
    original = dispatcher.handle

    def handle(req):
        resp = original(req)
        match = dispatcher.catalog.match(req.method, req.path)
        if match is None:
            return resp
        method = match[0]
        if 200 <= resp.status < 300:
            CALLED.add(method.id)
        fields_param = any(k == "fields" for k, _ in req.query)
        if resp.status == 200 and method.response_ref and resp.body and not fields_param:
            for err in check_response(method.api, method.response_ref, json.loads(resp.body)):
                VIOLATIONS.append(f"{method.id}: {err}")
        if resp.status >= 400 and resp.content_type.startswith("application/json"):
            error = json.loads(resp.body).get("error", {})
            if set(error) != {"code", "message", "errors", "status"} or error["code"] != resp.status:
                VIOLATIONS.append(f"{method.id}: malformed error body {resp.body[:200]!r}")
        if resp.status >= 500 and resp.status != 501:  # 501 = documented unsupported feature
            VIOLATIONS.append(f"{method.id}: server error {resp.status} {resp.body[:300]!r}")
        return resp

    dispatcher.handle = handle


class _ServerThread(threading.Thread):
    def __init__(self, app) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def run(self) -> None:
        self.server.run(sockets=[self.sock])

    def wait(self) -> None:
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)


@pytest.fixture(scope="session")
def _server():
    store = Store(ME, "Mock User")
    app = create_app(store)
    _instrument(app.state.dispatcher)
    thread = _ServerThread(app)
    thread.start()
    thread.wait()
    yield store, f"http://127.0.0.1:{thread.port}"
    thread.server.should_exit = True


@pytest.fixture(autouse=True)
def _no_contract_violations():
    VIOLATIONS.clear()
    yield
    assert not VIOLATIONS, "contract violations:\n" + "\n".join(VIOLATIONS[:30])


@pytest.fixture
def store(_server) -> Store:
    store, _ = _server
    store.reset()
    return store


@pytest.fixture
def base_url(_server, store) -> str:
    return _server[1]


@pytest.fixture
def http(base_url):
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {ME}"}) as client:
        yield client


def _service(api: str, base_url: str, token: str):
    return build_service(api, base_url, token)


@pytest.fixture
def gmail(base_url):
    """The official google-api-python-client, pointed at the mock."""
    return _service("gmail", base_url, ME)


@pytest.fixture
def gmail_for(base_url):
    return lambda email: _service("gmail", base_url, email)


@pytest.fixture
def people(base_url):
    return _service("people", base_url, ME)


def raw_message(to="friend@example.org", subject="Hello", body="Hi there", sender=None, **headers) -> str:
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


@pytest.fixture
def push_receiver():
    """A local HTTP endpoint that records Pub/Sub push deliveries."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/push", received
    server.shutdown()
