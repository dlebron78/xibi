"""transform_data — apply a pipeline of operations to a handle's payload.

The dispatcher in react.py resolves handle IDs to their raw payloads before
calling tools, so `params["handle"]` contains the actual data by the time
run() is called.

Supported operations (applied in sequence):
  filter   — keep items where item[field] <op> value
  sort     — sort items by field (order: "asc" | "desc")
  slice    — items[start:end]
  group_by — partition list into dict-of-lists keyed by field value
  dedupe   — remove items with duplicate field values (first wins)
  project  — keep only the specified fields on each item

For dict payloads with a single list field (e.g. {"jobs": [...]}), operations
are applied to the inner list and the result is re-wrapped in the same key.

Missing-field semantics (consistent across all ops): if an item is missing
the field a given op targets, the item is handled leniently rather than
raising. See `_get_field` for the full rules.
"""
from __future__ import annotations

import operator
from typing import Any


def run(params: dict) -> Any:
    payload = params.get("handle")
    operations: list[dict] = params.get("operations") or []

    if payload is None:
        return {"status": "error", "message": "Missing handle param"}

    # Unwrap dict-with-single-list payloads (e.g. {"jobs": [...]})
    _wrapper_key: str | None = None
    data: list | Any = payload
    if isinstance(payload, dict):
        list_keys = [k for k, v in payload.items() if isinstance(v, list)]
        if len(list_keys) == 1:
            _wrapper_key = list_keys[0]
            data = payload[_wrapper_key]

    try:
        for op_spec in operations:
            op = op_spec.get("op")
            args = op_spec.get("args") or {}
            data = _apply_op(data, op, args)
    except Exception as e:
        return {"status": "error", "message": f"Transformation error: {e}"}

    if _wrapper_key is not None and not isinstance(data, dict):
        return {_wrapper_key: data}
    return data


def _apply_op(data: Any, op: str, args: dict) -> Any:
    if op == "filter":
        return _filter(data, args)
    if op == "sort":
        return _sort(data, args)
    if op == "slice":
        return _slice(data, args)
    if op == "group_by":
        return _group_by(data, args)
    if op == "dedupe":
        return _dedupe(data, args)
    if op == "project":
        return _project(data, args)
    raise ValueError(f"Unknown operation: {op!r}")


# --- individual ops ---

_OPS: dict[str, Any] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


# Sentinel for "field not present on item" — distinct from a real None value
# so ops can choose how to handle missingness consistently.
_MISSING = object()


def _get_field(item: Any, field: str) -> Any:
    """Lenient field access: returns _MISSING if the field isn't present.

    All transform_data ops treat missing fields the same way: the item is
    considered to have no value for that field, and each op decides what
    that means (filter → excluded, sort → sorted last, group_by → bucketed
    under "__missing__", dedupe → collapsed into a single "missing" bucket).
    Non-dict items are also treated as missing rather than raising.
    """
    if not isinstance(item, dict):
        return _MISSING
    if field not in item:
        return _MISSING
    return item[field]


def _filter(data: list, args: dict) -> list:
    field = args["field"]
    op_str = args["op"]
    value = args["value"]
    cmp = _OPS.get(op_str)
    if cmp is None:
        raise ValueError(f"Unknown filter op: {op_str!r}")
    result = []
    for item in data:
        val = _get_field(item, field)
        if val is _MISSING:
            # Lenient: missing field → item is excluded from the filter.
            continue
        try:
            if cmp(val, value):
                result.append(item)
        except TypeError:
            # Incomparable types (e.g. None vs int) → item excluded.
            continue
    return result


def _sort(data: list, args: dict) -> list:
    field = args["field"]
    reverse = str(args.get("order", "asc")).lower() == "desc"

    # None-safe, missing-safe, type-safe sort key: missing values sort last
    # (regardless of order), and values are grouped by type before comparison
    # so heterogeneous lists don't blow up on None < int or str < int.
    def _key(item: Any) -> tuple:
        val = _get_field(item, field)
        if val is _MISSING or val is None:
            return (1, 0, "")
        return (0, type(val).__name__, val)

    return sorted(data, key=_key, reverse=reverse)


def _slice(data: list, args: dict) -> list:
    start = int(args.get("start", 0))
    end = args.get("end")
    return data[start:end]


def _group_by(data: list, args: dict) -> dict:
    field = args["field"]
    result: dict[str, list] = {}
    for item in data:
        val = _get_field(item, field)
        key = "__missing__" if val is _MISSING else str(val)
        result.setdefault(key, []).append(item)
    return result


def _dedupe(data: list, args: dict) -> list:
    field = args["field"]
    seen: set = set()
    out: list = []
    missing_seen = False
    for item in data:
        val = _get_field(item, field)
        if val is _MISSING:
            if missing_seen:
                continue
            missing_seen = True
            out.append(item)
            continue
        if val not in seen:
            seen.add(val)
            out.append(item)
    return out


def _project(data: list, args: dict) -> list:
    fields: list[str] = args["fields"]
    return [{k: item[k] for k in fields if k in item} for item in data]
