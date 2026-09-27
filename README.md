# gmail-mock

gmail-mock is a **stateful** mock HTTP server that behaves like the real
[Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest). It accepts the same requests and
parameters, rejects requests Google would reject, and keeps a real in-memory mailbox. So a message you
send shows up in `messages.list`, a label you add changes `labels.get` counts, and every change is written
to `history` and published as a **Gmail push notification** through a Pub/Sub stand-in.

It is modeled on [stripe-mock](https://github.com/stripe/stripe-mock): the route catalog and request
validation come from Google's published API discovery documents, which are bundled in
[`src/gmail_mock/discovery`](src/gmail_mock/discovery). Unlike stripe-mock, it keeps state, because
agents and integrations built on Gmail connectors depend on stateful behaviour: threads, labels, drafts and
history-driven triggers.

Use it to test code that talks to Gmail, such as AI agents using Gmail tools, connector implementations and sync
workers, without a Google account, OAuth, quotas or flaky network calls.

## Features

- **The whole Gmail v1 REST API**: all 79 methods (messages, threads, drafts, labels, history, watch/stop,
  profile, and every `settings.*` resource, including send-as, S/MIME, forwarding, delegates, filters and CSE).
  Every method has a stateful implementation.
- **The People API subset Gmail connectors use**: contacts, "other contacts" and contact groups. Unknown
  recipients you email are added to *Other contacts*, as in Gmail.
- **Realistic mail behaviour**:
  - RFC 822 parsing into Gmail's `MessagePart` tree, with `full`/`metadata`/`minimal`/`raw` formats and attachment ids.
  - Gmail threading: `threadId` plus a matching subject, or `References`/`In-Reply-To`.
  - Mail sent between two mailboxes on the mock is delivered to the recipient. Bcc headers are removed from the delivered copy.
  - `From` is rewritten to a verified send-as address.
  - Filters apply to incoming mail.
  - Trash/untrash keeps the right labels.
- **Gmail search (`q`)**: free text, phrases, `-`, `OR`, `()`/`{}`, and the operators `from:`, `to:`, `cc:`, `bcc:`,
  `subject:`, `label:`, `in:`, `is:`, `has:attachment`, `filename:`, `category:`, `after:`/`before:`,
  `newer_than:`/`older_than:`, `larger:`/`smaller:`, `rfc822msgid:` and `list:`.
- **Push notifications, the way Google does it**: `users.watch` → Pub/Sub message
  `{"emailAddress", "historyId"}` → your code calls `history.list`. Delivery options:
  - push subscriptions, using the real Pub/Sub push envelope;
  - pull through a Pub/Sub REST subset;
  - forwarding to a real [Pub/Sub emulator](https://cloud.google.com/pubsub/docs/emulator).
- **Google front-end behaviour**:
  - Google-shaped error JSON.
  - 401 without a bearer token.
  - 400 for unknown query parameters, unknown JSON fields, bad types or enums, and bad ids.
  - `fields` partial responses and `prettyPrint=true`. Responses are compact JSON unless you ask for pretty output.
  - snake_case field aliases.
  - Multipart media uploads (`/upload/...`) and `/batch` requests.
- **Test controls** under `/_mock`: simulate incoming mail, seed fixtures, inject faults (e.g. a 429 on
  `messages.send`), read the request log, inspect published notifications, and reset state.
- HTTP and HTTPS (self-signed) on fixed or random ports, a Unix socket option, and a Docker image.

## Limitations

- State is in memory and is lost on restart. Use `--seed` or `POST /_mock/seed` to preload data.
- Spam classification, vacation auto-replies, real outbound email, and verification emails for
  forwarding and send-as addresses are not simulated. Verification succeeds through the API or `/_mock`.
- Resumable uploads (`uploadType=resumable`) return 501. Use `multipart` or `media`, or send `raw` in JSON.
- The search syntax is a practical subset, and text matching is substring-based, not tokenized like Gmail.
- It is not Gmail. Always verify production behaviour against a real (test) account.

## Usage

```bash
uv tool install git+https://github.com/girish-m-s/gmail-mock   # or: pip install git+https://...
gmail-mock                                                   # HTTP :12411, HTTPS :12412
```

```text
gmail-mock --http-port 12411 --https-port 12412   # defaults
gmail-mock --http-port 0                          # pick a free port (printed on startup)
gmail-mock --http-unix /tmp/gmail-mock.sock
gmail-mock --seed examples/seed.json              # preload mailboxes
gmail-mock --email alice@example.com              # default mailbox for non-email tokens
gmail-mock --push projects/p/topics/gmail=http://localhost:8080/push
gmail-mock --pubsub-emulator-host localhost:8085  # also publish to the gcloud emulator
gmail-mock --no-auth                              # don't require Authorization
gmail-mock --history-limit 100000                 # history records kept per mailbox (older startHistoryId -> 404)
```

### Docker

```bash
docker build -t gmail-mock .
docker run --rm -p 12411-12412:12411-12412 gmail-mock --seed examples/seed.json
```

Tagged releases publish `ghcr.io/girish-m-s/gmail-mock` (see `.github/workflows/release.yml`).

### Sample request

Any bearer token is accepted. **If the token is an email address, it selects that mailbox**. Any other
token uses the default mailbox (`me@example.com`).

```bash
curl -s http://localhost:12411/gmail/v1/users/me/profile -H "Authorization: Bearer me@example.com"
```

### With google-api-python-client

```python
from gmail_mock.client import build_service

gmail = build_service("gmail", "http://localhost:12411", token="me@example.com")
people = build_service("people", "http://localhost:12411", token="me@example.com")
gmail.users().messages().list(userId="me", q="is:unread").execute()
```

`build_service` is `googleapiclient.discovery.build` with `client_options={"api_endpoint": ...}`, plus one
fix: the Google client sends batch requests and media uploads to `https://*.googleapis.com`
even when you override the endpoint, so the helper routes those to the mock as well. Other clients
(Node `googleapis`, Go, raw HTTP) only need their base URL pointed at the mock. The paths are the same as
Google's: `/gmail/v1/...`, `/upload/gmail/v1/...`, `/v1/people/...`, `/batch`. The discovery document is served at
`/discovery/v1/apis/gmail/v1/rest`, with `rootUrl` rewritten to the mock.

## Triggers (push notifications)

Gmail connectors typically expose two triggers, both built on this Gmail mechanism, and the mock reproduces it:

| Trigger | What fires it | What your code sees |
| --- | --- | --- |
| **New Gmail Message Received** | mail delivered to the mailbox (from another mock mailbox, `messages.import`/`insert`, or `POST /_mock/users/{email}/messages`) | Pub/Sub push → `history.list` shows `messagesAdded` with `INBOX` |
| **Email Sent** | `messages.send` or `drafts.send` | Pub/Sub push → `history.list` shows `messagesAdded` with `SENT` |

```bash
# 1. where Pub/Sub should push (or create a pull subscription with PUT /v1/projects/p/subscriptions/s)
curl -X POST localhost:12411/_mock/pubsub/subscriptions -H 'content-type: application/json' \
  -d '{"name":"projects/demo/subscriptions/push","topic":"projects/demo/topics/gmail","pushEndpoint":"http://localhost:8080/push"}'
# 2. watch the mailbox (the topic is created on demand)
curl -X POST localhost:12411/gmail/v1/users/me/watch -H 'Authorization: Bearer me@example.com' \
  -H 'content-type: application/json' -d '{"topicName":"projects/demo/topics/gmail","labelIds":["INBOX"]}'
# 3. simulate an incoming email
curl -X POST localhost:12411/_mock/users/me@example.com/messages -H 'content-type: application/json' \
  -d '{"from":"Carol <carol@example.org>","subject":"Lunch?","text":"12:30?"}'
```

Your endpoint receives the standard push envelope
`{"message": {"data": base64({"emailAddress": "...", "historyId": 100001}), "messageId": "...", "publishTime": "..."}, "subscription": "..."}`.
[`examples/triggers_demo.py`](examples/triggers_demo.py) runs the whole loop for both triggers.
`labelIds` and `labelFilterBehavior` (`include`/`exclude`) filter notifications as in Gmail.

## Control API (`/_mock`)

Interactive docs are at `/_mock/docs`.

| Endpoint | Purpose |
| --- | --- |
| `GET /_mock/health` | Liveness check |
| `POST /_mock/reset` | Wipe all state (mailboxes, Pub/Sub, faults, request log) |
| `GET/POST /_mock/users` | List or create mailboxes |
| `POST /_mock/users/{email}/messages` | Deliver an incoming message (`from`, `to`, `cc`, `subject`, `text`, `html`, `attachments`, `labelIds`, `inReplyTo`, `raw`) |
| `POST /_mock/seed` | Load fixtures (same format as `--seed`) |
| `GET/POST/DELETE /_mock/faults` | Inject errors: `{"methodId": "gmail.users.messages.send", "status": 429, "count": 2}` |
| `GET/DELETE /_mock/requests` | Request log (filter with `?methodId=`), useful for asserting what an agent called |
| `GET /_mock/pubsub/published`, `GET /_mock/pubsub/deliveries` | Notifications published and push delivery results |
| `POST /_mock/pubsub/subscriptions` | Create a push or pull subscription |
| `POST /_mock/users/{email}/forwardingAddresses/{address}/verify` | Accept a pending forwarding address |
| `GET /_mock/coverage` | Every discovery method and whether it has a stateful handler |

The Pub/Sub REST subset (`PUT/GET/DELETE /v1/projects/{p}/topics|subscriptions/{name}`, `:publish`, `:pull` and
`:acknowledge`) is served on the same port.

## Seed data

See [`examples/seed.json`](examples/seed.json). Each user can have `labels`, `messages` (with `from`/`to`/`subject`/
`text`/`html`/`attachments`/`labels`/`date`/`headers`, and `replyTo: <index>` to thread a reply), `drafts`,
`contacts`, `otherContacts`, `filters` (label names are resolved to ids) and `settings`. Seeding does not create history
entries, so the history log starts empty after seeding.

## Connector tool coverage

[`connectors/gmail_tools.json`](connectors/gmail_tools.json) maps 59 common Gmail connector tools and 2 triggers to the Google API methods behind them (including the People API for contacts). `tests/test_catalog.py` checks that every listed method is implemented.

<details>
<summary>Tool → Google API methods</summary>

| Tool | Methods |
| --- | --- |
| Modify email labels | `gmail.users.messages.modify` |
| Batch delete Gmail messages | `gmail.users.messages.batchDelete` |
| Batch modify Gmail messages | `gmail.users.messages.batchModify` |
| Create email draft | `gmail.users.drafts.create` |
| Create Gmail filter | `gmail.users.settings.filters.create` |
| Create label | `gmail.users.labels.create` |
| Delete Draft | `gmail.users.drafts.delete` |
| Delete Gmail filter | `gmail.users.settings.filters.delete` |
| Delete label from account (permanent) | `gmail.users.labels.delete` |
| Delete message | `gmail.users.messages.delete` |
| Delete thread | `gmail.users.threads.delete` |
| Fetch emails | `gmail.users.messages.list`, `gmail.users.messages.get` |
| Fetch message by message ID | `gmail.users.messages.get` |
| Fetch Message by Thread ID | `gmail.users.threads.get` |
| Forward email message | `gmail.users.messages.get`, `gmail.users.messages.attachments.get`, `gmail.users.messages.send` |
| Get Gmail attachment | `gmail.users.messages.attachments.get` |
| Get Auto-Forwarding Settings | `gmail.users.settings.getAutoForwarding` |
| Get contacts | `people.people.connections.list` |
| Get Draft | `gmail.users.drafts.get` |
| Get Gmail filter | `gmail.users.settings.filters.get` |
| Get label details | `gmail.users.labels.get` |
| Get Language Settings | `gmail.users.settings.getLanguage` |
| Get People | `people.people.get`, `people.otherContacts.list` |
| Get Profile | `gmail.users.getProfile` |
| Get Vacation Settings | `gmail.users.settings.getVacation` |
| Import message | `gmail.users.messages.import` |
| Insert message into mailbox | `gmail.users.messages.insert` |
| List CSE identities | `gmail.users.settings.cse.identities.list` |
| List CSE key pairs | `gmail.users.settings.cse.keypairs.list` |
| List Drafts | `gmail.users.drafts.list` |
| List Gmail filters | `gmail.users.settings.filters.list` |
| List forwarding addresses | `gmail.users.settings.forwardingAddresses.list` |
| List Gmail history | `gmail.users.history.list` |
| List Gmail labels | `gmail.users.labels.list` |
| List send-as aliases | `gmail.users.settings.sendAs.list` |
| List S/MIME configs | `gmail.users.settings.sendAs.smimeInfo.list` |
| List threads | `gmail.users.threads.list` |
| Modify thread labels | `gmail.users.threads.modify` |
| Trash thread | `gmail.users.threads.trash` |
| Move to Trash | `gmail.users.messages.trash` |
| Patch Label | `gmail.users.labels.patch` |
| Patch send-as alias | `gmail.users.settings.sendAs.patch` |
| Reply to email thread | `gmail.users.threads.get`, `gmail.users.messages.send` |
| Search People | `people.people.searchContacts`, `people.otherContacts.search` |
| Send Draft | `gmail.users.drafts.send` |
| Send Email | `gmail.users.messages.send` |
| Get IMAP Settings | `gmail.users.settings.getImap` |
| Get POP settings | `gmail.users.settings.getPop` |
| Get send-as alias | `gmail.users.settings.sendAs.get` |
| Stop watch notifications | `gmail.users.stop` |
| Untrash Message | `gmail.users.messages.untrash` |
| Untrash thread | `gmail.users.threads.untrash` |
| Update draft | `gmail.users.drafts.update` |
| Update IMAP settings | `gmail.users.settings.updateImap` |
| Update Label | `gmail.users.labels.update` |
| Update Language Settings | `gmail.users.settings.updateLanguage` |
| Update POP settings | `gmail.users.settings.updatePop` |
| Update send-as alias | `gmail.users.settings.sendAs.update` |
| Update Vacation Settings | `gmail.users.settings.updateVacation` |

</details>

## Testing and performance

The test suite has 521 tests and runs in about 40 seconds:

| Layer | What it covers |
| --- | --- |
| Contract (`test_contract.py`) | One happy-path scenario for each of the 103 methods. Also generated for every method: 401 without auth, 400 for unknown query parameters, 400 for non-object or unknown-field bodies, and 404 for unknown ids |
| Response schema | Every JSON response in every test is checked strictly against the discovery schema: unknown fields, types, int64-as-string, enums, base64 and timestamps. Error bodies must match Google's shape. Any 5xx fails the test |
| Model-based (`test_model_based.py`) | Hypothesis generates random sequences of receive, send, modify, trash, untrash, delete, label and draft operations. After every step, lists, search, label counts, profile, history and drafts must match a reference model |
| Behaviour | Messages, threads, drafts, labels, settings, People, both triggers, batch, uploads and faults, all tested through the official `google-api-python-client` |
| Edge cases | RFC 2047 and unicode, LF-only and unpadded base64, malformed MIME, nested and forwarded mail, 10 MB attachments, paging limits, 60-message threads, and concurrent writers and notifications |

[`stress/`](stress/README.md) has a load generator for throughput, mailbox scaling, soak, push, large-payload and
correctness-under-load runs. On a laptop, the single-process server handles about **1.7k mixed req/s** with 0 errors,
lists 50k-message mailboxes in about **0.5 ms**, and ran a 120 s soak (260k requests, 130k messages) with zero failures.
See [stress/README.md](stress/README.md) for the numbers and the fixes they led to.

## Development

```bash
uv sync            # install with dev dependencies
make test          # pytest, driven by the official google-api-python-client
make stress        # load / soak / scaling run (see stress/README.md)
make lint          # ruff
make run           # start with examples/seed.json
make update-spec   # refresh the bundled discovery documents
```

How it fits together:

| Module | Role |
| --- | --- |
| `spec.py` | Builds the route catalog from the discovery docs |
| `validate.py` | Query and body validation |
| `dispatch.py` | Auth, fault injection, handler or schema fallback, `fields`, batch |
| `store.py` | Mailboxes, history, and watch notifications |
| `handlers/` | One function per discovery method id |
| `pubsub.py` | Push, pull and emulator delivery |
| `app.py` | FastAPI wiring and the `/_mock` API |

A discovery method without a handler still works, stripe-mock style: `generate.py` returns a response of the
right shape and copies back matching request fields. Today only the two People contact-photo methods use that fallback.

## License

MIT
