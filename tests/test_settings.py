import pytest
from conftest import ME
from googleapiclient.errors import HttpError


def settings(gmail):
    return gmail.users().settings()


def test_simple_settings_round_trip(gmail):
    s = settings(gmail)
    assert s.getVacation(userId="me").execute() == {"enableAutoReply": False}
    body = {"enableAutoReply": True, "responseSubject": "OOO", "responseBodyPlainText": "Back Monday"}
    assert s.updateVacation(userId="me", body=body).execute() == body
    assert s.getVacation(userId="me").execute()["responseSubject"] == "OOO"

    assert s.getImap(userId="me").execute()["enabled"] is True
    assert s.updateImap(userId="me", body={"enabled": False, "maxFolderSize": 1000}).execute()["maxFolderSize"] == 1000
    with pytest.raises(HttpError):
        s.updateImap(userId="me", body={"maxFolderSize": 7}).execute()

    assert s.updatePop(userId="me", body={"accessWindow": "allMail", "disposition": "archive"}).execute()["accessWindow"] == "allMail"
    with pytest.raises(HttpError) as err:
        s.updatePop(userId="me", body={"accessWindow": "sometimes"}).execute()
    assert "TYPE_ENUM" in str(err.value)

    assert s.getLanguage(userId="me").execute() == {"displayLanguage": "en"}
    assert s.updateLanguage(userId="me", body={"displayLanguage": "fr"}).execute() == {"displayLanguage": "fr"}


def test_auto_forwarding_needs_verified_address(http, gmail):
    s = settings(gmail)
    fwd = s.forwardingAddresses().create(userId="me", body={"forwardingEmail": "backup@example.org"}).execute()
    assert fwd["verificationStatus"] == "pending"
    with pytest.raises(HttpError):
        s.updateAutoForwarding(userId="me", body={"enabled": True, "emailAddress": "backup@example.org"}).execute()
    http.post(f"/_mock/users/{ME}/forwardingAddresses/backup@example.org/verify")
    out = s.updateAutoForwarding(userId="me", body={"enabled": True, "emailAddress": "backup@example.org"}).execute()
    assert out["enabled"] is True
    assert s.getAutoForwarding(userId="me").execute()["emailAddress"] == "backup@example.org"
    assert [f["forwardingEmail"] for f in s.forwardingAddresses().list(userId="me").execute()["forwardingAddresses"]] == [
        "backup@example.org"
    ]
    s.forwardingAddresses().delete(userId="me", forwardingEmail="backup@example.org").execute()
    assert s.getAutoForwarding(userId="me").execute() == {"enabled": False}


def test_send_as_aliases(gmail):
    send_as = settings(gmail).sendAs()
    [primary] = send_as.list(userId="me").execute()["sendAs"]
    assert primary["isPrimary"] and primary["sendAsEmail"] == ME
    alias = send_as.create(userId="me", body={"sendAsEmail": "sales@example.com", "displayName": "Sales"}).execute()
    assert alias["verificationStatus"] == "accepted"  # same domain
    ext = send_as.create(userId="me", body={"sendAsEmail": "me@other.org"}).execute()
    assert ext["verificationStatus"] == "pending"
    send_as.verify(userId="me", sendAsEmail="me@other.org").execute()
    assert send_as.get(userId="me", sendAsEmail="me@other.org").execute()["verificationStatus"] == "accepted"
    patched = send_as.patch(userId="me", sendAsEmail="sales@example.com", body={"signature": "-- Sales", "isDefault": True}).execute()
    assert patched["signature"] == "-- Sales" and patched["isDefault"]
    assert not send_as.get(userId="me", sendAsEmail=ME).execute()["isDefault"]
    updated = send_as.update(userId="me", sendAsEmail="sales@example.com", body={"displayName": "Sales Team"}).execute()
    assert updated["displayName"] == "Sales Team" and updated["signature"] == ""
    with pytest.raises(HttpError):
        send_as.delete(userId="me", sendAsEmail=ME).execute()
    send_as.delete(userId="me", sendAsEmail="sales@example.com").execute()
    assert send_as.get(userId="me", sendAsEmail=ME).execute()["isDefault"]


def test_smime(gmail):
    smime = settings(gmail).sendAs().smimeInfo()
    a = smime.insert(userId="me", sendAsEmail=ME, body={"pkcs12": "AAAA", "encryptedKeyPassword": "pw"}).execute()
    b = smime.insert(userId="me", sendAsEmail=ME, body={"pem": "-----BEGIN-----"}).execute()
    assert a["isDefault"] and not b["isDefault"]
    smime.setDefault(userId="me", sendAsEmail=ME, id=b["id"]).execute()
    infos = {i["id"]: i for i in smime.list(userId="me", sendAsEmail=ME).execute()["smimeInfo"]}
    assert infos[b["id"]]["isDefault"] and not infos[a["id"]]["isDefault"]
    smime.delete(userId="me", sendAsEmail=ME, id=a["id"]).execute()
    assert smime.get(userId="me", sendAsEmail=ME, id=b["id"]).execute()["id"] == b["id"]


def test_filters_apply_to_incoming_mail(http, gmail):
    label = gmail.users().labels().create(userId="me", body={"name": "Newsletters"}).execute()
    filters = settings(gmail).filters()
    created = filters.create(
        userId="me",
        body={
            "criteria": {"from": "news@shop.example"},
            "action": {"addLabelIds": [label["id"]], "removeLabelIds": ["INBOX", "UNREAD"]},
        },
    ).execute()
    assert filters.list(userId="me").execute()["filter"][0]["id"] == created["id"]
    msg = http.post(f"/_mock/users/{ME}/messages", json={"from": "news@shop.example", "subject": "Deals"}).json()
    assert set(msg["labelIds"]) == {label["id"], "CATEGORY_PERSONAL"}
    other = http.post(f"/_mock/users/{ME}/messages", json={"from": "friend@example.org"}).json()
    assert "INBOX" in other["labelIds"]
    with pytest.raises(HttpError):
        filters.create(userId="me", body={"criteria": {}, "action": {"addLabelIds": ["STARRED"]}}).execute()
    filters.delete(userId="me", id=created["id"]).execute()
    with pytest.raises(HttpError):
        filters.get(userId="me", id=created["id"]).execute()


def test_delegates(gmail):
    delegates = settings(gmail).delegates()
    delegates.create(userId="me", body={"delegateEmail": "assistant@example.com"}).execute()
    assert delegates.get(userId="me", delegateEmail="assistant@example.com").execute()["verificationStatus"] == "accepted"
    delegates.delete(userId="me", delegateEmail="assistant@example.com").execute()
    assert delegates.list(userId="me").execute() == {}


def test_client_side_encryption(gmail):
    cse = settings(gmail).cse()
    pair = cse.keypairs().create(userId="me", body={"pkcs7": "MIIB..."}).execute()
    assert cse.keypairs().list(userId="me").execute()["cseKeyPairs"][0]["keyPairId"] == pair["keyPairId"]
    with pytest.raises(HttpError):
        cse.identities().create(userId="me", body={"primaryKeyPairId": "missing"}).execute()
    identity = cse.identities().create(userId="me", body={"primaryKeyPairId": pair["keyPairId"]}).execute()
    assert identity["emailAddress"] == ME
    assert cse.identities().list(userId="me").execute()["cseIdentities"] == [identity]
    with pytest.raises(HttpError):
        cse.keypairs().obliterate(userId="me", keyPairId=pair["keyPairId"], body={}).execute()
    disabled = cse.keypairs().disable(userId="me", keyPairId=pair["keyPairId"], body={}).execute()
    assert disabled["enablementState"] == "disabled" and "disableTime" in disabled
    cse.keypairs().obliterate(userId="me", keyPairId=pair["keyPairId"], body={}).execute()
