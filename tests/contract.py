"""Strict response validation against the discovery schemas.

Every JSON response the mock returns during the test run is checked here: unknown
fields, wrong JSON types, int64 values not sent as strings, bad enums and
malformed base64/timestamps are all reported as violations.
"""

from __future__ import annotations

import re
from typing import Any

from gmail_mock.spec import Api
from gmail_mock.util import b64decode_any

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def check_response(api: Api, ref: str, value: Any) -> list[str]:
    errors: list[str] = []
    _check(api, {"$ref": ref}, value, ref, errors)
    return errors


def _check(api: Api, schema: dict, value: Any, loc: str, errors: list[str]) -> None:
    if "$ref" in schema:
        schema = api.schemas[schema["$ref"]]
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(value, dict):
            errors.append(f"{loc}: expected object, got {type(value).__name__}")
            return
        props = schema.get("properties")
        addl = schema.get("additionalProperties")
        for key, item in value.items():
            if props is not None and key in props:
                _check(api, props[key], item, f"{loc}.{key}", errors)
            elif addl is not None:
                _check(api, addl, item, f"{loc}[{key}]", errors)
            elif props is not None:
                errors.append(f"{loc}: unknown field {key!r}")
    elif typ == "array":
        if not isinstance(value, list):
            errors.append(f"{loc}: expected array, got {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _check(api, schema.get("items", {}), item, f"{loc}[{i}]", errors)
    elif typ == "string":
        if not isinstance(value, str):
            errors.append(f"{loc}: expected string, got {type(value).__name__} ({value!r})")
            return
        fmt = schema.get("format")
        if fmt in ("int64", "uint64") and not re.fullmatch(r"-?\d+", value):
            errors.append(f"{loc}: {fmt} string is not numeric: {value!r}")
        elif fmt == "byte":
            try:
                b64decode_any(value)
            except ValueError:
                errors.append(f"{loc}: invalid base64")
        elif fmt == "google-datetime" and not _RFC3339.match(value):
            errors.append(f"{loc}: invalid RFC 3339 timestamp {value!r}")
        enum = schema.get("enum")
        if enum and value not in enum:
            errors.append(f"{loc}: {value!r} not in enum")
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{loc}: expected integer, got {type(value).__name__} ({value!r})")
        elif schema.get("format") == "uint32" and value < 0:
            errors.append(f"{loc}: negative uint32")
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{loc}: expected number")
    elif typ == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{loc}: expected boolean, got {type(value).__name__}")
