"""Command-line entry point: ``gmail-mock [-http-port 12411] [-https-port 12412] ...``."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn

from . import __version__, seed
from .app import create_app
from .pubsub import PubSub
from .store import Store

DEFAULT_HTTP_PORT = 12411
DEFAULT_HTTPS_PORT = 12412


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail-mock", description="Stateful mock server for the Gmail API.")
    p.add_argument("--host", default=os.environ.get("GMAIL_MOCK_HOST", "127.0.0.1"), help="interface to bind (use 0.0.0.0 in containers)")
    p.add_argument(
        "--http-port", "-http-port", type=int, default=None, help=f"HTTP port (default {DEFAULT_HTTP_PORT}; 0 picks a free port)"
    )
    p.add_argument(
        "--https-port", "-https-port", type=int, default=None, help=f"HTTPS port with a self-signed cert (default {DEFAULT_HTTPS_PORT})"
    )
    p.add_argument("--http-unix", help="listen for HTTP on a Unix socket")
    p.add_argument(
        "--email",
        default=os.environ.get("GMAIL_MOCK_EMAIL", "me@example.com"),
        help="mailbox used when the bearer token is not an email address",
    )
    p.add_argument("--display-name", default="Mock User")
    p.add_argument("--seed", default=os.environ.get("GMAIL_MOCK_SEED"), help="JSON fixture file to load at startup")
    p.add_argument(
        "--push",
        action="append",
        default=[],
        metavar="TOPIC=URL",
        help="create a push subscription: projects/p/topics/t=https://host/path (repeatable)",
    )
    p.add_argument(
        "--pubsub-emulator-host",
        default=os.environ.get("PUBSUB_EMULATOR_HOST"),
        help="also publish Gmail notifications to a Cloud Pub/Sub emulator",
    )
    p.add_argument("--no-auth", action="store_true", help="accept requests without an Authorization header")
    p.add_argument(
        "--history-limit", type=int, default=100_000, help="history records kept per mailbox; older startHistoryIds get 404 (0 = unlimited)"
    )
    p.add_argument("--cert", help="TLS certificate for HTTPS (defaults to a generated self-signed one)")
    p.add_argument("--key", help="TLS private key for HTTPS")
    p.add_argument("--log-level", default="info")
    p.add_argument("--version", action="version", version=f"gmail-mock {__version__}")
    return p


def _self_signed(host: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gmail-mock")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    folder = Path(tempfile.mkdtemp(prefix="gmail-mock-"))
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def build_store(args: argparse.Namespace) -> Store:
    store = Store(args.email, args.display_name, PubSub(emulator_host=args.pubsub_emulator_host), history_limit=args.history_limit)
    if args.seed:
        seed.load_file(store, args.seed)
    for spec in args.push:
        topic, _, url = spec.partition("=")
        if not topic or not url:
            raise SystemExit(f"--push expects TOPIC=URL, got {spec!r}")
        name = f"{topic.rsplit('/topics/', 1)[0]}/subscriptions/gmail-mock-push-{len(store.pubsub.subscriptions) + 1}"
        store.pubsub.create_subscription(name, topic, url)
    return store


async def _serve(args: argparse.Namespace) -> None:
    app = create_app(build_store(args), require_auth=not args.no_auth)
    servers = []
    http_port = args.http_port
    https_port = args.https_port
    if http_port is None and https_port is None and not args.http_unix:
        http_port, https_port = DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT
    log = logging.getLogger("gmail_mock")
    if args.http_unix:
        config = uvicorn.Config(app, uds=args.http_unix, log_level=args.log_level)
        servers.append((uvicorn.Server(config), None))
        log.warning("gmail-mock listening for HTTP on unix:%s", args.http_unix)
    if http_port is not None:
        sock = _bind(args.host, http_port)
        config = uvicorn.Config(app, log_level=args.log_level)
        servers.append((uvicorn.Server(config), sock))
        log.warning("gmail-mock %s listening for HTTP on http://%s:%d", __version__, args.host, sock.getsockname()[1])
    if https_port is not None:
        cert, key = (args.cert, args.key) if args.cert else _self_signed(args.host)
        sock = _bind(args.host, https_port)
        config = uvicorn.Config(app, ssl_certfile=cert, ssl_keyfile=key, log_level=args.log_level)
        servers.append((uvicorn.Server(config), sock))
        log.warning("gmail-mock %s listening for HTTPS on https://%s:%d", __version__, args.host, sock.getsockname()[1])
    await asyncio.gather(*(srv.serve(sockets=[sock] if sock else None) for srv, sock in servers))


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
