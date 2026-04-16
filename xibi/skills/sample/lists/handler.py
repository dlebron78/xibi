from __future__ import annotations

from typing import Any

from xibi.checklists import lists


def create_list(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    name = params.get("name")
    if not name:
        return {"status": "error", "error": "name is required"}
    try:
        return lists.create_list(str(db_path), name, params.get("description"))
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def add_to_list(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    list_name = params.get("list_name")
    label = params.get("label")
    if not list_name or not label:
        return {"status": "error", "error": "list_name and label are required"}
    try:
        return lists.add_item(
            str(db_path),
            list_name,
            label,
            status=params.get("status", "open"),
            metadata=params.get("metadata"),
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def remove_from_list(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    list_name = params.get("list_name")
    item = params.get("item")
    if not list_name or not item:
        return {"status": "error", "error": "list_name and item are required"}
    try:
        return lists.remove_item(str(db_path), list_name, item)
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def update_list_item(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    list_name = params.get("list_name")
    item = params.get("item")
    if not list_name or not item:
        return {"status": "error", "error": "list_name and item are required"}
    try:
        return lists.update_item(
            str(db_path),
            list_name,
            item,
            status=params.get("status"),
            label=params.get("label"),
            metadata=params.get("metadata"),
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def show_list(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    list_name = params.get("list_name")
    if not list_name:
        return {"status": "error", "error": "list_name is required"}
    try:
        return lists.show_list(str(db_path), list_name, params.get("status_filter"))
    except ValueError as e:
        return {"status": "error", "error": str(e)}
