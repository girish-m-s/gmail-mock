"""The People API subset used by Gmail connectors (contacts, other contacts, groups).

Gmail connectors' contact tools ("Get contacts", "Get People", "Search People") call these.
Sending mail adds unknown recipients to "Other contacts", as Gmail does.
"""

from __future__ import annotations

import hashlib

from ..errors import bad_request, failed_precondition, not_found
from ..util import compact, now_ms, paginate, rfc3339
from . import Ctx, handler

PERSON_FIELDS = {
    "addresses",
    "ageRanges",
    "biographies",
    "birthdays",
    "calendarUrls",
    "clientData",
    "coverPhotos",
    "emailAddresses",
    "events",
    "externalIds",
    "genders",
    "imClients",
    "interests",
    "locales",
    "locations",
    "memberships",
    "metadata",
    "miscKeywords",
    "names",
    "nicknames",
    "occupations",
    "organizations",
    "phoneNumbers",
    "photos",
    "relations",
    "sipAddresses",
    "skills",
    "urls",
    "userDefined",
}
OTHER_CONTACT_FIELDS = {"emailAddresses", "metadata", "names", "phoneNumbers", "photos"}
SYSTEM_GROUPS = {
    "contactGroups/myContacts": "My Contacts",
    "contactGroups/starred": "Starred",
    "contactGroups/friends": "Friends",
    "contactGroups/family": "Family",
    "contactGroups/coworkers": "Coworkers",
}


def _etag(person: dict) -> str:
    body = repr(sorted((k, repr(v)) for k, v in person.items() if k not in ("etag", "metadata")))
    return "%" + hashlib.sha1(body.encode()).hexdigest()[:24]


def _stamp(person: dict, source_type: str) -> dict:
    person["metadata"] = {
        "sources": [{"type": source_type, "id": person["resourceName"].split("/")[-1], "updateTime": rfc3339()}],
        "objectType": "PERSON",
    }
    person["etag"] = _etag(person)
    person["metadata"]["sources"][0]["etag"] = person["etag"]
    person["_updated"] = now_ms()
    return person


def _mask(value: str | None, name: str, allowed: set[str] = PERSON_FIELDS) -> set[str]:
    if not value:
        raise bad_request(
            f"{name} mask is required. Please specify one or more valid paths. "
            "Valid paths are documented at https://developers.google.com/people/api/rest/v1/people/get."
        )
    fields = {f.strip().removeprefix("person.") for f in value.split(",") if f.strip()}
    for f in fields:
        if f not in allowed:
            raise bad_request(
                f'Invalid {name} mask path: "{f}". Valid paths are documented at https://developers.google.com/people/api/rest/v1/people/get.'
            )
    return fields


def _project(person: dict, fields: set[str]) -> dict:
    out = {"resourceName": person["resourceName"], "etag": person["etag"]}
    for f in fields:
        if f in person and not f.startswith("_"):
            out[f] = person[f]
    return out


def _primary(values: list[dict]) -> list[dict]:
    out = []
    for i, v in enumerate(values or []):
        v = dict(v)
        v.setdefault("metadata", {"primary": i == 0, "source": {"type": "CONTACT"}})
        out.append(v)
    return out


def _clean_input(body: dict) -> dict:
    person = {k: v for k, v in body.items() if k in PERSON_FIELDS and k != "metadata"}
    for key in ("names", "emailAddresses", "phoneNumbers", "organizations", "addresses"):
        if key in person:
            person[key] = _primary(person[key])
    for name in person.get("names", []):
        if "displayName" not in name:
            name["displayName"] = " ".join(p for p in (name.get("givenName"), name.get("familyName")) if p) or name.get(
                "unstructuredName", ""
            )
    return person


def create_contact(box, body: dict) -> dict:
    rn = f"people/{box.store.ids.contact()}"
    person = {"resourceName": rn, **_clean_input(body)}
    person["memberships"] = [
        {"contactGroupMembership": {"contactGroupId": "myContacts", "contactGroupResourceName": "contactGroups/myContacts"}}
    ]
    box.contacts[rn] = _stamp(person, "CONTACT")
    return person


def new_other_contact(box, name: str, email: str) -> dict:
    rn = f"otherContacts/{box.store.ids.contact()}"
    person = {"resourceName": rn, "emailAddresses": _primary([{"value": email}])}
    if name:
        person["names"] = _primary([{"displayName": name, "unstructuredName": name}])
    box.other_contacts[rn] = _stamp(person, "OTHER_CONTACT")
    return person


def _profile(box) -> dict:
    person = {
        "resourceName": box.profile_resource,
        "names": _primary([{"displayName": box.display_name, "unstructuredName": box.display_name}]),
        "emailAddresses": _primary([{"value": box.email}]),
    }
    return _stamp(person, "PROFILE")


def _get_person(box, resource_name: str) -> dict:
    if resource_name in ("people/me", box.profile_resource):
        return _profile(box)
    person = box.contacts.get(resource_name)
    if person is None:
        raise not_found()
    return person


def _matches(person: dict, query: str) -> bool:
    words = []
    for key, attr in (
        ("names", "displayName"),
        ("names", "givenName"),
        ("names", "familyName"),
        ("nicknames", "value"),
        ("emailAddresses", "value"),
        ("phoneNumbers", "value"),
        ("organizations", "name"),
    ):
        for item in person.get(key, []):
            words.extend(str(item.get(attr, "")).lower().replace("@", " @").split())
            words.append(str(item.get(attr, "")).lower())
    q = query.lower().strip()
    return any(w.startswith(q) for w in words if w)


def _search(book: dict, query: str, page_size: int, fields: set[str]) -> dict:
    if not query:
        return {}
    hits = [p for p in book.values() if _matches(p, query)][: max(1, min(page_size, 30))]
    return compact({"results": [{"person": _project(p, fields)} for p in hits]})


# --- people ------------------------------------------------------------------------------


@handler("people.people.get")
def get_person(ctx: Ctx):
    fields = _mask(ctx.q("personFields"), "personFields")
    return _project(_get_person(ctx.mailbox, ctx.path["resourceName"]), fields)


@handler("people.people.getBatchGet")
def batch_get(ctx: Ctx):
    fields = _mask(ctx.q("personFields"), "personFields")
    names = ctx.q("resourceNames") or []
    if not names or len(names) > 200:
        raise bad_request("resourceNames must contain between 1 and 200 entries")
    responses = []
    for rn in names:
        try:
            responses.append(
                {"requestedResourceName": rn, "person": _project(_get_person(ctx.mailbox, rn), fields), "httpStatusCode": 200, "status": {}}
            )
        except Exception:  # noqa: BLE001
            responses.append(
                {"requestedResourceName": rn, "httpStatusCode": 404, "status": {"code": 5, "message": "Requested entity was not found."}}
            )
    return {"responses": responses}


@handler("people.people.connections.list")
def list_connections(ctx: Ctx):
    if ctx.path["resourceName"] != "people/me":
        raise bad_request("Only people/me is supported for connections.list")
    fields = _mask(ctx.q("personFields"), "personFields")
    people = list(ctx.mailbox.contacts.values())
    order = ctx.q("sortOrder", "LAST_MODIFIED_ASCENDING")
    if order == "LAST_MODIFIED_DESCENDING":
        people.sort(key=lambda p: p["_updated"], reverse=True)
    elif order in ("FIRST_NAME_ASCENDING", "LAST_NAME_ASCENDING"):
        key = "givenName" if order == "FIRST_NAME_ASCENDING" else "familyName"
        people.sort(key=lambda p: ((p.get("names") or [{}])[0].get(key) or "").lower())
    else:
        people.sort(key=lambda p: p["_updated"])
    size = ctx.q("pageSize", 100) or 100
    if not 1 <= size <= 1000:
        raise bad_request("pageSize must be between 1 and 1000")
    page, token = paginate(people, ctx.q("pageToken"), size)
    return compact(
        {"connections": [_project(p, fields) for p in page], "nextPageToken": token, "totalPeople": len(people), "totalItems": len(people)}
    )


@handler("people.people.searchContacts")
def search_contacts(ctx: Ctx):
    fields = _mask(ctx.q("readMask"), "readMask")
    return _search(ctx.mailbox.contacts, ctx.q("query") or "", ctx.q("pageSize", 10) or 10, fields)


@handler("people.people.createContact")
def create_contact_handler(ctx: Ctx):
    fields = _mask(ctx.q("personFields"), "personFields") if ctx.q("personFields") else PERSON_FIELDS
    return _project(create_contact(ctx.mailbox, ctx.data), fields)


@handler("people.people.updateContact")
def update_contact(ctx: Ctx):
    box = ctx.mailbox
    person = box.contacts.get(ctx.path["resourceName"])
    if person is None:
        raise not_found()
    update_fields = _mask(ctx.q("updatePersonFields"), "updatePersonFields")
    if ctx.data.get("etag") and ctx.data["etag"] != person["etag"]:
        raise failed_precondition(
            "Request person.etag is different than the current person.etag. Clear local cache and get the latest person."
        )
    cleaned = _clean_input(ctx.data)
    for f in update_fields:
        if f in cleaned:
            person[f] = cleaned[f]
        else:
            person.pop(f, None)
    _stamp(person, "CONTACT")
    fields = _mask(ctx.q("personFields"), "personFields") if ctx.q("personFields") else PERSON_FIELDS
    return _project(person, fields)


@handler("people.people.deleteContact")
def delete_contact(ctx: Ctx):
    if ctx.mailbox.contacts.pop(ctx.path["resourceName"], None) is None:
        raise not_found()
    return {}


@handler("people.people.batchCreateContacts")
def batch_create(ctx: Ctx):
    fields = _mask(ctx.data.get("readMask"), "readMask")
    created = [create_contact(ctx.mailbox, c.get("contactPerson") or {}) for c in ctx.data.get("contacts") or []]
    return {
        "createdPeople": [
            {"person": _project(p, fields), "httpStatusCode": 200, "status": {}, "requestedResourceName": p["resourceName"]}
            for p in created
        ]
    }


@handler("people.people.batchUpdateContacts")
def batch_update(ctx: Ctx):
    box = ctx.mailbox
    update_fields = _mask(ctx.data.get("updateMask"), "updateMask")
    read_fields = _mask(ctx.data.get("readMask"), "readMask")
    results = {}
    for rn, body in (ctx.data.get("contacts") or {}).items():
        person = box.contacts.get(rn)
        if person is None:
            results[rn] = {
                "requestedResourceName": rn,
                "httpStatusCode": 404,
                "status": {"code": 5, "message": "Requested entity was not found."},
            }
            continue
        cleaned = _clean_input(body)
        for f in update_fields:
            if f in cleaned:
                person[f] = cleaned[f]
            else:
                person.pop(f, None)
        _stamp(person, "CONTACT")
        results[rn] = {"person": _project(person, read_fields), "httpStatusCode": 200, "status": {}, "requestedResourceName": rn}
    return {"updateResult": results}


@handler("people.people.batchDeleteContacts")
def batch_delete(ctx: Ctx):
    for rn in ctx.data.get("resourceNames") or []:
        ctx.mailbox.contacts.pop(rn, None)
    return {}


@handler("people.people.listDirectoryPeople", "people.people.searchDirectoryPeople")
def directory(ctx: Ctx):
    _mask(ctx.q("readMask"), "readMask")
    return {}  # consumer accounts have no domain directory


# --- other contacts ------------------------------------------------------------------------


@handler("people.otherContacts.list")
def list_other_contacts(ctx: Ctx):
    fields = _mask(ctx.q("readMask"), "readMask", OTHER_CONTACT_FIELDS)
    people = sorted(ctx.mailbox.other_contacts.values(), key=lambda p: p["_updated"])
    size = ctx.q("pageSize", 100) or 100
    page, token = paginate(people, ctx.q("pageToken"), min(size, 1000))
    return compact({"otherContacts": [_project(p, fields) for p in page], "nextPageToken": token, "totalSize": len(people)})


@handler("people.otherContacts.search")
def search_other_contacts(ctx: Ctx):
    fields = _mask(ctx.q("readMask"), "readMask", OTHER_CONTACT_FIELDS)
    return _search(ctx.mailbox.other_contacts, ctx.q("query") or "", ctx.q("pageSize", 10) or 10, fields)


@handler("people.otherContacts.copyOtherContactToMyContactsGroup")
def copy_other_contact(ctx: Ctx):
    box = ctx.mailbox
    source = box.other_contacts.get(ctx.path["resourceName"])
    if source is None:
        raise not_found()
    copy_fields = _mask(ctx.data.get("copyMask"), "copyMask", {"emailAddresses", "names", "phoneNumbers"})
    person = create_contact(box, {k: source[k] for k in copy_fields if k in source})
    read = _mask(ctx.data.get("readMask"), "readMask") if ctx.data.get("readMask") else PERSON_FIELDS
    return _project(person, read)


# --- contact groups -----------------------------------------------------------------------


def _groups(box) -> dict:
    if not box.contact_groups:
        for rn, name in SYSTEM_GROUPS.items():
            box.contact_groups[rn] = {
                "resourceName": rn,
                "groupType": "SYSTEM_CONTACT_GROUP",
                "name": rn.split("/")[1],
                "formattedName": name,
            }
    return box.contact_groups


def _group_resource(box, group: dict, max_members: int = 0) -> dict:
    gid = group["resourceName"].split("/")[1]
    members = [
        p["resourceName"]
        for p in box.contacts.values()
        if any(m.get("contactGroupMembership", {}).get("contactGroupId") == gid for m in p.get("memberships", []))
    ]
    out = {k: v for k, v in group.items() if not k.startswith("_")}
    out["etag"] = _etag(out)
    out["memberCount"] = len(members)
    if max_members:
        out["memberResourceNames"] = members[:max_members]
    return out


def _group(ctx: Ctx) -> dict:
    group = _groups(ctx.mailbox).get(ctx.path["resourceName"])
    if group is None:
        raise not_found()
    return group


@handler("people.contactGroups.list")
def list_groups(ctx: Ctx):
    groups = [_group_resource(ctx.mailbox, g) for g in _groups(ctx.mailbox).values()]
    page, token = paginate(groups, ctx.q("pageToken"), ctx.q("pageSize", 30) or 30)
    return compact({"contactGroups": page, "nextPageToken": token, "totalItems": len(groups)})


@handler("people.contactGroups.get")
def get_group(ctx: Ctx):
    return _group_resource(ctx.mailbox, _group(ctx), ctx.q("maxMembers", 0) or 0)


@handler("people.contactGroups.batchGet")
def batch_get_groups(ctx: Ctx):
    groups = _groups(ctx.mailbox)
    responses = []
    for rn in ctx.q("resourceNames") or []:
        group = groups.get(rn)
        if group is None:
            responses.append({"requestedResourceName": rn, "status": {"code": 5, "message": "Requested entity was not found."}})
        else:
            responses.append(
                {
                    "requestedResourceName": rn,
                    "contactGroup": _group_resource(ctx.mailbox, group, ctx.q("maxMembers", 0) or 0),
                    "status": {},
                }
            )
    return {"responses": responses}


@handler("people.contactGroups.create")
def create_group(ctx: Ctx):
    name = ((ctx.data.get("contactGroup") or {}).get("name") or "").strip()
    if not name:
        raise bad_request("Contact group name is required")
    groups = _groups(ctx.mailbox)
    if any(g["formattedName"].lower() == name.lower() for g in groups.values()):
        raise bad_request("Contact group name already exists", reason="failedPrecondition")
    rn = f"contactGroups/{hashlib.sha1(f'{name}{now_ms()}'.encode()).hexdigest()[:12]}"
    groups[rn] = {"resourceName": rn, "groupType": "USER_CONTACT_GROUP", "name": name, "formattedName": name}
    return _group_resource(ctx.mailbox, groups[rn])


@handler("people.contactGroups.update")
def update_group(ctx: Ctx):
    group = _group(ctx)
    if group["groupType"] == "SYSTEM_CONTACT_GROUP":
        raise bad_request("System contact groups cannot be modified")
    name = ((ctx.data.get("contactGroup") or {}).get("name") or "").strip()
    if name:
        group["name"] = group["formattedName"] = name
    return _group_resource(ctx.mailbox, group)


@handler("people.contactGroups.delete")
def delete_group(ctx: Ctx):
    group = _group(ctx)
    if group["groupType"] == "SYSTEM_CONTACT_GROUP":
        raise bad_request("System contact groups cannot be deleted")
    gid = group["resourceName"].split("/")[1]
    for person in ctx.mailbox.contacts.values():
        person["memberships"] = [
            m for m in person.get("memberships", []) if m.get("contactGroupMembership", {}).get("contactGroupId") != gid
        ]
    del ctx.mailbox.contact_groups[group["resourceName"]]
    return {}


@handler("people.contactGroups.members.modify")
def modify_members(ctx: Ctx):
    box = ctx.mailbox
    group = _group(ctx)
    gid = group["resourceName"].split("/")[1]
    missing = []
    for rn in ctx.data.get("resourceNamesToAdd") or []:
        person = box.contacts.get(rn)
        if person is None:
            missing.append(rn)
            continue
        memberships = person.setdefault("memberships", [])
        if not any(m.get("contactGroupMembership", {}).get("contactGroupId") == gid for m in memberships):
            memberships.append({"contactGroupMembership": {"contactGroupId": gid, "contactGroupResourceName": group["resourceName"]}})
    for rn in ctx.data.get("resourceNamesToRemove") or []:
        person = box.contacts.get(rn)
        if person is None:
            missing.append(rn)
            continue
        person["memberships"] = [
            m for m in person.get("memberships", []) if m.get("contactGroupMembership", {}).get("contactGroupId") != gid
        ]
    return compact({"notFoundResourceNames": missing})
