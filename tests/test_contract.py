"""One happy-path scenario per discovery method, plus generated negative cases for every method.

Responses are validated against the discovery schema by the conftest instrumentation,
so each scenario doubles as a response-contract test.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import pytest
from conftest import ME

from gmail_mock import mime
from gmail_mock.handlers.people import create_contact, new_other_contact
from gmail_mock.spec import Catalog

CATALOG = Catalog()
METHODS = CATALOG.methods
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def raw(subject="Hello", to="friend@example.org", **kw) -> str:
    return base64.urlsafe_b64encode(mime.compose(sender=ME, to=[to], subject=subject, text="body", **kw)).decode()


@dataclass
class World:
    msg: str = ""
    msg2: str = ""
    thread: str = ""
    attachment: str = ""
    draft: str = ""
    label: str = ""
    filter: str = ""
    smime: str = ""
    keypair: str = ""
    contact: str = ""
    other: str = ""
    group: str = ""
    extra: dict = field(default_factory=dict)


@pytest.fixture
def world(store) -> World:
    w = World()
    with store.lock:
        box = store.ensure_mailbox(ME)
        incoming = mime.compose(
            sender="Alice <alice@example.org>",
            to=[ME],
            subject="Report",
            text="see attached",
            attachments=[("r.csv", "text/csv", b"a,b\n1,2\n")],
        )
        m = box.receive(incoming)
        w.msg, w.thread = m.id, m.thread_id
        w.attachment = next(iter(m.attachments))
        w.msg2 = box.receive(mime.compose(sender="bob@example.org", to=[ME], subject="Other", text="x")).id
        w.draft, _ = box.create_draft(base64.urlsafe_b64decode(raw("Draft")))
        w.label = box.create_label({"name": "Work"})["id"]
        w.filter = store.ids.filter()
        box.filters[w.filter] = {"id": w.filter, "criteria": {"from": "x@example.org"}, "action": {"addLabelIds": ["STARRED"]}}
        box.send_as["sales@example.com"] = {
            "sendAsEmail": "sales@example.com",
            "displayName": "Sales",
            "replyToAddress": "",
            "signature": "",
            "isPrimary": False,
            "isDefault": False,
            "treatAsAlias": True,
            "verificationStatus": "pending",
        }
        w.smime = "smime1"
        box.smime[ME] = {"smime1": {"id": "smime1", "issuerCn": "CA", "isDefault": True, "expiration": "1", "pem": PEM}}
        box.forwarding["fwd@example.org"] = {"forwardingEmail": "fwd@example.org", "verificationStatus": "accepted"}
        box.delegates["assistant@example.com"] = {"delegateEmail": "assistant@example.com", "verificationStatus": "accepted"}
        w.keypair = "kp1"
        box.cse_keypairs["kp1"] = {"keyPairId": "kp1", "pkcs7": "MIIB", "subjectEmailAddresses": [ME], "enablementState": "enabled"}
        box.cse_identities[ME] = {"emailAddress": ME, "primaryKeyPairId": "kp1"}
        w.contact = create_contact(box, {"names": [{"givenName": "Carol"}], "emailAddresses": [{"value": "carol@example.org"}]})[
            "resourceName"
        ]
        w.other = new_other_contact(box, "Dan", "dan@example.org")["resourceName"]
        w.group = "contactGroups/friends"
        box.watch = None
    return w


# method id -> (path params, query, body, expected status). Callables receive the World.
S = {
    # users
    "gmail.users.getProfile": lambda w: ({}, {}, None, 200),
    "gmail.users.watch": lambda w: (
        {},
        {},
        {"topicName": "projects/p/topics/t", "labelIds": ["INBOX"], "labelFilterBehavior": "include"},
        200,
    ),
    "gmail.users.stop": lambda w: ({}, {}, None, 204),
    "gmail.users.history.list": lambda w: ({}, {"startHistoryId": "100000", "historyTypes": "messageAdded"}, None, 200),
    # messages
    "gmail.users.messages.list": lambda w: ({}, {"q": "from:alice has:attachment", "maxResults": "10"}, None, 200),
    "gmail.users.messages.get": lambda w: ({"id": w.msg}, {"format": "full"}, None, 200),
    "gmail.users.messages.attachments.get": lambda w: ({"messageId": w.msg, "id": w.attachment}, {}, None, 200),
    "gmail.users.messages.send": lambda w: ({}, {}, {"raw": raw()}, 200),
    "gmail.users.messages.insert": lambda w: ({}, {"internalDateSource": "dateHeader"}, {"raw": raw(), "labelIds": ["INBOX"]}, 200),
    "gmail.users.messages.import": lambda w: ({}, {"neverMarkSpam": "true"}, {"raw": raw(), "labelIds": ["INBOX", "UNREAD"]}, 200),
    "gmail.users.messages.modify": lambda w: ({"id": w.msg}, {}, {"addLabelIds": [w.label], "removeLabelIds": ["UNREAD"]}, 200),
    "gmail.users.messages.batchModify": lambda w: ({}, {}, {"ids": [w.msg, w.msg2], "addLabelIds": ["STARRED"]}, 204),
    "gmail.users.messages.batchDelete": lambda w: ({}, {}, {"ids": [w.msg2]}, 204),
    "gmail.users.messages.delete": lambda w: ({"id": w.msg2}, {}, None, 204),
    "gmail.users.messages.trash": lambda w: ({"id": w.msg}, {}, None, 200),
    "gmail.users.messages.untrash": lambda w: ({"id": w.msg}, {}, None, 200),
    # threads
    "gmail.users.threads.list": lambda w: ({}, {"labelIds": "INBOX"}, None, 200),
    "gmail.users.threads.get": lambda w: ({"id": w.thread}, {"format": "metadata", "metadataHeaders": "Subject"}, None, 200),
    "gmail.users.threads.modify": lambda w: ({"id": w.thread}, {}, {"addLabelIds": ["IMPORTANT"]}, 200),
    "gmail.users.threads.trash": lambda w: ({"id": w.thread}, {}, None, 200),
    "gmail.users.threads.untrash": lambda w: ({"id": w.thread}, {}, None, 200),
    "gmail.users.threads.delete": lambda w: ({"id": w.thread}, {}, None, 204),
    # drafts
    "gmail.users.drafts.list": lambda w: ({}, {"q": "draft"}, None, 200),
    "gmail.users.drafts.get": lambda w: ({"id": w.draft}, {"format": "raw"}, None, 200),
    "gmail.users.drafts.create": lambda w: ({}, {}, {"message": {"raw": raw("New draft")}}, 200),
    "gmail.users.drafts.update": lambda w: ({"id": w.draft}, {}, {"id": w.draft, "message": {"raw": raw("Draft")}}, 200),
    "gmail.users.drafts.send": lambda w: ({}, {}, {"id": w.draft}, 200),
    "gmail.users.drafts.delete": lambda w: ({"id": w.draft}, {}, None, 204),
    # labels
    "gmail.users.labels.list": lambda w: ({}, {}, None, 200),
    "gmail.users.labels.get": lambda w: ({"id": w.label}, {}, None, 200),
    "gmail.users.labels.create": lambda w: ({}, {}, {"name": "New/Nested", "labelListVisibility": "labelShowIfUnread"}, 200),
    "gmail.users.labels.update": lambda w: ({"id": w.label}, {}, {"id": w.label, "name": "Work2", "messageListVisibility": "hide"}, 200),
    "gmail.users.labels.patch": lambda w: ({"id": w.label}, {}, {"color": {"textColor": "#000000", "backgroundColor": "#ffffff"}}, 200),
    "gmail.users.labels.delete": lambda w: ({"id": w.label}, {}, None, 204),
    # simple settings
    "gmail.users.settings.getVacation": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.updateVacation": lambda w: (
        {},
        {},
        {"enableAutoReply": True, "responseBodyHtml": "<p>away</p>", "startTime": "1", "restrictToContacts": True},
        200,
    ),
    "gmail.users.settings.getPop": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.updatePop": lambda w: ({}, {}, {"accessWindow": "fromNowOn", "disposition": "markRead"}, 200),
    "gmail.users.settings.getImap": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.updateImap": lambda w: ({}, {}, {"enabled": True, "expungeBehavior": "trash", "maxFolderSize": 5000}, 200),
    "gmail.users.settings.getLanguage": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.updateLanguage": lambda w: ({}, {}, {"displayLanguage": "de"}, 200),
    "gmail.users.settings.getAutoForwarding": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.updateAutoForwarding": lambda w: (
        {},
        {},
        {"enabled": True, "emailAddress": "fwd@example.org", "disposition": "archive"},
        200,
    ),
    # send-as & S/MIME
    "gmail.users.settings.sendAs.list": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.sendAs.get": lambda w: ({"sendAsEmail": ME}, {}, None, 200),
    "gmail.users.settings.sendAs.create": lambda w: (
        {},
        {},
        {"sendAsEmail": "alias@other.org", "displayName": "Alias", "treatAsAlias": True},
        200,
    ),
    "gmail.users.settings.sendAs.update": lambda w: (
        {"sendAsEmail": "sales@example.com"},
        {},
        {"displayName": "Sales Team", "signature": "sig"},
        200,
    ),
    "gmail.users.settings.sendAs.patch": lambda w: ({"sendAsEmail": "sales@example.com"}, {}, {"replyToAddress": "reply@example.com"}, 200),
    "gmail.users.settings.sendAs.delete": lambda w: ({"sendAsEmail": "sales@example.com"}, {}, None, 204),
    "gmail.users.settings.sendAs.verify": lambda w: ({"sendAsEmail": "sales@example.com"}, {}, None, 204),
    "gmail.users.settings.sendAs.smimeInfo.list": lambda w: ({"sendAsEmail": ME}, {}, None, 200),
    "gmail.users.settings.sendAs.smimeInfo.get": lambda w: ({"sendAsEmail": ME, "id": w.smime}, {}, None, 200),
    "gmail.users.settings.sendAs.smimeInfo.insert": lambda w: ({"sendAsEmail": ME}, {}, {"pkcs12": "AAAA"}, 200),
    "gmail.users.settings.sendAs.smimeInfo.delete": lambda w: ({"sendAsEmail": ME, "id": w.smime}, {}, None, 204),
    "gmail.users.settings.sendAs.smimeInfo.setDefault": lambda w: ({"sendAsEmail": ME, "id": w.smime}, {}, None, 204),
    # forwarding, delegates, filters
    "gmail.users.settings.forwardingAddresses.list": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.forwardingAddresses.get": lambda w: ({"forwardingEmail": "fwd@example.org"}, {}, None, 200),
    "gmail.users.settings.forwardingAddresses.create": lambda w: ({}, {}, {"forwardingEmail": "new@example.org"}, 200),
    "gmail.users.settings.forwardingAddresses.delete": lambda w: ({"forwardingEmail": "fwd@example.org"}, {}, None, 204),
    "gmail.users.settings.delegates.list": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.delegates.get": lambda w: ({"delegateEmail": "assistant@example.com"}, {}, None, 200),
    "gmail.users.settings.delegates.create": lambda w: ({}, {}, {"delegateEmail": "helper@example.com"}, 200),
    "gmail.users.settings.delegates.delete": lambda w: ({"delegateEmail": "assistant@example.com"}, {}, None, 204),
    "gmail.users.settings.filters.list": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.filters.get": lambda w: ({"id": w.filter}, {}, None, 200),
    "gmail.users.settings.filters.create": lambda w: (
        {},
        {},
        {
            "criteria": {"subject": "invoice", "hasAttachment": True, "size": 1000, "sizeComparison": "larger"},
            "action": {"addLabelIds": [w.label], "removeLabelIds": ["INBOX"], "forward": "fwd@example.org"},
        },
        200,
    ),
    "gmail.users.settings.filters.delete": lambda w: ({"id": w.filter}, {}, None, 204),
    # CSE
    "gmail.users.settings.cse.identities.list": lambda w: ({}, {"pageSize": "10"}, None, 200),
    "gmail.users.settings.cse.identities.get": lambda w: ({"cseEmailAddress": ME}, {}, None, 200),
    "gmail.users.settings.cse.identities.create": lambda w: (
        {},
        {},
        {"emailAddress": "sales@example.com", "primaryKeyPairId": w.keypair},
        200,
    ),
    "gmail.users.settings.cse.identities.patch": lambda w: (
        {"emailAddress": ME},
        {},
        {"signAndEncryptKeyPairs": {"signingKeyPairId": w.keypair, "encryptionKeyPairId": w.keypair}},
        200,
    ),
    "gmail.users.settings.cse.identities.delete": lambda w: ({"cseEmailAddress": ME}, {}, None, 204),
    "gmail.users.settings.cse.keypairs.list": lambda w: ({}, {}, None, 200),
    "gmail.users.settings.cse.keypairs.get": lambda w: ({"keyPairId": w.keypair}, {}, None, 200),
    "gmail.users.settings.cse.keypairs.create": lambda w: (
        {},
        {},
        {"pkcs7": "MIIC", "privateKeyMetadata": [{"privateKeyMetadataId": "m1"}]},
        200,
    ),
    "gmail.users.settings.cse.keypairs.enable": lambda w: ({"keyPairId": w.keypair}, {}, {}, 200),
    "gmail.users.settings.cse.keypairs.disable": lambda w: ({"keyPairId": w.keypair}, {}, {}, 200),
    "gmail.users.settings.cse.keypairs.obliterate": lambda w: ({"keyPairId": w.extra["disabled_keypair"]}, {}, {}, 204),
    # People
    "people.people.get": lambda w: ({"resourceName": w.contact}, {"personFields": "names,emailAddresses,metadata"}, None, 200),
    "people.people.getBatchGet": lambda w: ({}, {"resourceNames": [w.contact, "people/me"], "personFields": "names"}, None, 200),
    "people.people.connections.list": lambda w: (
        {"resourceName": "people/me"},
        {"personFields": "names,memberships", "pageSize": "10"},
        None,
        200,
    ),
    "people.people.searchContacts": lambda w: ({}, {"query": "car", "readMask": "names,emailAddresses"}, None, 200),
    "people.people.createContact": lambda w: (
        {},
        {"personFields": "names"},
        {"names": [{"givenName": "Eve", "familyName": "Ng"}], "phoneNumbers": [{"value": "+1 555"}]},
        200,
    ),
    "people.people.updateContact": lambda w: (
        {"resourceName": w.contact},
        {"updatePersonFields": "names"},
        {"names": [{"givenName": "Carla"}]},
        200,
    ),
    "people.people.deleteContact": lambda w: ({"resourceName": w.contact}, {}, None, 200),
    "people.people.batchCreateContacts": lambda w: (
        {},
        {},
        {"contacts": [{"contactPerson": {"names": [{"givenName": "Fay"}]}}], "readMask": "names"},
        200,
    ),
    "people.people.batchUpdateContacts": lambda w: (
        {},
        {},
        {"contacts": {w.contact: {"names": [{"givenName": "Cy"}]}}, "updateMask": "names", "readMask": "names"},
        200,
    ),
    "people.people.batchDeleteContacts": lambda w: ({}, {}, {"resourceNames": [w.contact]}, 200),
    "people.people.listDirectoryPeople": lambda w: (
        {},
        {"readMask": "names", "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"},
        None,
        200,
    ),
    "people.people.searchDirectoryPeople": lambda w: (
        {},
        {"query": "a", "readMask": "names", "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"},
        None,
        200,
    ),
    "people.people.updateContactPhoto": lambda w: ({"resourceName": w.contact}, {}, {"photoBytes": "AAAA"}, 200),
    "people.people.deleteContactPhoto": lambda w: ({"resourceName": w.contact}, {}, None, 200),
    "people.otherContacts.list": lambda w: ({}, {"readMask": "names,emailAddresses"}, None, 200),
    "people.otherContacts.search": lambda w: ({}, {"query": "dan", "readMask": "emailAddresses"}, None, 200),
    "people.otherContacts.copyOtherContactToMyContactsGroup": lambda w: (
        {"resourceName": w.other},
        {},
        {"copyMask": "names,emailAddresses"},
        200,
    ),
    "people.contactGroups.list": lambda w: ({}, {}, None, 200),
    "people.contactGroups.get": lambda w: ({"resourceName": w.group}, {"maxMembers": "5"}, None, 200),
    "people.contactGroups.batchGet": lambda w: ({}, {"resourceNames": [w.group, "contactGroups/nope"]}, None, 200),
    "people.contactGroups.create": lambda w: ({}, {}, {"contactGroup": {"name": "Climbers"}}, 200),
    "people.contactGroups.update": lambda w: ({"resourceName": w.extra["user_group"]}, {}, {"contactGroup": {"name": "Hikers"}}, 200),
    "people.contactGroups.delete": lambda w: ({"resourceName": w.extra["user_group"]}, {}, None, 200),
    "people.contactGroups.members.modify": lambda w: (
        {"resourceName": w.group},
        {},
        {"resourceNamesToAdd": [w.contact], "resourceNamesToRemove": ["people/c0"]},
        200,
    ),
}


def build_path(method, params: dict[str, str]) -> str:
    def sub(m):
        reserved, name = m.group(1), m.group(2)
        value = params.get(name, "me" if name == "userId" else None)
        assert value is not None, f"missing path param {name} for {method.id}"
        return value if reserved else quote(value, safe="@")

    return "/" + re.sub(r"\{(\+?)([^}]+)\}", sub, method.path)


def _prepare(http, w: World, mid: str) -> None:
    if mid == "gmail.users.settings.cse.keypairs.obliterate":
        kid = http.post("/gmail/v1/users/me/settings/cse/keypairs", json={"pkcs7": "MIID"}).json()["keyPairId"]
        http.post(f"/gmail/v1/users/me/settings/cse/keypairs/{kid}:disable", json={})
        w.extra["disabled_keypair"] = kid
    if mid in ("people.contactGroups.update", "people.contactGroups.delete"):
        w.extra["user_group"] = http.post("/v1/contactGroups", json={"contactGroup": {"name": "Temp"}}).json()["resourceName"]


def test_every_method_has_a_scenario():
    assert sorted(set(METHODS) - set(S)) == []
    assert sorted(set(S) - set(METHODS)) == []


@pytest.mark.parametrize("mid", sorted(S))
def test_happy_path(mid, http, world):
    _prepare(http, world, mid)
    path_params, query, body, status = S[mid](world)
    method = METHODS[mid]
    resp = http.request(method.http_method, build_path(method, path_params), params=query, json=body)
    assert resp.status_code == status, resp.text
    if status == 204:
        assert resp.content == b""


# --- generated negative cases ------------------------------------------------------------

DUMMY = {
    "userId": "me",
    "id": "abcdef0123456789",
    "messageId": "abcdef0123456789",
    "sendAsEmail": "ghost@example.com",
    "forwardingEmail": "ghost@example.com",
    "delegateEmail": "ghost@example.com",
    "cseEmailAddress": "ghost@example.com",
    "emailAddress": "ghost@example.com",
    "keyPairId": "nokey",
}


def _dummy_path(method) -> str:
    params = dict(DUMMY)
    for name, p in method.params.items():
        if p.location == "path" and name not in params:
            prefix = (p.pattern or "^x/").strip("^$").split("/")[0]
            params[name] = f"{prefix}/c404" if prefix != "people" else "people/c404"
    if method.id == "people.people.connections.list":
        params["resourceName"] = "people/me"
    return build_path(method, params)


@pytest.mark.parametrize("mid", sorted(METHODS))
def test_requires_authentication(mid, base_url):
    import httpx

    method = METHODS[mid]
    resp = httpx.request(method.http_method, base_url + _dummy_path(method), json={} if method.request_ref else None)
    assert resp.status_code == 401
    assert resp.json()["error"]["status"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("mid", sorted(METHODS))
def test_rejects_unknown_query_parameter(mid, http):
    method = METHODS[mid]
    resp = http.request(method.http_method, _dummy_path(method), params={"notAParam": "1"}, json={} if method.request_ref else None)
    assert resp.status_code == 400
    assert "Cannot bind query parameter" in resp.json()["error"]["message"]


BODY_METHODS = sorted(mid for mid, m in METHODS.items() if m.request_ref)


@pytest.mark.parametrize("mid", BODY_METHODS)
def test_rejects_non_object_body(mid, http):
    method = METHODS[mid]
    resp = http.request(method.http_method, _dummy_path(method), content=b"[1, 2]", headers={"content-type": "application/json"})
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("mid", BODY_METHODS)
def test_rejects_unknown_body_field(mid, http):
    method = METHODS[mid]
    resp = http.request(method.http_method, _dummy_path(method), json={"definitelyNotAField": 1})
    assert resp.status_code == 400, resp.text
    assert "Unknown name" in resp.json()["error"]["message"]


# Methods addressing one resource by id: a well-formed but unknown id must be a 404.
LOOKUP_METHODS = sorted(
    mid
    for mid, m in METHODS.items()
    if any(p.location == "path" and n != "userId" for n, p in m.params.items())
    and mid
    not in {
        "people.people.connections.list",  # only people/me
        "people.people.deleteContactPhoto",
        "people.people.updateContactPhoto",  # schema fallback
    }
)
LOOKUP_BODIES = {
    "gmail.users.messages.modify": {"addLabelIds": ["STARRED"]},
    "gmail.users.threads.modify": {"addLabelIds": ["STARRED"]},
    "gmail.users.labels.update": {"name": "Anything"},
    "gmail.users.drafts.update": {"message": {"raw": raw()}},
    "people.people.updateContact": {"names": [{"givenName": "x"}]},
    "gmail.users.settings.cse.identities.patch": {"primaryKeyPairId": "nokey"},
    "people.otherContacts.copyOtherContactToMyContactsGroup": {"copyMask": "names"},
}
LOOKUP_QUERY = {
    "people.people.get": {"personFields": "names"},
    "people.people.updateContact": {"updatePersonFields": "names"},
}


@pytest.mark.parametrize("mid", LOOKUP_METHODS)
def test_unknown_resource_is_404(mid, http, store):
    method = METHODS[mid]
    body = LOOKUP_BODIES.get(mid, {} if method.request_ref else None)
    resp = http.request(method.http_method, _dummy_path(method), params=LOOKUP_QUERY.get(mid), json=body)
    assert resp.status_code == 404, f"{mid}: {resp.status_code} {resp.text}"
    assert resp.json()["error"]["status"] == "NOT_FOUND"
