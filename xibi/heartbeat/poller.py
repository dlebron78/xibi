from __future__ import annotations

import importlib.util
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import xibi.db
from xibi.alerting.rules import RuleEngine
from xibi.channels.telegram import TelegramAdapter
from xibi.router import get_model

logger = logging.getLogger(__name__)


class HeartbeatPoller:
    def __init__(
        self,
        skills_dir: Path,
        db_path: Path,
        adapter: TelegramAdapter,
        rules: RuleEngine,
        allowed_chat_ids: list[int],
        interval_minutes: int = 15,
        quiet_start: int = 23,
        quiet_end: int = 8,
    ) -> None:
        self.skills_dir = skills_dir
        self.db_path = db_path
        self.adapter = adapter
        self.rules = rules
        self.allowed_chat_ids = allowed_chat_ids
        self.interval_minutes = interval_minutes
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self._last_reflection_date: Any = None  # Tracks date as string or None

    def _broadcast(self, text: str) -> None:
        for chat_id in self.allowed_chat_ids:
            try:
                self.adapter.send_message(chat_id, text)
                logger.info(f"Broadcast to {chat_id}: {text}")
            except Exception as e:
                logger.warning(f"Failed to broadcast to {chat_id}: {e}")

    def _is_quiet_hours(self) -> bool:
        hour = datetime.now().hour
        if self.quiet_start > self.quiet_end:
            return hour >= self.quiet_start or hour < self.quiet_end
        if self.quiet_start == self.quiet_end:
            return False
        return self.quiet_start <= hour < self.quiet_end

    def _run_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            # Find the tool in any skill subdirectory
            for skill_path in self.skills_dir.iterdir():
                if not skill_path.is_dir():
                    continue
                tool_path = skill_path / "tools" / f"{tool_name}.py"
                if tool_path.exists():
                    logger.info(f"Invoking tool {tool_name} from {tool_path}")
                    spec = importlib.util.spec_from_file_location(tool_name, tool_path)
                    if not spec or not spec.loader:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "run"):
                        res = module.run(params)  # type: ignore
                        return dict(res)
            return {"error": f"Tool {tool_name} not found in {self.skills_dir}"}
        except Exception as e:
            logger.error(f"Error running tool {tool_name}: {e}")
            return {"error": str(e)}

    def _check_email(self) -> list[dict[str, Any]]:
        result = self._run_tool("list_unread", {})
        if "error" in result:
            logger.warning(f"Email check failed: {result['error']}")
            return []
        emails = result.get("emails", [])
        return list(emails)

    def _classify_email(self, email: dict[str, Any]) -> str:
        sender = email.get("from", email.get("sender", "unknown"))
        if isinstance(sender, dict):
            sender = sender.get("name") or sender.get("addr", "unknown")
        subject = email.get("subject", "No Subject")

        prompt = (
            "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n"
            "URGENT = needs immediate attention.\n"
            "DIGEST = worth a summary later.\n"
            "NOISE = automated/newsletters/irrelevant.\n\n"
            f"From: {sender}\n"
            f"Subject: {subject}"
        )

        try:
            model = get_model(effort="fast")
            response = model.generate(prompt, max_tokens=5).strip().upper()
            first_word = response.split()[0] if response else ""
            if first_word in ["URGENT", "DIGEST", "NOISE"]:
                return first_word
            return "DIGEST"
        except Exception as e:
            logger.warning(f"LLM classification error: {e}")
            return "DEFER"

    def _should_escalate(self, verdict: str, topic: str, subject: str, priority_topics: list[str]) -> tuple[str, str]:
        if verdict == "DIGEST" and any(pt.lower() in topic.lower() for pt in priority_topics):
            return "URGENT", f"[Priority Topic] {subject}"
        return verdict, subject

    def tick(self) -> None:
        """Tick with atomic watermark locking to prevent duplicate processing."""
        if self._is_quiet_hours():
            logger.info("Quiet hours, skipping tick.")
            return

        with xibi.db.open_db(self.db_path) as conn, conn:  # BEGIN / COMMIT or ROLLBACK
            # Lock: use a sentinel row in heartbeat_state
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('tick_lock', ?)",
                (str(time.time()),),
            )

            self._tick_with_conn(conn)

    def _tick_with_conn(self, conn: sqlite3.Connection) -> None:
        # 1. Check tasks
        try:
            conn.row_factory = sqlite3.Row
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Handle cases where the table might not exist
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
            if cursor.fetchone():
                tasks = conn.execute(
                    "SELECT id, goal FROM tasks WHERE status IN ('pending', 'due') AND due_at <= ?", (now,)
                ).fetchall()
                for task in tasks:
                    self._broadcast(f"⏰ Task reminder: {task['goal']} (ID: {task['id']})")
        except Exception as e:
            logger.warning(f"Task check error: {e}")

        # 2. Fetch emails
        emails = self._check_email()
        email_rules = self.rules.load_rules("email_alert")
        seen_ids = self.rules.get_seen_ids_with_conn(conn)
        triage_rules = self.rules.load_triage_rules_with_conn(conn)

        for email in emails:
            email_id = str(email.get("id", ""))
            sender = email.get("from", email.get("sender", "unknown"))
            if isinstance(sender, dict):
                sender = sender.get("name") or sender.get("addr", "unknown")
            subject = email.get("subject", "No Subject")

            # Log signal
            self.rules.log_signal_with_conn(
                conn,
                source="email",
                topic_hint=None,
                entity_text=str(sender),
                entity_type="person",
                content_preview=f"{sender}: {subject}",
                ref_id=email_id,
                ref_source="email",
            )

            if email_id in seen_ids:
                continue

            # Auto-noise pre-filter
            verdict = ""
            sender_str = str(sender).lower()
            auto_noise = ["noreply@", "no-reply@", "notifications@", "newsletter@", "automated@", "mailer-daemon@"]
            if any(p in sender_str for p in auto_noise):
                verdict = "NOISE"

            # User triage rules
            if not verdict:
                for entity, status in triage_rules.items():
                    if entity.lower() in sender_str:
                        verdict = status.upper()
                        break

            # LLM classification
            if not verdict:
                verdict = self._classify_email(email)

            if verdict == "DEFER":
                continue

            # Escalation
            if verdict == "DIGEST":
                # Inferred topic (very simple)
                topic = subject
                # We could pull priority topics from DB if needed, but spec says check _should_escalate
                # For now use empty list or mock if not provided
                verdict, subject = self._should_escalate(verdict, topic, subject, [])

            # Log triage
            self.rules.log_triage_with_conn(conn, email_id, str(sender), subject, verdict)

            # Alert
            if verdict == "URGENT":
                alert_msg = self.rules.evaluate_email(email, email_rules)
                if alert_msg:
                    self._broadcast(alert_msg)

            # Mark seen
            self.rules.mark_seen_with_conn(conn, email_id)

    def digest_tick(self, force: bool = False) -> None:
        if self._is_quiet_hours() and not force:
            return

        items = self.rules.pop_digest_items()
        if not items:
            if force:
                self._broadcast("📥 Recap — no new emails triaged since last update. All quiet!")
            return

        msg_lines = ["📥 **Digest Recap**"]
        for item in items[:10]:
            msg_lines.append(f"• {item['sender']}: {item['subject']} ({item['verdict']})")

        self._broadcast("\n".join(msg_lines))

    def recap_tick(self) -> None:
        logger.info("Running recap tick")
        self.digest_tick(force=True)

    def _cleanup_telegram_cache(self) -> None:
        """Purge processed_messages rows older than 7 days. Runs once per day."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with xibi.db.open_db(self.db_path) as conn:
                # Check if already run today
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'ttl_cleanup_last_run'")
                row = cursor.fetchone()
                if row and row[0] == today:
                    return

                logger.info("Cleaning up Telegram message cache...")
                conn.execute("DELETE FROM processed_messages WHERE processed_at < datetime('now', '-7 days')")
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('ttl_cleanup_last_run', ?)",
                    (today,),
                )
        except Exception as e:
            logger.warning(f"Telegram cache cleanup error: {e}")

    def reflection_tick(self) -> None:
        if self._is_quiet_hours():
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reflection_date == today:
            return

        try:
            # Query triage patterns from 7 days
            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT sender, COUNT(*) as count FROM triage_log
                    WHERE timestamp > datetime('now', '-7 days')
                    GROUP BY sender ORDER BY count DESC LIMIT 5
                """)
                patterns = cursor.fetchall()

            pattern_str = ", ".join([f"{p[0]} ({p[1]})" for p in patterns])
            prompt = (
                f"Analyze these recent email triage patterns: {pattern_str}.\n"
                "Provide a brief, helpful reflection on what the user has been receiving."
            )

            model = get_model(effort="fast")
            reflection = model.generate(prompt)
            self._broadcast(f"💭 **Reflection**\n\n{reflection}")
            self.rules.log_background_event(reflection, "reflection")
            self._last_reflection_date = today
        except Exception as e:
            logger.warning(f"Reflection tick error: {e}")

    def run(self) -> None:
        tick_count = 0
        interval_secs = self.interval_minutes * 60
        logger.info(f"Starting heartbeat loop (interval: {self.interval_minutes}m)")

        while True:
            try:
                self.tick()
                tick_count += 1

                ticks_per_hour = max(1, 60 // self.interval_minutes)
                now = datetime.now()

                # Check windows
                if (now.hour == 9 or now.hour == 18) and now.minute < 15:
                    self.recap_tick()
                    tick_count = 0
                elif tick_count >= ticks_per_hour:
                    self.digest_tick()
                    tick_count = 0

                if now.hour == 7 and now.minute < 15:
                    self.reflection_tick()
                    self._cleanup_telegram_cache()

            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}")

            time.sleep(interval_secs)
