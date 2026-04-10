from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xibi.scheduling.api import disable_action, register_action
from xibi.scheduling.handlers import ExecutionContext, HandlerResult
from xibi.telegram.api import send_message_with_buttons, send_nudge

logger = logging.getLogger(__name__)


def _handle_fire_recurrence(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Internal hook: fire a new instance when template's recurrence triggers."""
    template_id = action_config["args"]["template_id"]
    db_path = str(ctx.db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        template = conn.execute("SELECT * FROM checklist_templates WHERE id = ?", (template_id,)).fetchone()
        if not template:
            return HandlerResult("error", "", f"Template {template_id} not found")

        # 1. Create new instance row
        instance_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES (?, ?, ?, ?)",
            (instance_id, template_id, now.strftime("%Y-%m-%d %H:%M:%S"), "open"),
        )

        # 2. Copy template items into instance items
        template_items = conn.execute(
            "SELECT * FROM checklist_template_items WHERE template_id = ?", (template_id,)
        ).fetchall()
        for t_item in template_items:
            item_id = str(uuid.uuid4())
            deadline_at = None
            if t_item["deadline_offset_seconds"]:
                deadline_at = now + timedelta(seconds=t_item["deadline_offset_seconds"])

            conn.execute(
                """
                INSERT INTO checklist_instance_items
                (id, instance_id, template_item_id, label, position, completed_at, deadline_at, deadline_action_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    instance_id,
                    t_item["id"],
                    t_item["label"],
                    t_item["position"],
                    None,
                    deadline_at.strftime("%Y-%m-%d %H:%M:%S") if deadline_at else None,
                    "[]",
                ),
            )

            # 3. If deadline is set, register THREE kernel actions
            if deadline_at:
                nudge_config = json.loads(template["nudge_config"] or "{}")
                action_ids = []

                try:
                    if not nudge_config.get("disable_warning_24h"):
                        action_ids.append(
                            register_action(
                                db_path=ctx.db_path,
                                name=f"Checklist deadline warning: {template['name']} / {t_item['label']}",
                                trigger_type="oneshot",
                                trigger_config={
                                    "fire_at": (deadline_at - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
                                },
                                action_type="internal_hook",
                                action_config={"hook": "checklist_warning_24h", "args": {"item_id": item_id}},
                                trust_tier="green",
                                created_via="checklists_module",
                            )
                        )

                    if not nudge_config.get("disable_deadline"):
                        action_ids.append(
                            register_action(
                                db_path=ctx.db_path,
                                name=f"Checklist deadline: {template['name']} / {t_item['label']}",
                                trigger_type="oneshot",
                                trigger_config={"fire_at": deadline_at.strftime("%Y-%m-%dT%H:%M:%S")},
                                action_type="internal_hook",
                                action_config={"hook": "checklist_deadline", "args": {"item_id": item_id}},
                                trust_tier="green",
                                created_via="checklists_module",
                            )
                        )

                    if not nudge_config.get("disable_nag_post_deadline"):
                        action_ids.append(
                            register_action(
                                db_path=ctx.db_path,
                                name=f"Checklist overdue nag: {template['name']} / {t_item['label']}",
                                trigger_type="oneshot",
                                trigger_config={
                                    "fire_at": (deadline_at + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
                                },
                                action_type="internal_hook",
                                action_config={"hook": "checklist_nag_post_deadline", "args": {"item_id": item_id}},
                                trust_tier="green",
                                created_via="checklists_module",
                            )
                        )
                    logger.info("checklist_fire_recurrence: Registered %d deadline actions for item %s", len(action_ids), item_id)
                except Exception as e:
                    logger.error("checklist_fire_recurrence: Failed to register deadline actions for item %s: %s", item_id, e)

                conn.execute(
                    "UPDATE checklist_instance_items SET deadline_action_ids = ? WHERE id = ?",
                    (json.dumps(action_ids), item_id),
                )

    # 4. Handle rollover from previous instance (if one exists)
    _handle_rollover(template_id, instance_id, db_path)

    logger.info("checklist_fire_recurrence: Instance %s created for template %s", instance_id, template_id)
    return HandlerResult("success", f"Instance {instance_id} created with {len(template_items)} items")


def _handle_rollover(template_id: str, new_instance_id: str, db_path: str) -> None:
    """Check if previous instance has stale open items; apply rollover policy."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        template = conn.execute("SELECT * FROM checklist_templates WHERE id = ?", (template_id,)).fetchone()

        # Get the previous instance (most recent one before new_instance_id)
        prev_instance = conn.execute(
            "SELECT * FROM checklist_instances WHERE template_id = ? AND id != ? ORDER BY created_at DESC LIMIT 1",
            (template_id, new_instance_id),
        ).fetchone()

        if not prev_instance:
            return

        open_items = conn.execute(
            "SELECT * FROM checklist_instance_items WHERE instance_id = ? AND completed_at IS NULL",
            (prev_instance["id"],),
        ).fetchall()

        if not open_items:
            return

        policy = template["rollover_policy"]
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if policy == "expire":
            conn.execute(
                "UPDATE checklist_instances SET status = 'expired', closed_at = ? WHERE id = ?",
                (now_str, prev_instance["id"]),
            )

        elif policy == "roll_forward":
            for item in open_items:
                conn.execute(
                    """
                    INSERT INTO checklist_instance_items
                    (id, instance_id, template_item_id, label, position, completed_at, deadline_at, deadline_action_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        new_instance_id,
                        item["template_item_id"],
                        item["label"],
                        item["position"],
                        None,
                        None,
                        "[]",
                    ),
                )
            conn.execute(
                "UPDATE checklist_instances SET status = 'expired', closed_at = ? WHERE id = ?",
                (now_str, prev_instance["id"]),
            )

        elif policy == "nag":
            labels = [i["label"] for i in open_items]
            send_nudge(f"Previous '{template['name']}' still has open items: {', '.join(labels)}")
            conn.execute(
                "UPDATE checklist_instances SET status = 'expired', closed_at = ? WHERE id = ?",
                (now_str, prev_instance["id"]),
            )

        elif policy == "confirm":
            for item in open_items:
                buttons = [
                    {"text": "✓ Done", "callback_data": f"checklist_rollover_done:{item['id']}"},
                    {"text": "✗ Drop", "callback_data": f"checklist_rollover_drop:{item['id']}"},
                    {"text": "→ Carry forward", "callback_data": f"checklist_rollover_carry:{item['id']}"},
                ]
                send_message_with_buttons(f"Rollover: is '{item['label']}' still relevant?", buttons)
                conn.execute(
                    "UPDATE checklist_instance_items SET rollover_prompted_at = ? WHERE id = ?", (now_str, item["id"])
                )


def _handle_rollover_timeout(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Periodic: every 6h, expire rollover-prompted items that are >48h old."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    db_path = str(ctx.db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        items = conn.execute(
            "SELECT * FROM checklist_instance_items WHERE rollover_prompted_at IS NOT NULL AND rollover_prompted_at < ?",
            (cutoff_str,),
        ).fetchall()

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            instance = conn.execute("SELECT * FROM checklist_instances WHERE id = ?", (item["instance_id"],)).fetchone()
            if instance and instance["status"] == "open":
                conn.execute(
                    "UPDATE checklist_instances SET status = 'expired', closed_at = ? WHERE id = ?",
                    (now_str, instance["id"]),
                )

    return HandlerResult("success", f"Expired {len(items)} stale rollover items")


def handle_rollover_callback(action: str, item_id: str, db_path: Path) -> str:
    """Handle callback from rollover confirmation buttons."""
    db_path_str = str(db_path)
    with sqlite3.connect(db_path_str) as conn:
        conn.row_factory = sqlite3.Row
        item = conn.execute("SELECT * FROM checklist_instance_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return "Item not found."

        instance = conn.execute("SELECT * FROM checklist_instances WHERE id = ?", (item["instance_id"],)).fetchone()
        template_id = instance["template_id"]

        # Get newest instance for that template to carry forward if needed
        new_instance = conn.execute(
            "SELECT id FROM checklist_instances WHERE template_id = ? ORDER BY created_at DESC LIMIT 1", (template_id,)
        ).fetchone()

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if action == "done":
            conn.execute("UPDATE checklist_instance_items SET completed_at = ? WHERE id = ?", (now_str, item_id))
            # If item is marked DONE, cancel any pending deadline actions
            action_ids = json.loads(item["deadline_action_ids"] or "[]")
            for aid in action_ids:
                try:
                    disable_action(db_path, aid)
                except Exception as e:
                    logger.error("handle_rollover_callback: Failed to disable action %s: %s", aid, e)

            # Check if all items in old instance are now done/expired
            remaining = conn.execute(
                "SELECT COUNT(*) FROM checklist_instance_items WHERE instance_id = ? AND completed_at IS NULL",
                (instance["id"],),
            ).fetchone()[0]
            if remaining == 0:
                conn.execute(
                    "UPDATE checklist_instances SET status = 'closed', closed_at = ? WHERE id = ?",
                    (now_str, instance["id"]),
                )
            return f"Marked '{item['label']}' as done."

        elif action == "drop":
            # Just clear the prompt, item stays incomplete in old instance which is likely already marked expired or will be
            conn.execute("UPDATE checklist_instance_items SET rollover_prompted_at = NULL WHERE id = ?", (item_id,))
            return f"Dropped '{item['label']}'."

        elif action == "carry":
            if new_instance and new_instance["id"] != instance["id"]:
                conn.execute(
                    """
                    INSERT INTO checklist_instance_items
                    (id, instance_id, template_item_id, label, position, completed_at, deadline_at, deadline_action_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        new_instance["id"],
                        item["template_item_id"],
                        item["label"],
                        item["position"],
                        None,
                        None,
                        "[]",
                    ),
                )
                conn.execute("UPDATE checklist_instance_items SET rollover_prompted_at = NULL WHERE id = ?", (item_id,))
                return f"Carried '{item['label']}' forward to new instance."
            else:
                return "Could not carry forward: no new instance found."

    return "Unknown action."
