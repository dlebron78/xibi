from __future__ import annotations

from typing import Any
from xibi.checklists import api

def list_checklists(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    return api.list_checklists(str(db_path))

def get_checklist(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    instance_id = params.get("instance_id")
    if not instance_id:
        return {"status": "error", "error": "instance_id is required"}
    return api.get_checklist(str(db_path), instance_id)

def update_checklist_item(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    instance_id = params.get("instance_id")
    position = params.get("position")
    label_hint = params.get("label_hint")
    status = params.get("status", "done")

    if not instance_id:
        return {"status": "error", "error": "instance_id is required"}

    try:
        return api.update_checklist_item(
            str(db_path), instance_id, position=position, label_hint=label_hint, status=status
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}

def create_checklist_template(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    name = params.get("name")
    description = params.get("description")
    items = params.get("items")
    recurrence = params.get("recurrence")
    rollover_policy = params.get("rollover_policy", "confirm")
    nudge_config = params.get("nudge_config")

    if not name:
        return {"status": "error", "error": "name is required"}
    if not items:
        return {"status": "error", "error": "items list is required"}

    return api.create_checklist_template(
        str(db_path), name, description, items, recurrence, rollover_policy, nudge_config
    )
