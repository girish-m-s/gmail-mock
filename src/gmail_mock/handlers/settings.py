"""users.settings.*: simple settings, send-as, S/MIME, forwarding, delegates, filters, CSE."""

from __future__ import annotations

import secrets

from ..errors import ApiError, bad_request, failed_precondition, not_found
from ..util import compact, now_ms, rfc3339
from . import Ctx, handler

# --- simple settings ------------------------------------------------------------------

_SIMPLE = {
    "Vacation": "vacation",
    "Pop": "pop",
    "Imap": "imap",
    "Language": "language",
    "AutoForwarding": "autoForwarding",
}


def _get_setting(key: str):
    def get(ctx: Ctx):
        return dict(ctx.mailbox.settings[key])

    return get


def _update_setting(key: str):
    def update(ctx: Ctx):
        box = ctx.mailbox
        body = dict(ctx.data)
        if key == "imap" and body.get("maxFolderSize", 0) not in (0, 1000, 2000, 5000, 10000):
            raise bad_request("Invalid maxFolderSize")
        if key == "autoForwarding" and body.get("enabled"):
            target = (body.get("emailAddress") or "").lower()
            fwd = box.forwarding.get(target)
            if not fwd or fwd.get("verificationStatus") != "accepted":
                raise failed_precondition("Unverified forwarding address")
            body.setdefault("disposition", "leaveInInbox")
        if key == "vacation" and body.get("enableAutoReply") and not (body.get("responseBodyPlainText") or body.get("responseBodyHtml")):
            raise bad_request("Vacation responder requires a response body")
        if key == "language":
            body = {"displayLanguage": body.get("displayLanguage") or "en"}
        box.settings[key] = body
        return dict(body)

    return update


for _suffix, _key in _SIMPLE.items():
    handler(f"gmail.users.settings.get{_suffix}")(_get_setting(_key))
    handler(f"gmail.users.settings.update{_suffix}")(_update_setting(_key))


# --- send-as -------------------------------------------------------------------------


def _send_as(ctx: Ctx) -> dict:
    alias = ctx.mailbox.send_as.get(ctx.path["sendAsEmail"].lower())
    if alias is None:
        raise not_found()
    return alias


@handler("gmail.users.settings.sendAs.list")
def list_send_as(ctx: Ctx):
    return {"sendAs": [dict(s) for s in ctx.mailbox.send_as.values()]}


@handler("gmail.users.settings.sendAs.get")
def get_send_as(ctx: Ctx):
    return dict(_send_as(ctx))


@handler("gmail.users.settings.sendAs.create")
def create_send_as(ctx: Ctx):
    box = ctx.mailbox
    email = (ctx.data.get("sendAsEmail") or "").lower()
    if "@" not in email:
        raise bad_request("Invalid sendAsEmail")
    if email in box.send_as:
        raise ApiError(409, "Alias already exists", reason="duplicate")
    alias = {
        "sendAsEmail": email,
        "displayName": ctx.data.get("displayName", ""),
        "replyToAddress": ctx.data.get("replyToAddress", ""),
        "signature": ctx.data.get("signature", ""),
        "isPrimary": False,
        "isDefault": False,
        "treatAsAlias": ctx.data.get("treatAsAlias", True),
        "verificationStatus": "accepted" if email.split("@")[1] == box.email.split("@")[1] else "pending",
    }
    if ctx.data.get("smtpMsa"):
        alias["smtpMsa"] = ctx.data["smtpMsa"]
    box.send_as[email] = alias
    if ctx.data.get("isDefault") and alias["verificationStatus"] == "accepted":
        _make_default(box, email)
    return dict(alias)


def _make_default(box, email: str) -> None:
    for addr, alias in box.send_as.items():
        alias["isDefault"] = addr == email


def _update_send_as(ctx: Ctx, patch: bool):
    box = ctx.mailbox
    alias = _send_as(ctx)
    for key in ("displayName", "replyToAddress", "signature", "treatAsAlias", "smtpMsa"):
        if key in ctx.data:
            alias[key] = ctx.data[key]
        elif not patch and key in ("displayName", "replyToAddress", "signature"):
            alias[key] = ""  # PUT replaces: omitted string fields reset
    if ctx.data.get("isDefault"):
        if alias.get("verificationStatus", "accepted") != "accepted":
            raise failed_precondition("Send-as alias must be verified before it can be the default")
        _make_default(box, alias["sendAsEmail"])
    return dict(alias)


@handler("gmail.users.settings.sendAs.update")
def update_send_as(ctx: Ctx):
    return _update_send_as(ctx, patch=False)


@handler("gmail.users.settings.sendAs.patch")
def patch_send_as(ctx: Ctx):
    return _update_send_as(ctx, patch=True)


@handler("gmail.users.settings.sendAs.delete")
def delete_send_as(ctx: Ctx):
    alias = _send_as(ctx)
    if alias.get("isPrimary"):
        raise bad_request("Cannot delete the primary send-as alias")
    box = ctx.mailbox
    del box.send_as[alias["sendAsEmail"]]
    if alias.get("isDefault"):
        _make_default(box, box.primary_send_as["sendAsEmail"])


@handler("gmail.users.settings.sendAs.verify")
def verify_send_as(ctx: Ctx):
    alias = _send_as(ctx)
    if alias.get("isPrimary"):
        raise bad_request("Primary address does not need verification")
    # Real Gmail emails a verification link; the mock accepts immediately.
    alias["verificationStatus"] = "accepted"


# --- S/MIME ----------------------------------------------------------------------------


def _smime_book(ctx: Ctx) -> dict:
    alias = _send_as(ctx)
    return ctx.mailbox.smime.setdefault(alias["sendAsEmail"], {})


@handler("gmail.users.settings.sendAs.smimeInfo.list")
def list_smime(ctx: Ctx):
    return compact({"smimeInfo": [dict(s) for s in _smime_book(ctx).values()]})


@handler("gmail.users.settings.sendAs.smimeInfo.get")
def get_smime(ctx: Ctx):
    info = _smime_book(ctx).get(ctx.path["id"])
    if info is None:
        raise not_found()
    return dict(info)


@handler("gmail.users.settings.sendAs.smimeInfo.insert")
def insert_smime(ctx: Ctx):
    book = _smime_book(ctx)
    if not (ctx.data.get("pkcs12") or ctx.data.get("pem")):
        raise bad_request("Either pkcs12 or pem is required")
    sid = secrets.token_urlsafe(16)
    info = {
        "id": sid,
        "issuerCn": "Gmail Mock CA",
        "isDefault": not book,
        "expiration": str(now_ms() + 365 * 86_400_000),
        "pem": ctx.data.get("pem") or "-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n",
    }
    book[sid] = info
    return dict(info)


@handler("gmail.users.settings.sendAs.smimeInfo.delete")
def delete_smime(ctx: Ctx):
    book = _smime_book(ctx)
    info = book.get(ctx.path["id"])
    if info is None:
        raise not_found()
    if info.get("isDefault") and len(book) > 1:
        raise bad_request("Cannot delete the default S/MIME config while others exist")
    del book[ctx.path["id"]]


@handler("gmail.users.settings.sendAs.smimeInfo.setDefault")
def default_smime(ctx: Ctx):
    book = _smime_book(ctx)
    if ctx.path["id"] not in book:
        raise not_found()
    for sid, info in book.items():
        info["isDefault"] = sid == ctx.path["id"]


# --- forwarding addresses ------------------------------------------------------------


@handler("gmail.users.settings.forwardingAddresses.list")
def list_forwarding(ctx: Ctx):
    return compact({"forwardingAddresses": [dict(f) for f in ctx.mailbox.forwarding.values()]})


@handler("gmail.users.settings.forwardingAddresses.get")
def get_forwarding(ctx: Ctx):
    fwd = ctx.mailbox.forwarding.get(ctx.path["forwardingEmail"].lower())
    if fwd is None:
        raise not_found()
    return dict(fwd)


@handler("gmail.users.settings.forwardingAddresses.create")
def create_forwarding(ctx: Ctx):
    box = ctx.mailbox
    email = (ctx.data.get("forwardingEmail") or "").lower()
    if "@" not in email:
        raise bad_request("Invalid forwardingEmail")
    if email in box.forwarding:
        raise ApiError(409, "Forwarding address already exists", reason="duplicate")
    # Real Gmail sends a confirmation email; use POST /_mock/.../verify to accept it.
    fwd = {"forwardingEmail": email, "verificationStatus": "pending"}
    box.forwarding[email] = fwd
    return dict(fwd)


@handler("gmail.users.settings.forwardingAddresses.delete")
def delete_forwarding(ctx: Ctx):
    box = ctx.mailbox
    email = ctx.path["forwardingEmail"].lower()
    if email not in box.forwarding:
        raise not_found()
    del box.forwarding[email]
    if box.settings["autoForwarding"].get("emailAddress", "").lower() == email:
        box.settings["autoForwarding"] = {"enabled": False}


# --- delegates ------------------------------------------------------------------------


@handler("gmail.users.settings.delegates.list")
def list_delegates(ctx: Ctx):
    return compact({"delegates": [dict(d) for d in ctx.mailbox.delegates.values()]})


@handler("gmail.users.settings.delegates.get")
def get_delegate(ctx: Ctx):
    delegate = ctx.mailbox.delegates.get(ctx.path["delegateEmail"].lower())
    if delegate is None:
        raise not_found()
    return dict(delegate)


@handler("gmail.users.settings.delegates.create")
def create_delegate(ctx: Ctx):
    box = ctx.mailbox
    email = (ctx.data.get("delegateEmail") or "").lower()
    if "@" not in email or email == box.email:
        raise bad_request("Invalid delegate")
    if email in box.delegates:
        raise ApiError(409, "Delegate already exists", reason="duplicate")
    delegate = {"delegateEmail": email, "verificationStatus": "accepted"}
    box.delegates[email] = delegate
    return dict(delegate)


@handler("gmail.users.settings.delegates.delete")
def delete_delegate(ctx: Ctx):
    if ctx.mailbox.delegates.pop(ctx.path["delegateEmail"].lower(), None) is None:
        raise not_found()


# --- filters ------------------------------------------------------------------------


@handler("gmail.users.settings.filters.list")
def list_filters(ctx: Ctx):
    return compact({"filter": [dict(f) for f in ctx.mailbox.filters.values()]})


@handler("gmail.users.settings.filters.get")
def get_filter(ctx: Ctx):
    f = ctx.mailbox.filters.get(ctx.path["id"])
    if f is None:
        raise not_found()
    return dict(f)


@handler("gmail.users.settings.filters.create")
def create_filter(ctx: Ctx):
    box = ctx.mailbox
    criteria = {k: v for k, v in (ctx.data.get("criteria") or {}).items() if v not in (None, "", False)}
    action = {k: v for k, v in (ctx.data.get("action") or {}).items() if v not in (None, "", [])}
    if not criteria:
        raise bad_request("Filter doesn't have any criteria")
    if not action:
        raise bad_request("Filter doesn't have any actions")
    box.check_labels((action.get("addLabelIds") or []) + (action.get("removeLabelIds") or []))
    if action.get("forward"):
        fwd = box.forwarding.get(action["forward"].lower())
        if not fwd or fwd.get("verificationStatus") != "accepted":
            raise failed_precondition("Unverified forwarding address")
    for existing in box.filters.values():
        if existing["criteria"] == criteria and existing["action"] == action:
            raise ApiError(400, "Filter already exists", reason="failedPrecondition", status="FAILED_PRECONDITION")
    fid = box.store.ids.filter()
    box.filters[fid] = {"id": fid, "criteria": criteria, "action": action}
    return dict(box.filters[fid])


@handler("gmail.users.settings.filters.delete")
def delete_filter(ctx: Ctx):
    if ctx.mailbox.filters.pop(ctx.path["id"], None) is None:
        raise not_found()


# --- client-side encryption ----------------------------------------------------------


@handler("gmail.users.settings.cse.identities.list")
def list_cse_identities(ctx: Ctx):
    return compact({"cseIdentities": [dict(i) for i in ctx.mailbox.cse_identities.values()]})


@handler("gmail.users.settings.cse.identities.get")
def get_cse_identity(ctx: Ctx):
    identity = ctx.mailbox.cse_identities.get(ctx.path["cseEmailAddress"].lower())
    if identity is None:
        raise not_found()
    return dict(identity)


def _check_keypair_refs(box, body: dict) -> None:
    refs = [body.get("primaryKeyPairId")] if body.get("primaryKeyPairId") else []
    pairs = body.get("signAndEncryptKeyPairs") or {}
    refs += [pairs.get("signingKeyPairId"), pairs.get("encryptionKeyPairId")]
    refs = [r for r in refs if r]
    if not refs:
        raise bad_request("A key pair id is required")
    for ref in refs:
        if ref not in box.cse_keypairs:
            raise bad_request(f"Unknown key pair: {ref}")


@handler("gmail.users.settings.cse.identities.create")
def create_cse_identity(ctx: Ctx):
    box = ctx.mailbox
    email = (ctx.data.get("emailAddress") or box.email).lower()
    if email in box.cse_identities:
        raise ApiError(409, "Identity already exists", reason="duplicate")
    _check_keypair_refs(box, ctx.data)
    identity = {**ctx.data, "emailAddress": email}
    box.cse_identities[email] = identity
    return dict(identity)


@handler("gmail.users.settings.cse.identities.patch")
def patch_cse_identity(ctx: Ctx):
    box = ctx.mailbox
    email = ctx.path["emailAddress"].lower()
    identity = box.cse_identities.get(email)
    if identity is None:
        raise not_found()
    _check_keypair_refs(box, ctx.data)
    identity.pop("primaryKeyPairId", None)
    identity.pop("signAndEncryptKeyPairs", None)
    identity.update({k: v for k, v in ctx.data.items() if k != "emailAddress"})
    return dict(identity)


@handler("gmail.users.settings.cse.identities.delete")
def delete_cse_identity(ctx: Ctx):
    if ctx.mailbox.cse_identities.pop(ctx.path["cseEmailAddress"].lower(), None) is None:
        raise not_found()


def _keypair(ctx: Ctx) -> dict:
    pair = ctx.mailbox.cse_keypairs.get(ctx.path["keyPairId"])
    if pair is None:
        raise not_found()
    return pair


@handler("gmail.users.settings.cse.keypairs.list")
def list_cse_keypairs(ctx: Ctx):
    return compact({"cseKeyPairs": [dict(k) for k in ctx.mailbox.cse_keypairs.values()]})


@handler("gmail.users.settings.cse.keypairs.get")
def get_cse_keypair(ctx: Ctx):
    return dict(_keypair(ctx))


@handler("gmail.users.settings.cse.keypairs.create")
def create_cse_keypair(ctx: Ctx):
    box = ctx.mailbox
    if not ctx.data.get("pkcs7"):
        raise bad_request("pkcs7 is required")
    kid = secrets.token_hex(16)
    pair = {
        "keyPairId": kid,
        "pkcs7": ctx.data["pkcs7"],
        "pem": ctx.data.get("pem", ""),
        "privateKeyMetadata": ctx.data.get("privateKeyMetadata", []),
        "subjectEmailAddresses": [box.email],
        "enablementState": "enabled",
    }
    box.cse_keypairs[kid] = pair
    return dict(pair)


@handler("gmail.users.settings.cse.keypairs.enable")
def enable_cse_keypair(ctx: Ctx):
    pair = _keypair(ctx)
    pair["enablementState"] = "enabled"
    pair.pop("disableTime", None)
    return dict(pair)


@handler("gmail.users.settings.cse.keypairs.disable")
def disable_cse_keypair(ctx: Ctx):
    pair = _keypair(ctx)
    pair["enablementState"] = "disabled"
    pair["disableTime"] = rfc3339()
    return dict(pair)


@handler("gmail.users.settings.cse.keypairs.obliterate")
def obliterate_cse_keypair(ctx: Ctx):
    pair = _keypair(ctx)
    if pair["enablementState"] != "disabled":
        raise failed_precondition("Key pair must be disabled before it can be obliterated")
    del ctx.mailbox.cse_keypairs[pair["keyPairId"]]
