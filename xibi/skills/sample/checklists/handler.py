from __future__ import annotations

from typing import Any

from xibi.checklists import tools


def list_checklists(params: dict[str, Any]) -> dict[str, Any]:
    return tools.list_checklists(params)

def get_checklist(params: dict[str, Any]) -> dict[str, Any]:
    return tools.get_checklist(params)

def update_checklist_item(params: dict[str, Any]) -> dict[str, Any]:
    return tools.update_checklist_item(params)

def create_checklist_template(params: dict[str, Any]) -> dict[str, Any]:
    return tools.create_checklist_template(params)
