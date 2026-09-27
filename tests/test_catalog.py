import json
from pathlib import Path

from gmail_mock.handlers import REGISTRY
from gmail_mock.spec import Catalog

ROOT = Path(__file__).resolve().parents[1]
FALLBACK_ONLY = {"people.people.deleteContactPhoto", "people.people.updateContactPhoto"}


def test_every_gmail_method_is_stateful():
    methods = Catalog().apis["gmail"].methods
    assert len(methods) >= 79
    assert sorted(m for m in methods if m not in REGISTRY) == []


def test_people_methods_are_stateful_except_photos():
    methods = Catalog().apis["people"].methods
    assert {m for m in methods if m not in REGISTRY} == FALLBACK_ONLY


def test_connector_tools_map_to_real_endpoints():
    doc = json.loads((ROOT / "connectors" / "gmail_tools.json").read_text())
    methods = Catalog().methods
    assert len(doc["tools"]) == 59
    assert len(doc["triggers"]) == 2
    for tool in doc["tools"] + doc["triggers"]:
        for mid in tool["methods"]:
            assert mid in methods, (tool["title"], mid)
            assert mid in REGISTRY, (tool["title"], mid)


def test_unknown_url_is_google_style_404(http):
    resp = http.get("/gmail/v1/users/me/nope")
    assert resp.status_code == 404
    assert "was not found on this server" in resp.text


def test_discovery_doc_points_at_mock(http, base_url):
    doc = http.get("/discovery/v1/apis/gmail/v1/rest").json()
    assert doc["rootUrl"] == base_url + "/"
    assert http.get("/$discovery/rest", params={"version": "v1"}).json()["name"] == "gmail"


def test_fallback_generates_schema_shaped_response(http):
    resp = http.delete("/v1/people/c123:deleteContactPhoto")
    assert resp.status_code == 200
    assert "person" in resp.json()


def test_coverage_endpoint(http):
    body = http.get("/_mock/coverage").json()
    assert body["total"] == len(Catalog().methods)
    assert body["stateful"] == body["total"] - len(FALLBACK_ONLY)
