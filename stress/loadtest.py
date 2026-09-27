"""Stress tests for gmail-mock.

Starts the real server (``python -m gmail_mock``) in a subprocess and runs:

  throughput  mixed agent-like workload at increasing concurrency (multi-process load generator)
  scaling     per-endpoint latency as the mailbox grows (1k -> 50k messages)
  soak        sustained write-heavy load while sampling server RSS / CPU
  push        Pub/Sub push-notification throughput and end-to-end latency
  payload     concurrent large (25 MB) sends
  verify      correctness after load: counts, history ordering, no 5xx

    uv run python stress/loadtest.py                 # everything
    uv run python stress/loadtest.py throughput soak # selected phases
    uv run python stress/loadtest.py --quick         # shorter runs

Results are printed and written to stress/results/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import multiprocessing as mp
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from gmail_mock import mime  # noqa: E402

ME = "me@example.com"
AUTH = {"Authorization": f"Bearer {ME}"}
QUERIES = [
    "is:unread",
    "from:alice",
    "subject:report",
    "has:attachment",
    "label:work",
    "newer_than:7d",
    "invoice OR receipt",
    "-in:sent is:starred",
    '"quarterly numbers"',
]

# op -> weight. Read-heavy, like an agent triaging an inbox.
AGENT_MIX = {
    "list": 20,
    "get": 25,
    "search": 12,
    "thread": 6,
    "profile": 3,
    "labels": 4,
    "label_counts": 3,
    "history": 5,
    "send": 10,
    "modify": 8,
    "draft": 2,
    "batch_get": 2,
}
WRITE_MIX = {"send": 40, "modify": 25, "draft": 10, "get": 15, "list": 10}


# --- server management ----------------------------------------------------------------------


class Server:
    def __init__(self, *extra: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gmail_mock", "--http-port", "0", "--log-level", "warning", *extra],
            cwd=ROOT,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        deadline = time.time() + 20
        self.url = None
        while time.time() < deadline and self.url is None:
            line = self.proc.stderr.readline()
            m = re.search(r"http://[\d.]+:\d+", line)
            if m:
                self.url = m.group(0)
        if not self.url:
            raise RuntimeError("server did not start")
        threading.Thread(target=lambda: [None for _ in self.proc.stderr], daemon=True).start()
        self.ps = psutil.Process(self.proc.pid)
        self.ps.cpu_percent()

    def rss_mb(self) -> float:
        return self.ps.memory_info().rss / 1e6

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)


def raw_message(subject: str, size: int = 0) -> str:
    atts = [("blob.bin", "application/octet-stream", os.urandom(size))] if size else []
    return base64.urlsafe_b64encode(
        mime.compose(sender=ME, to=["peer@example.org"], subject=subject, text="load test body", attachments=atts)
    ).decode()


def seed_mailbox(url: str, n: int) -> float:
    """Load n varied messages through /_mock/seed; returns seconds taken."""
    senders = ["Alice <alice@example.org>", "Bob <bob@example.org>", "billing@shop.example", "news@digest.example"]
    subjects = ["Quarterly report", "Invoice #{i}", "Re: planning", "Receipt {i}", "Weekly digest", "Lunch?"]
    msgs = []
    for i in range(n):
        labels = ["INBOX", "CATEGORY_PERSONAL"]
        if i % 3 == 0:
            labels.append("UNREAD")
        if i % 10 == 0:
            labels.append("Work")
        if i % 17 == 0:
            labels.append("STARRED")
        item = {
            "from": senders[i % len(senders)],
            "subject": subjects[i % len(subjects)].format(i=i),
            "text": f"Message {i}. The quarterly numbers look good." if i % 50 == 0 else f"Message body {i}",
            "labels": labels,
            "date": 1_790_000_000_000 - i * 60_000,
        }
        if i % 25 == 0:
            item["attachments"] = [{"filename": f"doc{i}.pdf", "mimeType": "application/pdf", "content": "%PDF-1.4"}]
        msgs.append(item)
    started = time.perf_counter()
    for chunk in range(0, n, 5000):
        body = {"users": [{"email": ME, "labels": [{"name": "Work"}], "messages": msgs[chunk : chunk + 5000]}]}
        httpx.post(f"{url}/_mock/seed", json=body, timeout=600).raise_for_status()
    return time.perf_counter() - started


# --- load generator (runs in worker processes) ---------------------------------------------------


async def _worker_loop(url: str, mix: dict, deadline: float, ids: list, threads: list, start_history: str, rec: dict, seed: int) -> None:
    rnd = random.Random(seed)
    ops, weights = zip(*mix.items())
    raw = raw_message(f"load {seed}")
    async with httpx.AsyncClient(base_url=url, headers=AUTH, timeout=60, limits=httpx.Limits(max_connections=4)) as c:
        while time.perf_counter() < deadline:
            op = rnd.choices(ops, weights)[0]
            t0 = time.perf_counter()
            try:
                if op == "list":
                    r = await c.get("/gmail/v1/users/me/messages", params={"maxResults": 20})
                elif op == "get":
                    r = await c.get(
                        f"/gmail/v1/users/me/messages/{rnd.choice(ids)}", params={"format": rnd.choice(["full", "metadata", "minimal"])}
                    )
                elif op == "search":
                    r = await c.get("/gmail/v1/users/me/messages", params={"q": rnd.choice(QUERIES), "maxResults": 20})
                elif op == "thread":
                    r = await c.get(f"/gmail/v1/users/me/threads/{rnd.choice(threads)}")
                elif op == "profile":
                    r = await c.get("/gmail/v1/users/me/profile")
                elif op == "labels":
                    r = await c.get("/gmail/v1/users/me/labels")
                elif op == "label_counts":
                    r = await c.get("/gmail/v1/users/me/labels/INBOX")
                elif op == "history":
                    r = await c.get("/gmail/v1/users/me/history", params={"startHistoryId": start_history, "maxResults": 100})
                elif op == "send":
                    r = await c.post("/gmail/v1/users/me/messages/send", json={"raw": raw})
                    if r.status_code == 200:
                        ids.append(r.json()["id"])
                elif op == "modify":
                    label = rnd.choice(["STARRED", "IMPORTANT", "UNREAD"])
                    body = {"addLabelIds": [label]} if rnd.random() < 0.5 else {"removeLabelIds": [label]}
                    r = await c.post(f"/gmail/v1/users/me/messages/{rnd.choice(ids)}/modify", json=body)
                elif op == "draft":
                    r = await c.post("/gmail/v1/users/me/drafts", json={"message": {"raw": raw}})
                    if r.status_code == 200 and rnd.random() < 0.5:
                        r = await c.post("/gmail/v1/users/me/drafts/send", json={"id": r.json()["id"]})
                elif op == "batch_get":
                    parts = "".join(
                        f"--B\r\nContent-Type: application/http\r\nContent-ID: <{i}>\r\n\r\nGET /gmail/v1/users/me/messages/{rnd.choice(ids)}?format=minimal HTTP/1.1\r\n\r\n"
                        for i in range(10)
                    )
                    r = await c.post(
                        "/batch/gmail/v1", content=(parts + "--B--").encode(), headers={"Content-Type": "multipart/mixed; boundary=B"}
                    )
                status = r.status_code
            except httpx.HTTPError as exc:
                status = f"exc:{type(exc).__name__}"
            rec[op].append((time.perf_counter() - t0, status))


def _process_main(
    url: str, mix: dict, duration: float, concurrency: int, ids: list, threads: list, start_history: str, seed: int, out: mp.Queue
) -> None:
    rec: dict = defaultdict(list)

    async def main():
        deadline = time.perf_counter() + duration
        await asyncio.gather(
            *(_worker_loop(url, mix, deadline, list(ids), threads, start_history, rec, seed * 1000 + i) for i in range(concurrency))
        )

    asyncio.run(main())
    out.put(dict(rec))


def run_load(url: str, mix: dict, duration: float, concurrency: int, processes: int | None = None) -> dict:
    listing = httpx.get(f"{url}/gmail/v1/users/me/messages", params={"maxResults": 500}, headers=AUTH).json()
    ids = [m["id"] for m in listing.get("messages", [])] or [raw_send(url)]
    threads = list({m["threadId"] for m in listing.get("messages", [])}) or ids
    start_history = httpx.get(f"{url}/gmail/v1/users/me/profile", headers=AUTH).json()["historyId"]
    processes = processes or min(concurrency, 8)
    per = [concurrency // processes + (1 if i < concurrency % processes else 0) for i in range(processes)]
    q: mp.Queue = mp.Queue()
    procs = [
        mp.Process(target=_process_main, args=(url, mix, duration, n, ids, threads, start_history, i, q)) for i, n in enumerate(per) if n
    ]
    started = time.perf_counter()
    for p in procs:
        p.start()
    merged: dict = defaultdict(list)
    for _ in procs:
        for op, samples in q.get().items():
            merged[op].extend(samples)
    for p in procs:
        p.join()
    return summarize(merged, time.perf_counter() - started)


def raw_send(url: str) -> str:
    return httpx.post(f"{url}/gmail/v1/users/me/messages/send", json={"raw": raw_message("seed")}, headers=AUTH).json()["id"]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def summarize(merged: dict, wall: float) -> dict:
    ops = {}
    total, errors, server_errors = 0, 0, 0
    for op, samples in sorted(merged.items()):
        lat = [s[0] * 1000 for s in samples]
        bad = [s for s in samples if not (isinstance(s[1], int) and s[1] < 400)]
        s5 = [s for s in samples if not isinstance(s[1], int) or s[1] >= 500]
        total += len(samples)
        errors += len(bad)
        server_errors += len(s5)
        ops[op] = {
            "count": len(samples),
            "errors": len(bad),
            "5xx_or_exc": len(s5),
            "p50_ms": round(pct(lat, 50), 2),
            "p95_ms": round(pct(lat, 95), 2),
            "p99_ms": round(pct(lat, 99), 2),
            "max_ms": round(max(lat), 2),
            "statuses": sorted({str(s[1]) for s in samples}),
        }
    all_lat = [s[0] * 1000 for samples in merged.values() for s in samples]
    return {
        "requests": total,
        "rps": round(total / wall, 1),
        "errors": errors,
        "server_errors": server_errors,
        "p50_ms": round(pct(all_lat, 50), 2),
        "p95_ms": round(pct(all_lat, 95), 2),
        "p99_ms": round(pct(all_lat, 99), 2),
        "ops": ops,
    }


# --- phases ---------------------------------------------------------------------------------------


def phase_throughput(args) -> dict:
    out = {}
    levels = [1, 8, 32, 64, 128] if not args.quick else [1, 16, 64]
    for conc in levels:
        srv = Server()
        try:
            seed_mailbox(srv.url, 2000)
            res = run_load(srv.url, AGENT_MIX, args.duration, conc)
            res["server_cpu_pct"] = round(srv.ps.cpu_percent(), 1)
            res["server_rss_mb"] = round(srv.rss_mb(), 1)
            out[str(conc)] = res
            print(
                f"  concurrency {conc:>3}: {res['rps']:>7} req/s  p50 {res['p50_ms']:>7} ms  p95 {res['p95_ms']:>7} ms  p99 {res['p99_ms']:>8} ms  "
                f"errors {res['errors']} (5xx {res['server_errors']})  cpu {res['server_cpu_pct']}%"
            )
        finally:
            srv.stop()
    return out


def _time_op(fn, reps: int) -> dict:
    lat = []
    for _ in range(reps):
        t0 = time.perf_counter()
        r = fn()
        lat.append((time.perf_counter() - t0) * 1000)
        assert r.status_code < 400, r.text[:200]
    return {"p50_ms": round(pct(lat, 50), 2), "p95_ms": round(pct(lat, 95), 2)}


def phase_scaling(args) -> dict:
    sizes = [1000, 10000, 50000] if not args.quick else [1000, 10000]
    out = {}
    for n in sizes:
        srv = Server()
        try:
            load_s = seed_mailbox(srv.url, n)
            c = httpx.Client(base_url=srv.url, headers=AUTH, timeout=120)
            some = c.get("/gmail/v1/users/me/messages", params={"maxResults": 50}).json()["messages"]
            mid, tid = some[10]["id"], some[10]["threadId"]
            reps = 30
            ops = {
                "messages.list (20)": lambda: c.get("/gmail/v1/users/me/messages", params={"maxResults": 20}),
                "messages.list page 10": lambda: c.get(
                    "/gmail/v1/users/me/messages", params={"maxResults": 20, "pageToken": "b2Zmc2V0OjIwMA"}
                ),
                "search is:unread": lambda: c.get("/gmail/v1/users/me/messages", params={"q": "is:unread", "maxResults": 20}),
                "search free text": lambda: c.get("/gmail/v1/users/me/messages", params={"q": '"quarterly numbers"', "maxResults": 20}),
                "search from: OR": lambda: c.get("/gmail/v1/users/me/messages", params={"q": "from:alice OR from:bob", "maxResults": 20}),
                "threads.list (20)": lambda: c.get("/gmail/v1/users/me/threads", params={"maxResults": 20}),
                "messages.get full": lambda: c.get(f"/gmail/v1/users/me/messages/{mid}"),
                "threads.get": lambda: c.get(f"/gmail/v1/users/me/threads/{tid}"),
                "labels.get INBOX (counts)": lambda: c.get("/gmail/v1/users/me/labels/INBOX"),
                "labels.list": lambda: c.get("/gmail/v1/users/me/labels"),
                "getProfile": lambda: c.get("/gmail/v1/users/me/profile"),
                "messages.send": lambda: c.post("/gmail/v1/users/me/messages/send", json={"raw": raw_message("scale")}),
                "messages.modify": lambda: c.post(f"/gmail/v1/users/me/messages/{mid}/modify", json={"addLabelIds": ["STARRED"]}),
            }
            res = {
                "seed_seconds": round(load_s, 1),
                "rss_mb": round(srv.rss_mb(), 1),
                "ops": {name: _time_op(fn, reps) for name, fn in ops.items()},
            }
            out[str(n)] = res
            print(f"  {n:>6} messages: seeded in {res['seed_seconds']}s, RSS {res['rss_mb']} MB")
            for name, r in res["ops"].items():
                print(f"      {name:<28} p50 {r['p50_ms']:>8} ms   p95 {r['p95_ms']:>8} ms")
        finally:
            srv.stop()
    return out


def phase_soak(args) -> dict:
    srv = Server()
    try:
        seed_mailbox(srv.url, 500)
        httpx.post(
            f"{srv.url}/gmail/v1/users/me/watch", json={"topicName": "projects/p/topics/t"}, headers=AUTH
        )  # publishes on every change
        samples = []
        stop = threading.Event()

        def sample():
            t0 = time.time()
            while not stop.is_set():
                samples.append({"t": round(time.time() - t0, 1), "rss_mb": round(srv.rss_mb(), 1), "cpu_pct": srv.ps.cpu_percent()})
                stop.wait(2)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        res = run_load(srv.url, WRITE_MIX, args.soak, 32)
        stop.set()
        th.join()
        profile = httpx.get(f"{srv.url}/gmail/v1/users/me/profile", headers=AUTH).json()
        published = len(httpx.get(f"{srv.url}/_mock/pubsub/published", timeout=60).json()["published"])
        rss = [s["rss_mb"] for s in samples]
        res.update(
            {
                "rss_start_mb": rss[0],
                "rss_end_mb": rss[-1],
                "rss_peak_mb": max(rss),
                "messages_at_end": profile["messagesTotal"],
                "notifications_retained": published,
                "samples": samples,
            }
        )
        grown = profile["messagesTotal"] - 500
        res["rss_kb_per_new_message"] = round((rss[-1] - rss[0]) * 1000 / max(grown, 1), 2)
        print(
            f"  {args.soak}s write-heavy @32: {res['rps']} req/s, p99 {res['p99_ms']} ms, errors {res['errors']} (5xx {res['server_errors']})"
        )
        print(
            f"  RSS {rss[0]} -> {rss[-1]} MB (peak {max(rss)}), +{grown} messages, {res['rss_kb_per_new_message']} KB/message, "
            f"{published} notifications retained in memory"
        )
        return res
    finally:
        srv.stop()


class _Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Receiver.received.append((time.time(), body))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def phase_push(args) -> dict:
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    push_url = f"http://127.0.0.1:{receiver.server_address[1]}/push"
    srv = Server("--push", f"projects/p/topics/t={push_url}")
    n = 1000 if not args.quick else 300
    try:
        c = httpx.Client(base_url=srv.url, headers=AUTH, timeout=60)
        c.post("/gmail/v1/users/me/watch", json={"topicName": "projects/p/topics/t"})
        _Receiver.received.clear()
        sent_at = {}

        async def fire():
            async with httpx.AsyncClient(base_url=srv.url, timeout=60) as ac:
                sem = asyncio.Semaphore(32)

                async def one(i):
                    async with sem:
                        sent_at[i] = time.time()
                        await ac.post(f"/_mock/users/{ME}/messages", json={"subject": f"push {i}"})

                await asyncio.gather(*(one(i) for i in range(n)))

        t0 = time.time()
        asyncio.run(fire())
        produce_s = time.time() - t0
        deadline = time.time() + 300
        while len(_Receiver.received) < n and time.time() < deadline:
            time.sleep(0.05)
        drain_s = time.time() - t0
        lat = []
        for ts, body in _Receiver.received:
            published = datetime.fromisoformat(body["message"]["publishTime"].replace("Z", "+00:00")).timestamp()
            lat.append((ts - published) * 1000)
        ids = [json.loads(base64.b64decode(b["message"]["data"]))["historyId"] for _, b in _Receiver.received]
        res = {
            "notifications": n,
            "delivered": len(_Receiver.received),
            "unique_history_ids": len(set(ids)),
            "produce_seconds": round(produce_s, 2),
            "all_delivered_seconds": round(drain_s, 2),
            "delivery_rate_per_s": round(len(_Receiver.received) / drain_s, 1),
            "publish_to_receive_p50_ms": round(pct(lat, 50), 1),
            "publish_to_receive_p99_ms": round(pct(lat, 99), 1),
        }
        print(
            f"  {res['delivered']}/{n} pushes delivered in {res['all_delivered_seconds']}s ({res['delivery_rate_per_s']}/s), "
            f"publish->receive p50 {res['publish_to_receive_p50_ms']} ms p99 {res['publish_to_receive_p99_ms']} ms"
        )
        return res
    finally:
        srv.stop()
        receiver.shutdown()


def phase_payload(args) -> dict:
    srv = Server()
    try:
        raw = raw_message("huge", size=25 * 1024 * 1024)
        before = srv.rss_mb()

        async def send_all():
            async with httpx.AsyncClient(base_url=srv.url, headers=AUTH, timeout=300) as ac:

                async def one():
                    t0 = time.perf_counter()
                    r = await ac.post("/gmail/v1/users/me/messages/send", json={"raw": raw})
                    return r.status_code, (time.perf_counter() - t0) * 1000

                return await asyncio.gather(*(one() for _ in range(8)))

        results = asyncio.run(send_all())
        mid = httpx.get(f"{srv.url}/gmail/v1/users/me/messages", headers=AUTH).json()["messages"][0]["id"]
        t0 = time.perf_counter()
        full = httpx.get(f"{srv.url}/gmail/v1/users/me/messages/{mid}", headers=AUTH, timeout=120)
        get_ms = (time.perf_counter() - t0) * 1000
        res = {
            "sends": len(results),
            "statuses": sorted({s for s, _ in results}),
            "send_p50_ms": round(pct([ms for _, ms in results], 50), 1),
            "send_max_ms": round(max(ms for _, ms in results), 1),
            "get_full_ms": round(get_ms, 1),
            "get_status": full.status_code,
            "rss_before_mb": round(before, 1),
            "rss_after_mb": round(srv.rss_mb(), 1),
        }
        print(
            f"  8 x 25 MB concurrent sends: statuses {res['statuses']}, p50 {res['send_p50_ms']} ms, max {res['send_max_ms']} ms; "
            f"get full {res['get_full_ms']} ms; RSS {res['rss_before_mb']} -> {res['rss_after_mb']} MB"
        )
        return res
    finally:
        srv.stop()


def phase_verify(args) -> dict:
    """Hammer one mailbox with concurrent writes, then check the state is exactly right."""
    srv = Server()
    try:
        workers, per = 64, 40
        start = httpx.get(f"{srv.url}/gmail/v1/users/me/profile", headers=AUTH).json()["historyId"]

        async def run():
            async with httpx.AsyncClient(base_url=srv.url, headers=AUTH, timeout=60, limits=httpx.Limits(max_connections=workers)) as ac:

                async def worker(w):
                    ok = []
                    for i in range(per):
                        r = await ac.post("/gmail/v1/users/me/messages/send", json={"raw": raw_message(f"v{w}-{i}")})
                        if r.status_code == 200:
                            mid = r.json()["id"]
                            ok.append(mid)
                            await ac.post(f"/gmail/v1/users/me/messages/{mid}/modify", json={"addLabelIds": ["STARRED"]})
                            if i % 4 == 0:
                                await ac.post(f"/gmail/v1/users/me/messages/{mid}/trash")
                    return ok

                return await asyncio.gather(*(worker(w) for w in range(workers)))

        ids = [m for ok in asyncio.run(run()) for m in ok]
        c = httpx.Client(base_url=srv.url, headers=AUTH, timeout=60)
        profile = c.get("/gmail/v1/users/me/profile").json()
        starred = c.get("/gmail/v1/users/me/labels/STARRED").json()["messagesTotal"]
        trash = c.get("/gmail/v1/users/me/labels/TRASH").json()["messagesTotal"]
        hist, token = [], None
        while True:
            body = c.get(
                "/gmail/v1/users/me/history", params={"startHistoryId": start, "maxResults": 500, **({"pageToken": token} if token else {})}
            ).json()
            hist += [int(h["id"]) for h in body.get("history", [])]
            token = body.get("nextPageToken")
            if not token:
                break
        log = c.get("/_mock/requests", params={"limit": 100000}).json()["requests"]
        expected = workers * per
        checks = {
            "all sends succeeded": len(ids) == expected,
            "ids unique": len(set(ids)) == len(ids),
            "profile.messagesTotal": profile["messagesTotal"] == expected,
            "STARRED count": starred == expected,
            "TRASH count": trash == workers * ((per + 3) // 4),
            "history strictly increasing": hist == sorted(set(hist)),
            "history record count": len(hist) == expected * 2 + workers * ((per + 3) // 4),
            "no 5xx in request log": not any(r["status"] >= 500 for r in log),
        }
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        return {"checks": checks, "messages": profile["messagesTotal"], "history_records": len(hist)}
    finally:
        srv.stop()


PHASES = {
    "throughput": phase_throughput,
    "scaling": phase_scaling,
    "soak": phase_soak,
    "push": phase_push,
    "payload": phase_payload,
    "verify": phase_verify,
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("phases", nargs="*", help=f"any of: {', '.join(PHASES)} (default: all)")
    p.add_argument("--duration", type=float, default=15, help="seconds per throughput level")
    p.add_argument("--soak", type=float, default=120, help="soak duration in seconds")
    p.add_argument("--quick", action="store_true")
    args = p.parse_args()
    unknown = set(args.phases) - set(PHASES)
    if unknown:
        p.error(f"unknown phase(s): {', '.join(sorted(unknown))}")
    args.phases = args.phases or list(PHASES)
    if args.quick:
        args.duration, args.soak = min(args.duration, 6), min(args.soak, 30)
    results = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "machine": {"cpus": os.cpu_count(), "python": sys.version.split()[0]},
    }
    for name in args.phases:
        print(f"\n== {name} ==")
        t0 = time.time()
        results[name] = PHASES[name](args)
        results[name + "_seconds"] = round(time.time() - t0, 1)
    out = ROOT / "stress" / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nresults -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
