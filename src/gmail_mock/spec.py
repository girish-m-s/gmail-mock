"""Route catalog built from Google's API discovery documents.

Like stripe-mock's use of the OpenAPI spec, every URL the mock answers comes from
the bundled discovery docs in ``discovery/``. Refresh them with ``make update-spec``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from importlib import resources
from urllib.parse import unquote

SPEC_FILES = {"gmail": "gmail_v1.json", "people": "people_v1.json"}


def load_discovery(name: str) -> dict:
    return json.loads(resources.files("gmail_mock.discovery").joinpath(SPEC_FILES[name]).read_text())


@dataclass
class Param:
    name: str
    location: str
    type: str = "string"
    required: bool = False
    repeated: bool = False
    enum: list[str] | None = None
    format: str | None = None
    pattern: str | None = None
    default: str | None = None

    @classmethod
    def from_discovery(cls, name: str, d: dict) -> Param:
        return cls(
            name=name,
            location=d.get("location", "query"),
            type=d.get("type", "string"),
            required=bool(d.get("required")),
            repeated=bool(d.get("repeated")),
            enum=d.get("enum"),
            format=d.get("format"),
            pattern=d.get("pattern"),
            default=d.get("default"),
        )


@dataclass
class Route:
    regex: re.Pattern
    weight: int
    is_upload: bool
    method: Method


@dataclass
class Method:
    api: Api
    id: str
    http_method: str
    path: str
    params: dict[str, Param]
    request_ref: str | None
    response_ref: str | None
    upload_paths: list[str] = field(default_factory=list)

    @cached_property
    def query_params(self) -> dict[str, Param]:
        return {k: v for k, v in self.params.items() if v.location == "query"}


def _compile(template: str, params: dict[str, Param]) -> tuple[re.Pattern, int]:
    template = template.lstrip("/")
    out, weight, pos = [], 0, 0
    for m in re.finditer(r"\{(\+?)([^}]+)\}", template):
        literal = template[pos : m.start()]
        out.append(re.escape(literal))
        weight += len(literal)
        name = m.group(2)
        param = params.get(name)
        if m.group(1):
            inner = param.pattern.strip("^$") if param and param.pattern else ".+"
        else:
            inner = "[^/]+"
        out.append(f"(?P<{name}>{inner})")
        pos = m.end()
    literal = template[pos:]
    out.append(re.escape(literal))
    weight += len(literal)
    return re.compile("^/" + "".join(out) + "$"), weight


class Api:
    def __init__(self, name: str, doc: dict) -> None:
        self.name = name
        self.doc = doc
        self.schemas: dict[str, dict] = doc["schemas"]
        self.global_params = {k: Param.from_discovery(k, v) for k, v in doc.get("parameters", {}).items()}
        self.methods: dict[str, Method] = {}
        self._walk(doc.get("resources", {}))

    def _walk(self, resources_: dict) -> None:
        for res in resources_.values():
            for m in res.get("methods", {}).values():
                params = {k: Param.from_discovery(k, v) for k, v in m.get("parameters", {}).items()}
                uploads = [p["path"] for p in m.get("mediaUpload", {}).get("protocols", {}).values()]
                self.methods[m["id"]] = Method(
                    api=self,
                    id=m["id"],
                    http_method=m["httpMethod"],
                    path=m["path"],
                    params=params,
                    request_ref=m.get("request", {}).get("$ref"),
                    response_ref=m.get("response", {}).get("$ref"),
                    upload_paths=uploads,
                )
            self._walk(res.get("resources", {}))


class Catalog:
    def __init__(self) -> None:
        self.apis = {name: Api(name, load_discovery(name)) for name in SPEC_FILES}
        routes: list[Route] = []
        for api in self.apis.values():
            for method in api.methods.values():
                regex, weight = _compile(method.path, method.params)
                routes.append(Route(regex, weight, False, method))
                for upload in method.upload_paths:
                    regex, weight = _compile(upload, method.params)
                    routes.append(Route(regex, weight, True, method))
        # Prefer the most literal template when several match (e.g. messages/send vs messages/{id}).
        routes.sort(key=lambda r: -r.weight)
        self.routes = routes

    @property
    def methods(self) -> dict[str, Method]:
        return {mid: m for api in self.apis.values() for mid, m in api.methods.items()}

    def match(self, http_method: str, raw_path: str) -> tuple[Method, dict[str, str], bool] | None:
        for route in self.routes:
            if route.method.http_method != http_method:
                continue
            m = route.regex.match(raw_path)
            if m:
                return route.method, {k: unquote(v) for k, v in m.groupdict().items()}, route.is_upload
        return None
