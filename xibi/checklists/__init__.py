from __future__ import annotations

import logging
from pathlib import Path

from xibi.checklists.handlers import _handle_deadline, _handle_nag_post_deadline, _handle_warning_24h
from xibi.checklists.lifecycle import _handle_fire_recurrence, _handle_rollover_timeout
from xibi.scheduling.api import register_action
from xibi.scheduling.handlers import register_internal_hook

logger = logging.getLogger(__name__)

def initialize_checklists(db_path: Path) -> None:
    """Register handlers and periodic actions."""
    register_internal_hook("checklist_warning_24h", _handle_warning_24h)
    register_internal_hook("checklist_deadline", _handle_deadline)
    register_internal_hook("checklist_nag_post_deadline", _handle_nag_post_deadline)
    register_internal_hook("checklist_fire_recurrence", _handle_fire_recurrence)
    register_internal_hook("checklist_rollover_timeout", _handle_rollover_timeout)

    # Register periodic rollover timeout cleanup (idempotent)
    from xibi.scheduling.api import list_actions
    existing = list_actions(db_path)
    if not any(a["name"] == "Checklist rollover timeout cleanup" for a in existing):
        register_action(
            db_path=db_path,
            name="Checklist rollover timeout cleanup",
            trigger_type="interval",
            trigger_config={"interval_seconds": 21600},  # 6h
            action_type="internal_hook",
            action_config={"hook": "checklist_rollover_timeout", "args": {}},
            created_by="system",
            created_via="checklists_module",
            trust_tier="green",
            enabled=True,
        )
    logger.info("Checklist module initialized")
