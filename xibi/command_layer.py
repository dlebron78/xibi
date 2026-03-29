from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.db import open_db
from xibi.tools import PermissionTier, resolve_tier, validate_schema

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    allowed: bool  # False → caller should NOT execute
    tier: PermissionTier
    validation_errors: list[str]  # non-empty → schema invalid
    dedup_suppressed: bool  # True → duplicate detected, suppressed
    audit_required: bool  # True → caller must write audit log entry
    block_reason: str  # non-empty when allowed=False
    retry_hint: str  # non-empty when validation failed — include in re-prompt


class CommandLayer:
    """
    Wraps Executor calls with schema validation, permission gating, and action dedup.

    Usage:
        layer = CommandLayer(db_path=db_path, profile=profile, interactive=True)
        result = layer.check(tool_name, tool_input, manifest_schema)
        if not result.allowed:
            # handle block — re-prompt if validation_errors, skip if dedup/red-blocked
        else:
            output = executor.execute(tool_name, tool_input)
            if result.audit_required:
                layer.audit(tool_name, tool_input, output)
    """

    def __init__(
        self,
        db_path: str | None = None,
        profile: dict[str, Any] | None = None,
        interactive: bool = True,
        *,
        trust_gradient: TrustGradient | None = None,
    ) -> None:
        """
        db_path: SQLite database path for dedup + audit log.
        profile: merged profile.json dict; used for tier promotions + dedup window config.
        interactive: True = Red calls allowed (user present to confirm).
                     False = Red calls blocked (heartbeat, observation cycle).
        """
        self.db_path = db_path
        self.profile = profile or {}
        self.interactive = interactive
        self.trust_gradient = trust_gradient

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        manifest_schema: dict[str, Any] | None = None,
    ) -> CommandResult:
        """
        Run all gates. Returns a CommandResult. Never raises.

        Gate order:
        1. Schema validation — if errors, return allowed=False with retry_hint
        2. Permission tier — if RED and not interactive, return allowed=False
        3. Action dedup — if duplicate, return allowed=False with dedup_suppressed=True
        4. All passed → return allowed=True, set audit_required=(tier == YELLOW)
        """
        try:
            tier = resolve_tier(tool_name, self.profile)

            # 1. Schema validation
            validation_errors = validate_schema(tool_name, tool_input, manifest_schema)
            if validation_errors:
                retry_hint = f"Schema validation failed for {tool_name}: {', '.join(validation_errors)}. Please fix the parameters and try again."
                return CommandResult(
                    allowed=False,
                    tier=tier,
                    validation_errors=validation_errors,
                    dedup_suppressed=False,
                    audit_required=False,
                    block_reason=f"Validation failed: {', '.join(validation_errors)}",
                    retry_hint=retry_hint,
                )

            # 2. Permission tier
            if tier == PermissionTier.RED and not self.interactive:
                return CommandResult(
                    allowed=False,
                    tier=tier,
                    validation_errors=[],
                    dedup_suppressed=False,
                    audit_required=False,
                    block_reason=f"Tool '{tool_name}' requires user confirmation and cannot be run in non-interactive mode.",
                    retry_hint="",
                )

            # 3. Action dedup
            if self._check_dedup(tool_name, tool_input):
                return CommandResult(
                    allowed=False,
                    tier=tier,
                    validation_errors=[],
                    dedup_suppressed=True,
                    audit_required=False,
                    block_reason="Duplicate action suppressed",
                    retry_hint="",
                )

            # 4. All passed
            return CommandResult(
                allowed=True,
                tier=tier,
                validation_errors=[],
                dedup_suppressed=False,
                audit_required=(tier == PermissionTier.YELLOW),
                block_reason="",
                retry_hint="",
            )
        except Exception as e:
            logger.exception(f"CommandLayer.check internal error: {e}")
            # Safe default: allow but log error
            return CommandResult(
                allowed=True,
                tier=PermissionTier.GREEN,
                validation_errors=[],
                dedup_suppressed=False,
                audit_required=False,
                block_reason="",
                retry_hint="",
            )

    def audit(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """
        Write an audit log entry for a YELLOW tool call that was executed.
        Stored in the `access_log` table (migration 5, already exists).
        Never raises.
        """
        if not self.db_path:
            return

        try:
            thread_id = tool_input.get("thread_id", "")
            category = tool_input.get("category", "")
            refs = tool_input.get("refs", [])

            payload = {
                "thread_id": thread_id,
                "category": category,
                "refs": refs,
                "tool_input": tool_input,
                "result": result,
            }

            with open_db(Path(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO access_log (chat_id, authorized, user_name) VALUES (?, ?, ?)",
                    (f"tool:{tool_name}", 1, json.dumps(payload)),
                )
        except Exception as e:
            logger.warning(f"CommandLayer.audit failed: {e}")

    def _check_dedup(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """
        Returns True if this call should be suppressed as a duplicate.

        Dedup rules:
        - For nudge(): check `access_log` for a row with the same
          (tool_name="nudge", thread_id, category) within the last 4 hours.
          If found AND all refs in the new call are already covered by stored refs → suppress.
          "Covered" means: stored refs JSON contains all refs from tool_input["refs"].
          If new refs are present → allow (new information).
        - For all other tools: no dedup (always allow).

        thread_id = tool_input.get("thread_id", "")
        category = tool_input.get("category", "")
        refs = tool_input.get("refs", [])

        The dedup window is 4 hours by default; configurable via
        profile["command_layer"]["nudge_dedup_hours"] (integer).

        Never raises. Returns False (allow) on any DB error.
        """
        if tool_name != "nudge":
            return False

        if not self.db_path:
            return False

        try:
            thread_id = tool_input.get("thread_id", "")
            category = tool_input.get("category", "")
            new_refs = set(tool_input.get("refs", []))

            dedup_hours = self.profile.get("command_layer", {}).get("nudge_dedup_hours", 4)
            # Use UTC to match SQLite CURRENT_TIMESTAMP
            since_time = (datetime.now(timezone.utc) - timedelta(hours=dedup_hours)).strftime("%Y-%m-%d %H:%M:%S")

            with open_db(Path(self.db_path)) as conn:
                # Query access_log for recent nudge calls
                cursor = conn.execute(
                    "SELECT user_name FROM access_log WHERE chat_id = ? AND timestamp > ?",
                    ("tool:nudge", since_time),
                )
                rows = cursor.fetchall()

            for (user_name_json,) in rows:
                try:
                    payload = json.loads(user_name_json)
                    if payload.get("thread_id") == thread_id and payload.get("category") == category:
                        stored_refs = set(payload.get("refs", []))
                        # "Covered" means: stored refs JSON contains all refs from tool_input["refs"].
                        if new_refs.issubset(stored_refs):
                            return True
                except (json.JSONDecodeError, TypeError):
                    continue

            return False
        except Exception as e:
            logger.warning(f"CommandLayer._check_dedup failed: {e}")
            return False
