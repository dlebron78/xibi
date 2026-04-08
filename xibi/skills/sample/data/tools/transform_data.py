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


def _filter(data: list, args: dict) -> list:
    field = args["field"]
    op_str = args["op"]
    value = args["value"]
    cmp = _OPS.get(op_str)
    if cmp is None:
        raise ValueError(f"Unknown filter op: {op_str!r}")
    result = []
    for item in data:
        if field not in item:
            raise KeyError(f"Field {field!r} not found in item")
        result.append(item) if cmp(item[field], value) else None
    return result


def _sort(data: list, args: dict) -> list:
    field = args["field"]
    reverse = str(args.get("order", "asc")).lower() == "desc"
    return sorted(data, key=lambda item: item.get(field), reverse=reverse)


def _slice(data: list, args: dict) -> list:
    start = int(args.get("start", 0))
    end = args.get("end")
    return data[start:end]


def _group_by(data: list, args: dict) -> dict:
    field = args["field"]
    result: dict[str, list] = {}
    for item in data:
        key = str(item.get(field, "__missing__"))
        result.setdefault(key, []).append(item)
    return result


def _dedupe(data: list, args: dict) -> list:
    field = args["field"]
    seen: set = set()
    out: list = []
    for item in data:
        val = item.get(field)
        if val not in seen:
            seen.add(val)
            out.append(item)
    return out


def _project(data: list, args: dict) -> list:
    fields: list[str] = args["fields"]
    return [{k: item[k] for k in fields if k in item} for item in data]
