# Stress testing

`loadtest.py` starts the real server (`python -m gmail_mock`) in a subprocess and drives it with a
multi-process asyncio load generator.

```bash
uv run python stress/loadtest.py                    # all phases (~6 min)
uv run python stress/loadtest.py --quick            # shorter
uv run python stress/loadtest.py throughput soak    # selected phases
```

| Phase | What it does |
| --- | --- |
| `throughput` | Mixed, agent-like workload (list, get, search, threads, history, send, modify, drafts, batch) at concurrency 1 → 128 against a 2k-message mailbox |
| `scaling` | Per-endpoint latency with 1k, 10k and 50k messages |
| `soak` | Write-heavy load at concurrency 32 with an active `watch`, sampling server RSS and CPU |
| `push` | 1,000 concurrent incoming messages with a push subscription: delivery rate and publish → receive latency |
| `payload` | 8 concurrent 25 MB sends |
| `verify` | 64 concurrent writers (2,560 sends, plus a modify on each and a trash on every fourth), then checks that counts, history ordering and the request log are exactly right |

## Results (2026-09-27)

Machine: 14-core Apple Silicon Mac, Python 3.12. The load generator runs on the same machine.
The raw numbers are in [`results/baseline-2026-09-27.json`](results/baseline-2026-09-27.json).

**About 400k requests across all phases: 0 errors, 0 5xx, and every correctness check passed.**

### Throughput (mixed workload, 2k-message mailbox)

| Concurrency | req/s | p50 | p95 | p99 | Errors |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 676 | 0.96 ms | 2.9 ms | 5.6 ms | 0 |
| 8 | 1,663 | 3.3 ms | 9.7 ms | 13.8 ms | 0 |
| 32 | 1,800 | 15.1 ms | 26.2 ms | 34.9 ms | 0 |
| 64 | 1,749 | 31.3 ms | 46.6 ms | 65.2 ms | 0 |
| 128 | 1,763 | 61.0 ms | 90.0 ms | 120.4 ms | 0 |

The server is a single process (state lives in memory), so it saturates one CPU core at about 1.7–1.8k req/s.
Beyond that point, latency grows with queue depth, but requests do not fail.

### Mailbox size (p50, single client)

| Operation | 1k | 10k | 50k |
| --- | ---: | ---: | ---: |
| `messages.list` (any page) | 0.49 ms | 0.49 ms | 0.51 ms |
| `q=is:unread` (label-only queries) | 0.49 ms | 0.57 ms | 1.5 ms |
| `q="free text"` | 0.75 ms | 4.4 ms | 28.5 ms |
| `q=from:alice OR from:bob` | 1.2 ms | 9.8 ms | 52.4 ms |
| `threads.list` | 0.66 ms | 2.8 ms | 21.2 ms |
| `labels.get` (with counts) | 0.44 ms | 0.85 ms | 8.7 ms |
| `messages.get`, `threads.get`, `getProfile` | ~0.4 ms | ~0.4 ms | ~0.4 ms |
| `messages.send` | 1.4 ms | 1.4 ms | 1.5 ms |

Seeding 50k messages takes 34 s. The server then uses 260 MB of memory.

### Soak (120 s, write-heavy, concurrency 32, watch active)

- 260,904 requests at 2,170 req/s, with a p99 of 30 ms and 0 errors.
- 130k messages created. Memory grew linearly at about 5.2 KB per message.
- Published notifications are capped at 10k in memory, and history at 100k records per mailbox
  (`--history-limit`). Older `startHistoryId` values get a 404, as in Gmail.

### Push notifications

- 1,000 of 1,000 delivered, with unique `historyId`s, at about 295/s. The rate was limited by how fast the test produced mail.
- Publish → receive latency: p50 1.7 ms, p99 629 ms, at the peak of the burst.

### Large payloads

- 8 concurrent 25 MB sends: all returned 200, with a median of 6.2 s. `messages.get` on one of them takes about 1 s.
- Memory kept afterwards is roughly the stored message size. While an upload is being processed, it needs
  about 4× its size, so at most 2 large bodies are decoded at a time.

## What the stress testing found and fixed

| Finding | Fix | Effect |
| --- | --- | --- |
| Throughput capped at 670 req/s; p99 2.1 s at concurrency 128 | Google API requests skip FastAPI routing (raw ASGI middleware); uvloop and httptools; compact JSON via the C encoder; serialize under the lock instead of `deepcopy` | 1.76k req/s; p99 120 ms |
| `messages.list`, label counts and `is:unread` scanned and sorted every message on each call | Sorted `(internalDate, id)` index, label → message and label → thread indexes, and label-only queries answered from the indexes | 50k list: 50 → 0.5 ms; `is:unread`: 39 → 1.5 ms; `labels.get`: 37 → 8.7 ms |
| Published-notification log and history grew without limit | Bounded deques and `--history-limit` (default 100k) | Memory is flat apart from stored mail |
| The request log kept every request body, including 47 MB base64 strings | Long values are abbreviated in the log | About 48 MB less memory kept per large request |
| A 25 MB send re-parsed and re-serialized the whole message twice | Header-only edits for From, Date, Message-ID and Bcc; no cached parse for messages over 1 MB; large bodies processed in a thread with a limit of 2 | 17.7 s → 6.2 s p50 |
| Every `PubSub` started 8 threads with HTTP clients that never stopped (a leak when building many stores) | Workers start on the first push; added `PubSub.close()` | Model-test run: 61 → 21 s |
| Push delivery used a single thread | A pool of 8 workers | p50 publish → receive: 128 → 1.7 ms |

## Known limits

- The server runs as one process by design, because all state is in memory. For more throughput, run one mock per test worker.
- Free-text and address searches are linear scans, about 30–50 ms at 50k messages. There is no full-text index.
- Large uploads need memory proportional to the upload volume, and macOS does not return freed memory to the OS quickly.
