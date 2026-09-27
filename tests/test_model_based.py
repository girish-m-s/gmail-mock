"""Model-based testing: random operation sequences checked against a tiny reference model.

Hypothesis generates sequences of Gmail operations (receive, send, modify, trash, untrash,
delete, label create/delete, drafts). After every step the mock's observable state
(list results, search, label counts, profile, threads, history) must match the model.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from gmail_mock import mime
from gmail_mock.app import create_app
from gmail_mock.store import Store

ME = "me@example.com"
MUTABLE = ["STARRED", "UNREAD", "IMPORTANT", "SPAM"]
SUBJECTS = st.sampled_from(["alpha", "beta", "gamma", "Re: alpha", "délta ✓", "日本語"])


class GmailMachine(RuleBasedStateMachine):
    messages = Bundle("messages")
    user_labels = Bundle("labels")
    drafts = Bundle("drafts")

    def __init__(self) -> None:
        super().__init__()
        self.client = TestClient(create_app(Store(ME)), headers={"Authorization": f"Bearer {ME}"})
        self.model: dict[str, set[str]] = {}  # message id -> labels
        self.threads: dict[str, str] = {}  # message id -> thread id
        self.label_names: dict[str, str] = {}
        self.draft_ids: set[str] = set()
        self.last_history = 0

    def api(self, method: str, path: str, **kw):
        resp = self.client.request(method, "/gmail/v1/users/me" + path, **kw)
        assert resp.status_code < 500, resp.text
        return resp

    # --- rules -----------------------------------------------------------------------------

    @rule(target=messages, subject=SUBJECTS, unread=st.booleans())
    def receive(self, subject, unread):
        labels = ["INBOX", "CATEGORY_PERSONAL"] + (["UNREAD"] if unread else [])
        resp = self.client.post(
            f"/_mock/users/{ME}/messages", json={"from": "x@example.org", "subject": subject, "text": subject, "labelIds": labels}
        )
        assert resp.status_code == 201
        m = resp.json()
        self.model[m["id"]] = set(labels)
        self.threads[m["id"]] = m["threadId"]
        return m["id"]

    @rule(target=messages, subject=SUBJECTS)
    def send(self, subject):
        raw = base64.urlsafe_b64encode(mime.compose(sender=ME, to=["out@example.org"], subject=subject, text="hi")).decode()
        m = self.api("POST", "/messages/send", json={"raw": raw}).json()
        assert m["labelIds"] == ["SENT"]
        self.model[m["id"]] = {"SENT"}
        self.threads[m["id"]] = m["threadId"]
        return m["id"]

    @rule(target=user_labels, name=st.text(alphabet="abcxyz/ ", min_size=1, max_size=6))
    def create_label(self, name):
        resp = self.api("POST", "/labels", json={"name": name})
        taken = {n.lower() for n in self.label_names.values()}
        stripped = name.strip()
        if (
            not stripped
            or stripped.lower() in taken
            or stripped.lower() in {"inbox", "sent", "trash", "spam", "draft", "starred", "unread", "important"}
        ):
            assert resp.status_code in (400, 409)
            return "Label_missing"
        assert resp.status_code == 200, resp.text
        lid = resp.json()["id"]
        self.label_names[lid] = stripped
        return lid

    @rule(
        mid=messages,
        add=st.lists(st.sampled_from(MUTABLE), max_size=2),
        remove=st.lists(st.sampled_from(MUTABLE), max_size=2),
        label=st.none() | user_labels,
    )
    def modify(self, mid, add, remove, label):
        add = add + ([label] if label else [])
        resp = self.api("POST", f"/messages/{mid}/modify", json={"addLabelIds": add, "removeLabelIds": remove})
        if mid not in self.model:
            assert resp.status_code == 404
            return
        if any(lbl not in self.label_names and lbl not in MUTABLE for lbl in add):
            assert resp.status_code == 400
            return
        assert resp.status_code == 200, resp.text
        labels = self.model[mid] | set(add)
        labels -= {lbl for lbl in remove if lbl not in add}
        self.model[mid] = labels
        assert set(resp.json()["labelIds"]) == labels

    @rule(mid=messages)
    def trash(self, mid):
        resp = self.api("POST", f"/messages/{mid}/trash")
        if mid not in self.model:
            assert resp.status_code == 404
            return
        if "TRASH" not in self.model[mid]:
            self.model[mid] = (self.model[mid] | {"TRASH"}) - {"INBOX", "SPAM"}
        assert set(resp.json()["labelIds"]) == self.model[mid]

    @rule(mid=messages)
    def untrash(self, mid):
        resp = self.api("POST", f"/messages/{mid}/untrash")
        if mid not in self.model:
            assert resp.status_code == 404
            return
        assert resp.status_code == 200
        self.model[mid] = set(resp.json()["labelIds"])
        assert "TRASH" not in self.model[mid]

    @rule(mid=consumes(messages))
    def delete(self, mid):
        resp = self.api("DELETE", f"/messages/{mid}")
        if mid in self.model:
            assert resp.status_code == 204
            del self.model[mid]
        else:
            assert resp.status_code == 404

    @rule(lid=consumes(user_labels))
    def delete_label(self, lid):
        resp = self.api("DELETE", f"/labels/{lid}")
        if lid in self.label_names:
            assert resp.status_code == 204
            del self.label_names[lid]
            for labels in self.model.values():
                labels.discard(lid)
        else:
            assert resp.status_code == 404

    @rule(target=drafts, subject=SUBJECTS)
    def create_draft(self, subject):
        raw = base64.urlsafe_b64encode(mime.compose(sender=ME, to=["d@example.org"], subject=subject, text="draft")).decode()
        d = self.api("POST", "/drafts", json={"message": {"raw": raw}}).json()
        self.model[d["message"]["id"]] = {"DRAFT"}
        self.threads[d["message"]["id"]] = d["message"]["threadId"]
        self.draft_ids.add(d["id"])
        return (d["id"], d["message"]["id"])

    @rule(draft=consumes(drafts))
    def send_draft(self, draft):
        draft_id, message_id = draft
        resp = self.api("POST", "/drafts/send", json={"id": draft_id})
        if draft_id not in self.draft_ids or message_id not in self.model:
            assert resp.status_code == 404
            self.draft_ids.discard(draft_id)
            return
        assert resp.status_code == 200
        self.draft_ids.discard(draft_id)
        del self.model[message_id]
        sent = resp.json()
        self.model[sent["id"]] = {"SENT"}
        self.threads[sent["id"]] = sent["threadId"]

    # --- invariants ------------------------------------------------------------------------

    def _list_all(self, **params) -> set[str]:
        out, token = set(), None
        while True:
            body = self.api("GET", "/messages", params={**params, "maxResults": 7, **({"pageToken": token} if token else {})}).json()
            out |= {m["id"] for m in body.get("messages", [])}
            token = body.get("nextPageToken")
            if not token:
                return out

    @invariant()
    def lists_match_model(self):
        assert self._list_all(includeSpamTrash="true") == set(self.model)
        visible = {mid for mid, labels in self.model.items() if not labels & {"TRASH", "SPAM"}}
        assert self._list_all() == visible
        unread = {mid for mid in visible if "UNREAD" in self.model[mid]}
        assert self._list_all(q="is:unread") == unread

    @invariant()
    def label_counts_match_model(self):
        for lid in ["INBOX", "UNREAD", "STARRED", "TRASH", *self.label_names]:
            label = self.api("GET", f"/labels/{lid}").json()
            members = [mid for mid, labels in self.model.items() if lid in labels]
            assert label["messagesTotal"] == len(members), lid
            assert label["threadsTotal"] == len({self.threads[m] for m in members}), lid

    @invariant()
    def profile_and_history_are_consistent(self):
        profile = self.api("GET", "/profile").json()
        assert profile["messagesTotal"] == len(self.model)
        assert profile["threadsTotal"] == len({self.threads[m] for m in self.model})
        history_id = int(profile["historyId"])
        assert history_id >= self.last_history
        history = self.api("GET", "/history", params={"startHistoryId": "100000", "maxResults": 500}).json()
        ids = [int(h["id"]) for h in history.get("history", [])]
        assert ids == sorted(set(ids)), "history ids must be unique and increasing"
        assert not ids or ids[-1] == history_id
        self.last_history = history_id

    @invariant()
    def drafts_match_model(self):
        listed = self.api("GET", "/drafts").json().get("drafts", [])
        assert {d["id"] for d in listed} == self.draft_ids


TestGmailModel = GmailMachine.TestCase
TestGmailModel.settings = settings(max_examples=60, stateful_step_count=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
