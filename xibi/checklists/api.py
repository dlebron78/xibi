from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from xibi.checklists.fuzzy import fuzzy_match_item
from xibi.scheduling.api import disable_action, register_action

logger = logging.getLogger(__name__)


def create_checklist_template(
    db_path: str,
    name: str,
    description: str | None = None,
    items: list[dict] | None = None,
    recurrence: dict | None = None,
    rollover_policy: str = "confirm",
    nudge_config: dict | None = None,
) -> dict:
    """Create template and optionally schedule first instance."""
    template_id = str(uuid.uuid4())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO checklist_templates
            (id, name, description, recurrence, rollover_policy, nudge_config)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                name,
                description,
                json.dumps(recurrence) if recurrence else None,
                rollover_policy,
                json.dumps(nudge_config) if nudge_config else None,
            ),
        )

        if items:
            for i, item in enumerate(items):
                conn.execute(
                    """
                    INSERT INTO checklist_template_items
                    (id, template_id, position, label, item_type, deadline_offset_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        template_id,
                        i,
                        item["label"],
                        item.get("item_type", "human"),
                        item.get("deadline_offset_seconds"),
                    ),
                )

    recurrence_action_id = None
    if recurrence:
        recurrence_action_id = register_action(
            db_path=Path(db_path),
            name=f"Checklist recurrence: {name}",
            trigger_type=recurrence["trigger_type"],
            trigger_config=recurrence["trigger_config"],
            action_type="internal_hook",
            action_config={
                "hook": "checklist_fire_recurrence",
                "args": {"template_id": template_id},
            },
            created_by="user",
            created_via="checklists_module",
            trust_tier="green",
            enabled=True,
        )

    logger.info("create_checklist_template: Created template %s (%s)", template_id, name)
    return {
        "template_id": template_id,
        "name": name,
        "item_count": len(items) if items else 0,
        "recurrence_action_id": recurrence_action_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def update_checklist_item(
    db_path: str,
    instance_id: str,
    position: int | None = None,
    label_hint: str | None = None,
    status: str = "done",
) -> dict:
    """Mark item done/undone. Accepts EITHER position (strict) OR label_hint (fuzzy)."""

    if (position is None and label_hint is None) or (position is not None and label_hint is not None):
        raise ValueError("Provide exactly one of: position OR label_hint")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if position is not None:
            item = conn.execute(
                "SELECT * FROM checklist_instance_items WHERE instance_id = ? AND position = ?",
                (instance_id, position),
            ).fetchone()
        elif label_hint is not None:
            item = fuzzy_match_item(db_path, instance_id, label_hint)
        else:
            # Should be unreachable due to check above, but satisfies mypy
            raise ValueError("Provide exactly one of: position OR label_hint")

        if not item:
            raise ValueError(f"No item matched (position={position}, label_hint={label_hint})")

        item_id = item["id"]
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if status == "done" else None
        conn.execute(
            "UPDATE checklist_instance_items SET completed_at = ? WHERE id = ?",
            (now_str, item_id),
        )

        if status == "done":
            action_ids = json.loads(item["deadline_action_ids"] or "[]")
            for action_id in action_ids:
                try:
                    disable_action(Path(db_path), action_id)
                except Exception as e:
                    logger.error("update_checklist_item: Failed to disable action %s: %s", action_id, e)

        # Check if instance is fully closed
        remaining = conn.execute(
            "SELECT COUNT(*) FROM checklist_instance_items WHERE instance_id = ? AND completed_at IS NULL",
            (instance_id,),
        ).fetchone()[0]
        instance_fully_closed = False
        if remaining == 0:
            instance_fully_closed = True
            conn.execute(
                "UPDATE checklist_instances SET status = 'closed', closed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), instance_id),
            )

        return {
            "item_position": item["position"],
            "item_label": item["label"],
            "status": status,
            "instance_fully_closed": instance_fully_closed,
        }


def list_checklists(db_path: str) -> dict:
    """List all open checklist instances."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        instances = conn.execute(
            """
            SELECT i.id as instance_id, t.name as template_name, i.created_at, i.status
            FROM checklist_instances i
            JOIN checklist_templates t ON i.template_id = t.id
            WHERE i.status = 'open'
            ORDER BY i.created_at DESC
            """
        ).fetchall()

        result = []
        for inst in instances:
            counts = conn.execute(
                """
                SELECT COUNT(*) as total, COUNT(completed_at) as completed
                FROM checklist_instance_items
                WHERE instance_id = ?
                """,
                (inst["instance_id"],),
            ).fetchone()

            result.append(
                {
                    "instance_id": inst["instance_id"],
                    "template_name": inst["template_name"],
                    "created_at": inst["created_at"],
                    "item_count": counts["total"],
                    "completed_count": counts["completed"],
                    "open_count": counts["total"] - counts["completed"],
                    "status": inst["status"],
                }
            )
        return {"instances": result}


def get_checklist(db_path: str, instance_id: str) -> dict:
    """Get full state of one checklist instance."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        inst = conn.execute(
            """
            SELECT i.id as instance_id, t.name as template_name, i.created_at, i.status
            FROM checklist_instances i
            JOIN checklist_templates t ON i.template_id = t.id
            WHERE i.id = ?
            """,
            (instance_id,),
        ).fetchone()

        if not inst:
            raise ValueError(f"Instance {instance_id} not found")

        items = conn.execute(
            "SELECT * FROM checklist_instance_items WHERE instance_id = ? ORDER BY position ASC",
            (instance_id,),
        ).fetchall()

        now = datetime.now(timezone.utc)
        item_list = []
        for item in items:
            is_overdue = False
            if item["deadline_at"] and item["completed_at"] is None:
                deadline = datetime.fromisoformat(item["deadline_at"]).replace(tzinfo=timezone.utc)
                if now > deadline:
                    is_overdue = True

            item_list.append(
                {
                    "position": item["position"],
                    "label": item["label"],
                    "completed_at": item["completed_at"],
                    "deadline_at": item["deadline_at"],
                    "is_overdue": is_overdue,
                }
            )

        return {
            "instance_id": inst["instance_id"],
            "template_name": inst["template_name"],
            "created_at": inst["created_at"],
            "status": inst["status"],
            "items": item_list,
        }


def instantiate_checklist(
    db_path: str,
    template_id: str | None = None,
    template_name: str | None = None,
) -> dict:
    """Create an active instance from a checklist template.

    Accepts either template_id (exact) or template_name (case-insensitive substring match).
    This is the ad-hoc counterpart to the recurrence-triggered fire in lifecycle.py.
    """
    if not template_id and not template_name:
        raise ValueError("Provide either template_id or template_name")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Resolve template
        if template_id:
            template = conn.execute("SELECT * FROM checklist_templates WHERE id = ?", (template_id,)).fetchone()
            if not template:
                raise ValueError(f"Template {template_id} not found")
        else:
            if not template_name:
                raise ValueError("Must provide template_id or template_name")
            template = conn.execute(
                "SELECT * FROM checklist_templates WHERE LOWER(name) LIKE ?",
                (f"%{template_name.lower()}%",),
            ).fetchone()
            if not template:
                raise ValueError(f"No template matching '{template_name}'")

        template_id = template["id"]

        # Create instance
        instance_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES (?, ?, ?, ?)",
            (instance_id, template_id, now_str, "open"),
        )

        # Copy template items into instance items (no deadline scheduling — ad-hoc lists)
        template_items = conn.execute(
            "SELECT * FROM checklist_template_items WHERE template_id = ? ORDER BY position ASC",
            (template_id,),
        ).fetchall()
        for t_item in template_items:
            conn.execute(
                """
                INSERT INTO checklist_instance_items
                (id, instance_id, template_item_id, label, position, completed_at, deadline_at, deadline_action_ids)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, '[]')
                """,
                (
                    str(uuid.uuid4()),
                    instance_id,
                    t_item["id"],
                    t_item["label"],
                    t_item["position"],
                ),
            )

    logger.info(
        "instantiate_checklist: Created instance %s from template %s (%s)",
        instance_id,
        template_id,
        template["name"],
    )
    return {
        "instance_id": instance_id,
        "template_name": template["name"],
        "item_count": len(template_items),
        "created_at": now_str,
    }
