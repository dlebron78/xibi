from __future__ import annotations

import logging
import sqlite3

from xibi.scheduling.handlers import ExecutionContext, HandlerResult
from xibi.telegram.api import send_nudge

logger = logging.getLogger(__name__)


def _is_item_open(db_path: str, item_id: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        item = conn.execute("SELECT completed_at FROM checklist_instance_items WHERE id = ?", (item_id,)).fetchone()
        return item is not None and item["completed_at"] is None


def _get_item_label(db_path: str, item_id: str) -> str:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        item = conn.execute("SELECT label FROM checklist_instance_items WHERE id = ?", (item_id,)).fetchone()
        return item["label"] if item else "Unknown item"


def _handle_warning_24h(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire 24h before an item deadline. Dumb handler: posts a nudge if item is still open."""
    item_id = action_config.get("item_id")
    if not item_id:
        return HandlerResult("error", "", "Missing item_id in action_config")

    if _is_item_open(str(ctx.db_path), item_id):
        label = _get_item_label(str(ctx.db_path), item_id)
        send_nudge(f"Reminder: {label} is due in 24h", category="info")
        return HandlerResult("success", f"24h warning nudge posted for {item_id}")
    else:
        return HandlerResult("success", f"item {item_id} already completed, skipped nudge")


def _handle_deadline(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire at item deadline. Posts a stronger nudge."""
    item_id = action_config.get("item_id")
    if not item_id:
        return HandlerResult("error", "", "Missing item_id in action_config")

    if _is_item_open(str(ctx.db_path), item_id):
        label = _get_item_label(str(ctx.db_path), item_id)
        send_nudge(f"Deadline NOW: {label}", category="alert")
        return HandlerResult("success", f"deadline nudge posted for {item_id}")
    else:
        return HandlerResult("success", f"item {item_id} already completed, skipped nudge")


def _handle_nag_post_deadline(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire 24h after item deadline. Posts a nag if item is still open."""
    item_id = action_config.get("item_id")
    if not item_id:
        return HandlerResult("error", "", "Missing item_id in action_config")

    if _is_item_open(str(ctx.db_path), item_id):
        label = _get_item_label(str(ctx.db_path), item_id)
        send_nudge(f"OVERDUE: {label}", category="urgent")
        return HandlerResult("success", f"overdue nag posted for {item_id}")
    else:
        return HandlerResult("success", f"item {item_id} already completed, skipped nudge")
