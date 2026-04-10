from __future__ import annotations

from typing import Any

from xibi.checklists import tools


def run(params: dict[str, Any]) -> dict[str, Any]:
    return tools.create_checklist_template(params)
