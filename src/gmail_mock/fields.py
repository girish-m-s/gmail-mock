"""Partial responses: the ``fields`` query parameter, e.g. ``messages(id,threadId),nextPageToken``."""

from __future__ import annotations

from typing import Any

from .errors import bad_request

Tree = dict[str, "Tree | bool"]


def parse(spec: str) -> Tree:
    tree, rest = _parse_list(spec.replace(" ", ""), 0)
    if rest != len(spec.replace(" ", "")):
        raise bad_request(f"Invalid field selection {spec}")
    return tree


def _merge(tree: Tree, path: list[str], sub: Tree | bool) -> None:
    node = tree
    for part in path[:-1]:
        nxt = node.get(part)
        if nxt is True:
            return
        if not isinstance(nxt, dict):
            nxt = node[part] = {}
        node = nxt
    last = path[-1]
    if sub is True or node.get(last) is True:
        node[last] = True
    else:
        existing = node.get(last)
        if isinstance(existing, dict):
            for k, v in sub.items():
                _merge(existing, [k], v)
        else:
            node[last] = sub


def _parse_list(s: str, i: int) -> tuple[Tree, int]:
    tree: Tree = {}
    while i < len(s):
        j = i
        while j < len(s) and s[j] not in ",()":
            j += 1
        path = [p for p in s[i:j].split("/") if p]
        if not path:
            raise bad_request(f"Invalid field selection {s}")
        sub: Tree | bool = True
        if j < len(s) and s[j] == "(":
            sub, j = _parse_list(s, j + 1)
            if j >= len(s) or s[j] != ")":
                raise bad_request(f"Invalid field selection {s}")
            j += 1
        _merge(tree, path, sub)
        if j < len(s) and s[j] == ",":
            i = j + 1
            continue
        return tree, j
    return tree, i


def apply(value: Any, tree: Tree | bool) -> Any:
    if tree is True:
        return value
    if isinstance(value, list):
        return [apply(v, tree) for v in value]
    if isinstance(value, dict):
        return {k: apply(value[k], sub) for k, sub in tree.items() if k in value}
    return value
