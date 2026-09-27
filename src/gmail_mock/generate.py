"""Schema-driven responses for methods without a stateful handler, stripe-mock style.

The response has the right shape and types; request fields with matching names
and types are copied back into it.
"""

from __future__ import annotations

from typing import Any

from .spec import Api

_STRING_EXAMPLES = {"google-datetime": "2026-01-01T00:00:00Z", "byte": "", "int64": "0", "uint64": "0", "google-fieldmask": ""}


def generate(api: Api, ref: str | None, reflect: Any = None) -> Any:
    if ref is None:
        return None
    return _gen(api, {"$ref": ref}, reflect, depth=0, seen=())


def _gen(api: Api, schema: dict, reflect: Any, depth: int, seen: tuple) -> Any:
    ref = schema.get("$ref")
    if ref:
        if ref in seen or depth > 6:
            return {}
        seen = (*seen, ref)
        schema = api.schemas[ref]
    typ = schema.get("type")
    if typ == "object":
        out = {}
        reflect = reflect if isinstance(reflect, dict) else {}
        for name, prop in (schema.get("properties") or {}).items():
            out[name] = _gen(api, prop, reflect.get(name), depth + 1, seen)
        return out
    if typ == "array":
        if isinstance(reflect, list):
            return [_gen(api, schema.get("items", {}), r, depth + 1, seen) for r in reflect]
        return [_gen(api, schema.get("items", {}), None, depth + 1, seen)] if depth < 3 else []
    if typ == "string":
        if isinstance(reflect, str):
            return reflect
        if schema.get("enum"):
            enum = [e for e in schema["enum"] if not e.endswith("_UNSPECIFIED")]
            return (enum or schema["enum"])[0]
        return _STRING_EXAMPLES.get(schema.get("format", ""), "string")
    if typ == "integer":
        return reflect if isinstance(reflect, int) and not isinstance(reflect, bool) else 0
    if typ == "number":
        return reflect if isinstance(reflect, (int, float)) else 0.0
    if typ == "boolean":
        return reflect if isinstance(reflect, bool) else False
    return reflect if reflect is not None else {}
