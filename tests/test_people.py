import pytest
from conftest import raw_message
from googleapiclient.errors import HttpError

SEED = {
    "users": [
        {
            "email": "me@example.com",
            "contacts": [
                {"name": "Bob Builder", "email": "bob@builders.example", "phone": "+1 555 0100", "organization": "Builders Inc"},
                {"name": "Alice Liddell", "email": "alice@wonder.example"},
            ],
        }
    ]
}


@pytest.fixture
def seeded(http):
    assert http.post("/_mock/seed", json=SEED).status_code == 200


def test_connections_list_requires_person_fields(people, seeded):
    with pytest.raises(HttpError) as err:
        people.people().connections().list(resourceName="people/me").execute()
    assert "personFields mask is required" in str(err.value)
    result = (
        people.people()
        .connections()
        .list(resourceName="people/me", personFields="names,emailAddresses", sortOrder="FIRST_NAME_ASCENDING")
        .execute()
    )
    assert result["totalPeople"] == 2
    first = result["connections"][0]
    assert first["names"][0]["displayName"] == "Alice Liddell"
    assert set(first) == {"resourceName", "etag", "names", "emailAddresses"}


def test_search_contacts_prefix(people, seeded):
    hits = people.people().searchContacts(query="bui", readMask="names,organizations").execute()["results"]
    assert [h["person"]["names"][0]["displayName"] for h in hits] == ["Bob Builder"]
    assert people.people().searchContacts(query="zzz", readMask="names").execute() == {}


def test_get_me_and_batch_get(people, seeded):
    me = people.people().get(resourceName="people/me", personFields="emailAddresses").execute()
    assert me["emailAddresses"][0]["value"] == "me@example.com"
    rn = people.people().connections().list(resourceName="people/me", personFields="names").execute()["connections"][0]["resourceName"]
    batch = people.people().getBatchGet(resourceNames=[rn, "people/c0"], personFields="names").execute()["responses"]
    assert [r["httpStatusCode"] for r in batch] == [200, 404]


def test_other_contacts_learned_from_sent_mail(gmail, people):
    gmail.users().messages().send(userId="me", body={"raw": raw_message(to="Zed Zebra <zed@zoo.example>")}).execute()
    listed = people.otherContacts().list(readMask="names,emailAddresses").execute()
    assert listed["otherContacts"][0]["emailAddresses"][0]["value"] == "zed@zoo.example"
    found = people.otherContacts().search(query="zed", readMask="emailAddresses").execute()
    assert len(found["results"]) == 1
    with pytest.raises(HttpError):
        people.otherContacts().list(readMask="organizations").execute()


def test_create_update_delete_contact_with_etag(people):
    created = (
        people.people()
        .createContact(body={"names": [{"givenName": "Carol"}], "emailAddresses": [{"value": "carol@example.org"}]})
        .execute()
    )
    assert created["names"][0]["displayName"] == "Carol"
    with pytest.raises(HttpError) as err:
        people.people().updateContact(
            resourceName=created["resourceName"], updatePersonFields="names", body={"etag": "%stale", "names": [{"givenName": "Caroline"}]}
        ).execute()
    assert err.value.resp.status == 400
    updated = (
        people.people()
        .updateContact(
            resourceName=created["resourceName"],
            updatePersonFields="names",
            body={"etag": created["etag"], "names": [{"givenName": "Caroline"}]},
        )
        .execute()
    )
    assert updated["names"][0]["displayName"] == "Caroline" and updated["etag"] != created["etag"]
    people.people().deleteContact(resourceName=created["resourceName"]).execute()
    with pytest.raises(HttpError):
        people.people().get(resourceName=created["resourceName"], personFields="names").execute()


def test_contact_groups(people, seeded):
    group = people.contactGroups().create(body={"contactGroup": {"name": "Climbers"}}).execute()
    rn = people.people().connections().list(resourceName="people/me", personFields="names").execute()["connections"][0]["resourceName"]
    people.contactGroups().members().modify(resourceName=group["resourceName"], body={"resourceNamesToAdd": [rn]}).execute()
    got = people.contactGroups().get(resourceName=group["resourceName"], maxMembers=10).execute()
    assert got["memberCount"] == 1 and got["memberResourceNames"] == [rn]
    names = {g["formattedName"] for g in people.contactGroups().list().execute()["contactGroups"]}
    assert {"My Contacts", "Climbers"} <= names
