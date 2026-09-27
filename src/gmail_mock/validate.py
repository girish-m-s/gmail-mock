"""Request validation against discovery-document schemas.

Mirrors how Google's front end rejects requests: unknown query parameters,
unknown JSON fields, wrong types and bad enum values all become 400s.
snake_case field names are accepted and normalised to lowerCamelCase, as the
real proto-JSON front end does.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ApiError
from .spec import Api, Method, Param
from .util import b64decode_any, camel, snake

_INT_FORMATS = {"int64", "uint64"}
_PROTO_TYPES = {"int32": "TYPE_INT32", "uint32": "TYPE_UINT32", "int64": "TYPE_INT64", "uint64": "TYPE_UINT64"}


def _invalid_value(name: str, type_name: str, value: Any) -> ApiError:
    return ApiError(400, f"Invalid value at '{snake(name)}' ({type_name}), \"{value}\"")


def _coerce_param(p: Param, value: str) -> Any:
    if p.type == "integer":
        try:
            return int(value)
        except ValueError:
            raise _invalid_value(p.name, _PROTO_TYPES.get(p.format or "", "TYPE_INT32"), value) from None
    if p.type == "boolean":
        lowered = value.lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise _invalid_value(p.name, "TYPE_BOOL", value)
    if p.enum and value not in p.enum:
        raise _invalid_value(p.name, "TYPE_ENUM", value)
    return value


def validate_query(method: Method, items: list[tuple[str, str]]) -> dict[str, Any]:
    allowed = {**method.api.global_params, **method.query_params}
    out: dict[str, Any] = {}
    for key, value in items:
        p = allowed.get(key) or allowed.get(camel(key))
        if p is None:
            raise ApiError(
                400,
                f'Invalid JSON payload received. Unknown name "{key}": Cannot bind query parameter. '
                f"Field '{key}' could not be found in request message.",
            )
        coerced = _coerce_param(p, value)
        if p.repeated:
            out.setdefault(p.name, []).append(coerced)
        else:
            out[p.name] = coerced
    for name, p in method.query_params.items():
        if p.required and name not in out:
            raise ApiError(400, f"Required parameter: {name}", reason="required")
    return out


def validate_body(api: Api, ref: str, body: Any) -> Any:
    return _validate(api, {"$ref": ref}, body, "")


_B64 = re.compile(r"[A-Za-z0-9+/_\-\s]*={0,2}\s*")


def _looks_base64(value: str) -> bool:
    """Structural base64 check without decoding (payloads can be tens of MB)."""
    if len(value) < 4096:
        try:
            b64decode_any(value)
            return True
        except ValueError:
            return False
    return _B64.fullmatch(value) is not None and len(re.sub(r"[\s=]", "", value)) % 4 != 1


def _at(loc: str) -> str:
    return f" at '{loc}'" if loc else ""


def _validate(api: Api, schema: dict, value: Any, loc: str) -> Any:
    if value is None:
        return None
    if "$ref" in schema:
        schema = api.schemas[schema["$ref"]]
    typ = schema.get("type")

    if typ == "object":
        if not isinstance(value, dict):
            raise ApiError(400, f"Invalid JSON payload received. Expected an object{_at(loc)}.")
        props = schema.get("properties")
        addl = schema.get("additionalProperties")
        out = {}
        for key, item in value.items():
            if props is not None and (key in props or camel(key) in props):
                name = key if key in props else camel(key)
                child = _validate(api, props[name], item, f"{loc}.{name}" if loc else name)
                if child is not None:
                    out[name] = child
            elif addl is not None:
                out[key] = _validate(api, addl, item, f"{loc}[{key}]")
            elif props is None:
                out[key] = item
            else:
                raise ApiError(400, f'Invalid JSON payload received. Unknown name "{key}"{_at(loc)}: Cannot find field.')
        return out

    if typ == "array":
        if not isinstance(value, list):
            raise ApiError(400, f"Invalid JSON payload received. Expected a list{_at(loc)}.")
        items = schema.get("items", {})
        return [_validate(api, items, v, f"{loc}[{i}]") for i, v in enumerate(value)]

    if typ == "string":
        fmt = schema.get("format")
        if fmt in _INT_FORMATS:
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).lstrip("-").isdigit():
                raise ApiError(400, f"Invalid value at '{loc}' ({_PROTO_TYPES[fmt]}), \"{value}\"")
            return str(value)
        if not isinstance(value, str):
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_STRING), {value!r}")
        if fmt == "byte":
            if not _looks_base64(value):
                raise ApiError(400, f"Invalid value at '{loc}' (TYPE_BYTES), Base64 decoding failed for \"{value[:40]}\"") from None
        enum = schema.get("enum")
        if enum and value not in enum:
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_ENUM), \"{value}\"")
        return value

    if typ == "integer":
        if isinstance(value, bool):
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_INT32), {value}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_INT32), \"{value}\"") from None

    if typ == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_DOUBLE), \"{value}\"") from None

    if typ == "boolean":
        if not isinstance(value, bool):
            raise ApiError(400, f"Invalid value at '{loc}' (TYPE_BOOL), {value!r}")
        return value

    return value
