"""FastAPI application: Google API surface, Pub/Sub subset, discovery docs and the /_mock control API."""

from __future__ import annotations

import base64
import copy
from typing import Any
from urllib.parse import parse_qsl

import anyio
from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, mime, seed
from .dispatch import Dispatcher, RawRequest
from .errors import ApiError
from .handlers import REGISTRY
from .store import Store
from .util import now_ms

FASTAPI_PREFIXES = ("/_mock", "/v1/projects/", "/discovery/", "/$discovery")
LARGE_BODY = 1_000_000


class GoogleApiMiddleware:
    """Serve Google API paths straight from the dispatcher, bypassing FastAPI routing.

    Only the control API, the Pub/Sub subset and discovery documents go through
    FastAPI. Requests with large bodies run in a worker thread so decoding them
    does not stall the event loop; at most two of those run at a time.
    """

    def __init__(self, app, dispatcher: Dispatcher, large_body_concurrency: int = 2) -> None:
        self.app = app
        self.dispatcher = dispatcher
        # Large uploads briefly need ~4x their size in memory; bound how many decode at once.
        self.large_limiter = anyio.CapacityLimiter(large_body_concurrency)

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(FASTAPI_PREFIXES):
            await self.app(scope, receive, send)
            return
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        raw_path = (scope.get("raw_path") or path.encode()).decode("latin-1").split("?", 1)[0]
        req = RawRequest(
            method=scope["method"],
            path=raw_path,
            query=parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True),
            headers={k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]},
            body=body,
        )
        is_batch = scope["method"] == "POST" and (raw_path == "/batch" or raw_path.startswith("/batch/"))
        handle = self.dispatcher.handle_batch if is_batch else self.dispatcher.handle
        if len(body) > LARGE_BODY:
            resp = await anyio.to_thread.run_sync(handle, req, limiter=self.large_limiter)
        else:
            resp = handle(req)
        headers = [(b"content-type", resp.content_type.encode()), (b"content-length", str(len(resp.body)).encode())]
        headers += [(k.lower().encode(), v.encode()) for k, v in resp.headers.items()]
        await send({"type": "http.response.start", "status": resp.status, "headers": headers})
        await send({"type": "http.response.body", "body": resp.body})


def _api_error(err: ApiError) -> JSONResponse:
    return JSONResponse(err.to_dict(), status_code=err.code)


# --- control API models -----------------------------------------------------------------


class UserIn(BaseModel):
    email: str
    displayName: str | None = None


class AttachmentIn(BaseModel):
    filename: str
    mimeType: str = "application/octet-stream"
    data: str | None = Field(None, description="Base64 content")
    content: str | None = Field(None, description="Plain-text content (alternative to data)")


class IncomingMessage(BaseModel):
    """Simulate a message arriving from outside (fires the "New message received" trigger)."""

    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field("Sender <sender@example.net>", alias="from")
    to: list[str] | str | None = None
    cc: list[str] | str | None = None
    subject: str = ""
    text: str | None = None
    html: str | None = None
    attachments: list[AttachmentIn] = []
    labelIds: list[str] | None = Field(None, description="Defaults to INBOX, UNREAD, CATEGORY_PERSONAL (then filters apply)")
    inReplyTo: str | None = Field(None, description="Gmail message id in this mailbox to reply to (threads the message)")
    raw: str | None = Field(None, description="Base64url RFC 822 message; overrides the other fields")
    headers: dict[str, str] | None = None


class FaultIn(BaseModel):
    methodId: str = Field(..., description="Discovery method id, e.g. gmail.users.messages.send, or * for all")
    status: int = 500
    message: str | None = None
    reason: str | None = None
    count: int = Field(1, description="How many requests fail; -1 means until cleared")


class SubscriptionIn(BaseModel):
    name: str = Field(..., examples=["projects/demo/subscriptions/gmail-push"])
    topic: str = Field(..., examples=["projects/demo/topics/gmail"])
    pushEndpoint: str | None = None


def create_app(store: Store | None = None, *, require_auth: bool = True) -> FastAPI:
    store = store or Store()
    dispatcher = Dispatcher(store, require_auth=require_auth)
    app = FastAPI(
        title="gmail-mock",
        version=__version__,
        description="A stateful mock of the Gmail API (plus the People API subset Gmail connectors use).",
        docs_url="/_mock/docs",
        redoc_url=None,
        openapi_url="/_mock/openapi.json",
    )
    app.state.store = store
    app.state.dispatcher = dispatcher
    control = APIRouter(prefix="/_mock", tags=["control"])

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, err: ApiError):
        return _api_error(err)

    # --- control API ------------------------------------------------------------------

    @control.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @control.post("/reset")
    def reset():
        store.reset()
        return {"status": "reset"}

    @control.get("/users")
    def list_users():
        with store.lock:
            return {
                "users": [
                    {
                        "email": b.email,
                        "displayName": b.display_name,
                        "messages": len(b.messages),
                        "threads": len(b.threads),
                        "historyId": str(b.history_id),
                        "watch": b.watch,
                    }
                    for b in store.mailboxes.values()
                ]
            }

    @control.post("/users", status_code=201)
    def create_user(user: UserIn):
        box = store.ensure_mailbox(user.email, user.displayName)
        return {"email": box.email, "displayName": box.display_name}

    @control.post("/users/{email}/messages", status_code=201)
    def receive(email: str, msg: IncomingMessage):
        with store.lock:
            box = store.mailboxes.get(email.lower())
            if box is None:
                raise HTTPException(404, f"No mailbox {email}; create it with POST /_mock/users")
            if msg.raw:
                raw = base64.urlsafe_b64decode(msg.raw + "=" * (-len(msg.raw) % 4))
            else:
                parent = box.message(msg.inReplyTo) if msg.inReplyTo else None
                spec: dict[str, Any] = msg.model_dump(by_alias=True, exclude_none=True)
                spec["to"] = spec.get("to") or [box.email]
                spec["date"] = now_ms()
                raw = seed.compose_message(box, spec, parent)
            labels = seed.resolve_labels(box, msg.labelIds) if msg.labelIds is not None else None
            created = box.receive(raw, labels)
            return created.resource("minimal")

    @control.post("/seed")
    def load_seed(data: dict = Body(...)):
        boxes = seed.load(store, data)
        return {"loaded": [b.email for b in boxes]}

    @control.get("/faults")
    def list_faults():
        return {"faults": store.faults}

    @control.post("/faults", status_code=201)
    def add_fault(fault: FaultIn):
        entry = {
            "methodId": fault.methodId,
            "status": fault.status,
            "message": fault.message,
            "reason": fault.reason,
            "remaining": fault.count,
        }
        with store.lock:
            store.faults.append(entry)
        return entry

    @control.delete("/faults")
    def clear_faults():
        with store.lock:
            store.faults.clear()
        return {"faults": []}

    @control.get("/requests")
    def list_requests(methodId: str | None = None, limit: int = 100):
        items = [r for r in list(store.requests) if not methodId or r["methodId"] == methodId]
        return {"requests": items[-limit:]}

    @control.delete("/requests")
    def clear_requests():
        store.requests.clear()
        return {"requests": []}

    @control.get("/pubsub/published")
    def published(topic: str | None = None):
        return {"published": [p for p in store.pubsub.published if not topic or p["topic"] == topic]}

    @control.get("/pubsub/deliveries")
    def deliveries():
        store.pubsub.wait_idle()
        return {"deliveries": list(store.pubsub.deliveries)}

    @control.post("/pubsub/subscriptions", status_code=201)
    def add_subscription(sub: SubscriptionIn):
        return store.pubsub.create_subscription(sub.name, sub.topic, sub.pushEndpoint).resource()

    @control.post("/users/{email}/forwardingAddresses/{address}/verify")
    def verify_forwarding(email: str, address: str):
        with store.lock:
            box = store.mailboxes.get(email.lower())
            fwd = box.forwarding.get(address.lower()) if box else None
            if fwd is None:
                raise HTTPException(404, "Unknown mailbox or forwarding address")
            fwd["verificationStatus"] = "accepted"
            return fwd

    @control.get("/coverage")
    def coverage():
        methods = dispatcher.catalog.methods
        return {
            "methods": [
                {"id": mid, "httpMethod": m.http_method, "path": m.path, "stateful": mid in REGISTRY} for mid, m in sorted(methods.items())
            ],
            "stateful": sum(1 for mid in methods if mid in REGISTRY),
            "total": len(methods),
        }

    app.include_router(control)

    # --- Pub/Sub REST subset (pubsub.googleapis.com/v1) ----------------------------------

    pubsub = store.pubsub

    @app.put("/v1/projects/{project}/topics/{topic}", tags=["pubsub"])
    def ps_create_topic(project: str, topic: str):
        return pubsub.create_topic(f"projects/{project}/topics/{topic}", exist_ok=False)

    @app.post("/v1/projects/{project}/topics/{topic}:publish", tags=["pubsub"])
    def ps_publish(project: str, topic: str, body: dict = Body(...)):
        name = f"projects/{project}/topics/{topic}"
        ids = [pubsub.publish(name, base64.b64decode(m.get("data", "")), m.get("attributes")) for m in body.get("messages", [])]
        return {"messageIds": ids}

    @app.delete("/v1/projects/{project}/topics/{topic}", tags=["pubsub"])
    def ps_delete_topic(project: str, topic: str):
        pubsub.delete_topic(f"projects/{project}/topics/{topic}")
        return {}

    @app.post("/v1/projects/{project}/subscriptions/{sub}:pull", tags=["pubsub"])
    def ps_pull(project: str, sub: str, body: dict = Body(default={})):
        received = pubsub.pull(f"projects/{project}/subscriptions/{sub}", int(body.get("maxMessages", 10)))
        return {"receivedMessages": received} if received else {}

    @app.post("/v1/projects/{project}/subscriptions/{sub}:acknowledge", tags=["pubsub"])
    def ps_ack(project: str, sub: str, body: dict = Body(...)):
        pubsub.acknowledge(f"projects/{project}/subscriptions/{sub}", body.get("ackIds", []))
        return {}

    @app.put("/v1/projects/{project}/subscriptions/{sub}", tags=["pubsub"])
    def ps_create_sub(project: str, sub: str, body: dict = Body(...)):
        push = (body.get("pushConfig") or {}).get("pushEndpoint")
        return pubsub.create_subscription(f"projects/{project}/subscriptions/{sub}", body["topic"], push).resource()

    @app.get("/v1/projects/{project}/subscriptions/{sub}", tags=["pubsub"])
    def ps_get_sub(project: str, sub: str):
        return pubsub.get_subscription(f"projects/{project}/subscriptions/{sub}").resource()

    @app.delete("/v1/projects/{project}/subscriptions/{sub}", tags=["pubsub"])
    def ps_delete_sub(project: str, sub: str):
        pubsub.delete_subscription(f"projects/{project}/subscriptions/{sub}")
        return {}

    # --- discovery documents -----------------------------------------------------------

    def _discovery(api: str, request: Request):
        doc = copy.deepcopy(dispatcher.catalog.apis[api].doc)
        root = str(request.base_url)
        doc["rootUrl"] = doc["mtlsRootUrl"] = root
        doc["baseUrl"] = root + doc.get("servicePath", "")
        return JSONResponse(doc)

    @app.get("/$discovery/rest", include_in_schema=False)
    def discovery_default(request: Request, version: str = "v1"):
        return _discovery("gmail", request)

    @app.get("/discovery/v1/apis/{api}/{version}/rest", include_in_schema=False)
    def discovery(api: str, version: str, request: Request):
        if api not in dispatcher.catalog.apis or version != "v1":
            raise HTTPException(404, "Unknown API")
        return _discovery(api, request)

    # Everything else is the Google API surface, served by GoogleApiMiddleware (below).
    app.add_middleware(GoogleApiMiddleware, dispatcher=dispatcher)
    return app


def example_message(store: Store, email: str, **kwargs) -> dict:
    """Convenience for tests and scripts: deliver a composed message into a mailbox."""
    box = store.ensure_mailbox(email)
    raw = mime.compose(sender=kwargs.pop("sender", "Sender <sender@example.net>"), to=[box.email], **kwargs)
    return box.receive(raw).resource("minimal")
