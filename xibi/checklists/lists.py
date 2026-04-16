from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from xibi.checklists.fuzzy import fuzzy_match_item

logger = logging.getLogger(__name__)


def _find_template(conn: sqlite3.Connection, list_name: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    result: sqlite3.Row | None = conn.execute(
        "SELECT * FROM checklist_templates WHERE LOWER(name) = LOWER(?)",
        (list_name,),
    ).fetchone()
    return result


def _find_active_instance(conn: sqlite3.Connection, template_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    result: sqlite3.Row | None = conn.execute(
        "SELECT * FROM checklist_instances WHERE template_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (template_id,),
    ).fetchone()
    return result


def _require_list(conn: sqlite3.Connection, list_name: str) -> tuple[dict, dict]:
    """Return (template, instance) or raise ValueError."""
    template = _find_template(conn, list_name)
    if not template:
        raise ValueError(f"List not found: {list_name!r}")
    instance = _find_active_instance(conn, template["id"])
    if not instance:
        raise ValueError(f"No active instance for list: {list_name!r}")
    return dict(template), dict(instance)


def create_list(db_path: str, name: str, description: str | None = None) -> dict:
    """Create a named list (template + active instance). Raises if name already exists."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing = _find_template(conn, name)
        if existing:
            raise ValueError(f"List already exists: {name!r}")

        template_id = str(uuid.uuid4())
        instance_id = str(uuid.uuid4())
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO checklist_templates (id, name, description, rollover_policy) VALUES (?, ?, ?, ?)",
            (template_id, name, description, "confirm"),
        )
        conn.execute(
            "INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES (?, ?, ?, ?)",
            (instance_id, template_id, now_str, "open"),
        )

    logger.info("create_list: created %r", name)
    return {"name": name}


def add_item(
    db_path: str,
    list_name: str,
    label: str,
    status: str = "open",
    metadata: dict | None = None,
) -> dict:
    """Add an item to a named list. Auto-creates the list if it doesn't exist."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        template = _find_template(conn, list_name)
        if not template:
            # auto-create
            template_id = str(uuid.uuid4())
            instance_id = str(uuid.uuid4())
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO checklist_templates (id, name, rollover_policy) VALUES (?, ?, ?)",
                (template_id, list_name, "confirm"),
            )
            conn.execute(
                "INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES (?, ?, ?, ?)",
                (instance_id, template_id, now_str, "open"),
            )
            instance_id_val = instance_id
        else:
            instance = _find_active_instance(conn, template["id"])
            if not instance:
                raise ValueError(f"No active instance for list: {list_name!r}")
            instance_id_val = instance["id"]

        row = conn.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM checklist_instance_items WHERE instance_id = ?",
            (instance_id_val,),
        ).fetchone()[0]
        position = row if row is not None else 0

        item_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO checklist_instance_items
               (id, instance_id, template_item_id, label, position, status, metadata, deadline_action_ids)
               VALUES (?, ?, NULL, ?, ?, ?, ?, '[]')""",
            (item_id, instance_id_val, label, position, status, json.dumps(metadata) if metadata is not None else None),
        )

    logger.info("add_item: added %r to list %r at position %d", label, list_name, position)
    return {"name": list_name, "position": position, "label": label, "status": status}


def remove_item(db_path: str, list_name: str, label_hint: str) -> dict:
    """Remove an item by fuzzy label match. Hard DELETE."""
    with sqlite3.connect(db_path) as conn:
        _, instance = _require_list(conn, list_name)
        instance_id = instance["id"]

        item = fuzzy_match_item(db_path, instance_id, label_hint)
        if item is None:
            raise ValueError(f"No unambiguous match for {label_hint!r} in list {list_name!r}")

        removed_label = item["label"]
        conn.execute("DELETE FROM checklist_instance_items WHERE id = ?", (item["id"],))

    logger.info("remove_item: removed %r from list %r", removed_label, list_name)
    return {"name": list_name, "removed": removed_label}


def update_item(
    db_path: str,
    list_name: str,
    label_hint: str,
    status: str | None = None,
    label: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Update an item's status, label, or metadata by fuzzy match."""
    with sqlite3.connect(db_path) as conn:
        _, instance = _require_list(conn, list_name)
        instance_id = instance["id"]

        item = fuzzy_match_item(db_path, instance_id, label_hint)
        if item is None:
            raise ValueError(f"No unambiguous match for {label_hint!r} in list {list_name!r}")

        new_label = label if label is not None else item["label"]
        new_status = status if status is not None else item["status"]
        new_metadata = json.dumps(metadata) if metadata is not None else item.get("metadata")

        conn.execute(
            "UPDATE checklist_instance_items SET label = ?, status = ?, metadata = ? WHERE id = ?",
            (new_label, new_status, new_metadata, item["id"]),
        )

    logger.info("update_item: updated %r in list %r → status=%r", item["label"], list_name, new_status)
    return {"name": list_name, "position": item["position"], "label": new_label, "status": new_status}


def show_list(db_path: str, list_name: str, status_filter: str | None = None) -> dict:
    """Show all items in a named list, optionally filtered by status."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _, instance = _require_list(conn, list_name)
        instance_id = instance["id"]

        all_items = conn.execute(
            "SELECT * FROM checklist_instance_items WHERE instance_id = ? ORDER BY position ASC",
            (instance_id,),
        ).fetchall()

        counts: dict[str, int] = {}
        for row in all_items:
            s = row["status"] or "open"
            counts[s] = counts.get(s, 0) + 1

        if status_filter:
            visible = [dict(r) for r in all_items if r["status"] == status_filter]
        else:
            visible = [dict(r) for r in all_items]

        items_out = []
        for r in visible:
            entry = {"position": r["position"], "label": r["label"], "status": r["status"]}
            if r["metadata"]:
                try:
                    entry["metadata"] = json.loads(r["metadata"])
                except (json.JSONDecodeError, TypeError):
                    entry["metadata"] = r["metadata"]
            items_out.append(entry)

    return {"name": list_name, "items": items_out, "counts": counts}
