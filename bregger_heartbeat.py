#!/usr/bin/env python3
"""
bregger_heartbeat.py — Proactive polling loop for Bregger.

Runs independently alongside bregger_telegram.py.
Every POLL_INTERVAL_MINUTES it:
  1. Checks email via the `email/list_unread` skill tool
  2. Applies user-defined alert rules from the `rules` SQLite table
  3. Pushes matching alerts to Telegram

Run:
  python3 bregger_heartbeat.py ~/.bregger/config.json
"""

import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Add project root to sys.path to allow importing from the root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))
import contextlib

from bregger_utils import (
    ensure_signals_schema,
    inference_lock,
)
from bregger_utils import (
    get_active_threads as _get_active_threads,
)
from bregger_utils import (
    get_pinned_topics as _get_pinned_topics,
)
from bregger_utils import (
    normalize_topic as _normalize_topic,
)
from xibi.heartbeat.sender_trust import (
    assess_sender_trust,
    _extract_sender_addr,
    _extract_sender_name,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_INTERVAL_MINUTES = int(os.environ.get("HEARTBEAT_INTERVAL_MIN", "15"))
QUIET_HOUR_START = int(os.environ.get("HEARTBEAT_QUIET_START", "23"))
QUIET_HOUR_END = int(os.environ.get("HEARTBEAT_QUIET_END", "8"))


# ---------------------------------------------------------------------------
# Telegram Notifier (zero-dependency, reuses same pattern as bregger_telegram.py)
# ---------------------------------------------------------------------------


class TelegramNotifier:
    def __init__(self, token: str, allowed_chats: list[str]):
        self.token = token
        self.allowed_chats = allowed_chats
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send(self, text: str):
        """Broadcast to all allowed chats."""
        for chat_id in self.allowed_chats:
            try:
                payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
                req = urllib.request.Request(
                    f"{self.base_url}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception as e:
                print(f"⚠️ Heartbeat: failed to notify chat {chat_id}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Rule Engine
# ---------------------------------------------------------------------------


class RuleEngine:
    """
    Evaluates alert rules stored in the `rules` SQLite table.

    Rule schema (stored as JSON in `rules.condition` column):
      {
        "field":    "from" | "subject" | "body",
        "contains": "<text to match (case-insensitive)>"
      }

    Example rows:
      ("email_alert", '{"field": "from", "contains": "apple"}',  "📬 Email from Apple!")
      ("email_alert", '{"field": "subject", "contains": "urgent"}', "🚨 Urgent email!")
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._rule_cache = []
        self._watermark_cache = "1970-01-01 00:00:00"
        self._ensure_table()
        self._prewarm()

    def _ensure_table(self):
        """Create the rules table if it doesn't exist yet."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS rules (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        type    TEXT NOT NULL,          -- e.g. 'email_alert'
                        condition TEXT NOT NULL,        -- JSON condition
                        message TEXT NOT NULL,          -- alert message template
                        enabled INTEGER DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Seed a default rule so the system has something to work with
                conn.execute("""
                    INSERT OR IGNORE INTO rules (id, type, condition, message)
                    VALUES (1, 'email_alert',
                            '{"field": "from", "contains": "@"}',
                            '📬 New email from {from}: {subject}')
                """)
        except Exception as e:
            print(f"⚠️ RuleEngine init error: {e}", flush=True)

    def _prewarm(self):
        """Pre-warm rules and state into RAM."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 1. Warm rules
                cursor = conn.execute("SELECT type, condition, message FROM rules WHERE enabled=1")
                for r_type, cond_json, msg in cursor.fetchall():
                    try:
                        self._rule_cache.append({"type": r_type, "condition": json.loads(cond_json), "message": msg})
                    except Exception as e:
                        print(f"⚠️ [rule_cache] Failed to parse rule JSON: {e}", flush=True)

                # 2. Warm watermark
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_digest_at'")
                row = cursor.fetchone()
                if row:
                    self._watermark_cache = row[0]
        except Exception as e:
            print(f"⚠️ RuleEngine prewarm error: {e}", flush=True)

    def _ensure_triage_tables(self):
        """Create triage and digest tables if they don't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS triage_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email_id TEXT,
                        sender TEXT,
                        subject TEXT,
                        verdict TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Track if any digest was sent today to gate AM/PM recaps
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS heartbeat_state (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
        except Exception as e:
            print(f"⚠️ Triage table init error: {e}", flush=True)

    def log_triage(self, email_id: str, sender: str, subject: str, verdict: str):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES (?, ?, ?, ?)",
                    (email_id, sender, subject, verdict),
                )
        except Exception as e:
            print(f"⚠️ [log_triage] Failed to log triage result for email {email_id}: {e}", flush=True)

    def get_digest_items(self) -> list[dict]:
        """Fetch all items from triage_log since the last digest_at watermark."""
        try:
            # Use RAM watermark (Reference Data)
            watermark = self._watermark_cache

            with sqlite3.connect(self.db_path) as conn:
                # Query triage_log (excluding URGENT as they were alerted immediately)
                cursor = conn.execute(
                    """
                    SELECT sender, subject, verdict, timestamp FROM triage_log
                    WHERE timestamp > ? AND verdict != 'URGENT'
                    ORDER BY timestamp ASC
                """,
                    (watermark,),
                )
                rows = cursor.fetchall()
                return [{"sender": r[0], "subject": r[1], "verdict": r[2], "timestamp": r[3]} for r in rows]
        except Exception as e:
            print(f"⚠️ Error fetching digest items: {e}", flush=True)
            return []

    def update_watermark(self):
        """Update the last_digest_at watermark in DB and RAM."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_digest_at', CURRENT_TIMESTAMP) RETURNING value"
                )
                row = cursor.fetchone()
                if row:
                    self._watermark_cache = row[0]
        except Exception as e:
            print(f"⚠️ Error updating watermark: {e}", flush=True)

    def was_digest_sent_since(self, since_dt: datetime) -> bool:
        """Check against cached RAM watermark."""
        try:
            last_sent = datetime.fromisoformat(self._watermark_cache)
            return last_sent > since_dt
        except Exception as e:
            print(f"⚠️ [was_digest_sent_since] Failed to parse watermark '{self._watermark_cache}': {e}", flush=True)
            return False

    def load_rules(self, rule_type: str) -> list[dict]:
        """Return cached rules for the specified type."""
        return [r for r in self._rule_cache if r["type"] == rule_type]

    def log_background_event(self, content: str, topic: str):
        """Persist a proactive event (like a digest) to the Ledger for bot awareness."""
        try:
            import uuid

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO ledger (id, category, content, entity, status)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (str(uuid.uuid4()), "background_event", content, topic, "sent"),
                )
        except Exception as e:
            print(f"⚠️ Error logging background event: {e}", flush=True)

    def load_triage_rules(self) -> dict:
        """Load user-declared triage rules from the Ledger.

        Returns a dict of {lowercase_entity: verdict} for all
        ledger rows where category='triage_rule'.
        Example: {"asmallworld": "NOISE", "apple": "URGENT"}
        """
        rules = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT COALESCE(entity, content), status FROM ledger WHERE category='triage_rule'"
                )
                for entity, status in cursor.fetchall():
                    if entity and status:
                        rules[entity.lower()] = status.upper()
            if rules:
                print(f"📋 Triage rules loaded: {rules}", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to load triage rules: {e}", flush=True)
        return rules

    def log_signal(
        self,
        source,
        topic_hint,
        entity_text,
        entity_type,
        content_preview,
        ref_id,
        ref_source,
        summary=None,
        summary_model=None,
        summary_ms=None,
        sender_trust=None,
        sender_contact_id=None,
    ):
        """Insert a signal into the signals table."""
        try:
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            # Ensure schema is current (delegates to bregger_utils — single source of truth)
            ensure_signals_schema(self.db_path)
            with sqlite3.connect(self.db_path) as conn:
                # Dedup check: skip if same source+ref_id was logged today
                if ref_id:
                    cursor = conn.execute(
                        "SELECT 1 FROM signals WHERE source = ? AND ref_id = ? AND date(timestamp) = date('now')",
                        (source, str(ref_id)),
                    )
                    if cursor.fetchone():
                        return

                conn.execute(
                    """
                    INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id, env)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'production')
                """,
                    (source, topic_hint, entity_text, entity_type, preview, str(ref_id), ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id),
                )
        except Exception as e:
            print(f"⚠️ Heartbeat: Failed to log signal: {e}", flush=True)

    def extract_topic_from_subject(self, subject: str) -> tuple[str | None, str | None, str | None]:
        """Simple Python keyword extractor for email subjects."""
        if not subject or subject == "No Subject":
            return None, None, None

        # Normalize: strip common prefixes, lowercase, remove special chars
        clean = re.sub(r"^(Re:|Fwd:|\[.*?\]|\(.*?\))\s*", "", subject, flags=re.IGNORECASE).strip().lower()
        clean = re.sub(r"[^a-z0-9\s_-]", "", clean)

        # Topic candidates: first 2-3 meaningful words
        words = [w for w in clean.split() if len(w) > 2 and w not in {"the", "and", "for", "your", "from", "with"}]
        topic = "_".join(words[:2]) if words else None

        # Entity candidates: look for project/org patterns in ORIGINAL subject
        # Brackets [Afya-fit] often contain the most reliable metadata.
        entity_text = None
        entity_type = None

        m = re.search(r"\[(.*?)\]", subject)  # Check original for brackets
        if m:
            entity_text = m.group(1).split("/")[0]  # e.g. [Afya-fit/...] -> Afya-fit
            entity_type = "project" if "-" in entity_text or "afya" in entity_text.lower() else "org"

        return topic, entity_text, entity_type

    def evaluate_email(self, email: dict, rules: list[dict], sender_trust=None) -> str | None:
        """
        Returns the alert message if any rule matches, else None.
        email is a dict with keys: from, subject, date, id
        """
        for rule in rules:
            cond = rule["condition"]
            field = cond.get("field", "subject")
            contains = cond.get("contains", "").lower()
            # Handle 'from' being a dict or string
            raw_val = email.get(field, "")
            if isinstance(raw_val, dict):
                value = (raw_val.get("name") or raw_val.get("addr", "")).lower()
            else:
                value = str(raw_val).lower()
            if contains and contains in value:
                # Simple template substitution
                msg = rule["message"]
                for k, v in email.items():
                    if isinstance(v, dict):
                        v = v.get("name") or v.get("addr", str(v))
                    msg = msg.replace(f"{{{k}}}", str(v))

                if sender_trust:
                    trust_line = sender_trust.format_nudge_line()
                    if trust_line:
                        msg = f"{trust_line}\n{msg}"

                return msg
        return None


# ---------------------------------------------------------------------------
# Email Checker
# ---------------------------------------------------------------------------


def _run_tool(skill_dir: Path, tool_name: str, params: dict) -> dict:
    """Dynamically load and run a skill tool Python file."""
    tool_file = skill_dir / "tools" / f"{tool_name}.py"
    if not tool_file.exists():
        return {"status": "error", "message": f"Tool file not found: {tool_file}"}
    try:
        spec = importlib.util.spec_from_file_location(tool_name, tool_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(params)
    except Exception as e:
        return {"status": "error", "message": str(e)}


def check_email(skills_dir: Path) -> list[dict]:
    """Run email/list_unread and return a list of envelope dicts."""
    email_skill = skills_dir / "email"
    result = _run_tool(email_skill, "list_unread", {"count": 10})
    if result.get("status") != "success":
        print(f"⚠️ Heartbeat email check failed: {result.get('message')}", flush=True)
        return []
    data = result.get("data", {})
    # list_unread returns {"emails": [...]} or {"data": {"emails": [...]}}
    emails = data if isinstance(data, list) else data.get("emails", [])
    return emails


# ---------------------------------------------------------------------------
# Ollama Intelligence
# ---------------------------------------------------------------------------


def _batch_extract_topics(emails: list[dict], model: str = "llama3.2:latest") -> dict[str, dict]:
    """
    Batch LLM call to extract topic + entity from email subjects.
    Returns a dict keyed by email ID: {id: {topic, entity_text, entity_type}}.
    Falls back to empty dict on failure (caller should use regex fallback).
    """
    if not emails:
        return {}

    # Build a numbered list of subjects for the prompt
    lines = []
    id_map = {}  # index -> email_id
    for i, email in enumerate(emails):
        email_id = str(email.get("id", ""))
        sender = _extract_sender(email)
        subject = email.get("subject", "No Subject")
        lines.append(f"{i + 1}. From: {sender} | Subject: {subject}")
        id_map[i + 1] = email_id

    prompt = (
        "Extract the main topic and any named entity from each email below.\n"
        "For each email, output one JSON object per line with keys: num, topic, entity_text, entity_type.\n"
        "- topic: 1-3 word summary of the core subject (e.g. 'board deck', 'job application', 'flight booking')\n"
        "- entity_text: a specific person, company, or project name if present, otherwise null\n"
        "- entity_type: one of 'person', 'company', 'project', or null\n\n"
        "Output ONLY a JSON array. No explanation.\n\n"
        "Emails:\n" + "\n".join(lines) + "\n\n"
        "JSON:"
    )

    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 300, "temperature": 0}}
    ).encode()

    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with inference_lock, urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            raw = resp.get("response", "").strip()

        # Parse JSON — handle markdown fences
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?", "", raw)
            raw = re.sub(r"```$", "", raw.strip())

        extracted = json.loads(raw.strip())
        if not isinstance(extracted, list):
            extracted = [extracted]

        result = {}
        for item in extracted:
            try:
                num = int(item.get("num", 0))
            except (ValueError, TypeError):
                continue
            if num and num in id_map:
                topic = item.get("topic")
                if topic:
                    raw_topic = "_".join(topic.lower().split()[:3])
                    topic = _normalize_topic(raw_topic) or raw_topic
                result[id_map[num]] = {
                    "topic": topic,
                    "entity_text": item.get("entity_text"),
                    "entity_type": item.get("entity_type"),
                }

        print(f"🧠 [batch_extract] Extracted topics for {len(result)}/{len(emails)} emails", flush=True)
        return result

    except Exception as e:
        print(f"⚠️ [batch_extract] LLM topic extraction failed: {e} — falling back to regex", flush=True)
        return {}


def is_ollama_busy() -> bool:
    """Check if Ollama is currently running inference."""
    try:
        # Use a short timeout to avoid blocking the heartbeat tick
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=2) as r:
            data = json.loads(r.read())
            return len(data.get("models", [])) > 0
    except Exception as e:
        print(f"⚠️ [is_ollama_busy] Could not reach Ollama: {e} — assuming busy", flush=True)
        return True


def _extract_sender(email: dict) -> str:
    """Safely extract sender name and address from himalaya envelope.
    The 'from' field can be a dict {name, addr} or a plain string."""
    raw = email.get("from", "Unknown")
    if isinstance(raw, dict):
        name = raw.get("name")
        addr = raw.get("addr")
        if name and addr:
            return f"{name} <{addr}>"
        return name or addr or "Unknown"
    return str(raw) if raw else "Unknown"


def _extract_sender_addr(email: dict) -> str:
    """Extract just the email address from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("addr") or "").strip().lower()
    # Fall back to parsing "Name <addr>" format
    raw = str(sender)
    if "<" in raw and ">" in raw:
        return raw.split("<")[1].split(">")[0].strip().lower()
    return raw.strip().lower()


def _extract_sender_name(email: dict) -> str:
    """Extract just the display name from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("name") or "").strip()
    raw = str(sender)
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"')
    return ""


def classify_email(email: dict, model: str = "llama3.2:latest") -> str:
    """Ask Ollama to classify email as URGENT, DIGEST, or NOISE."""
    # Note: We removed the is_ollama_busy guard here to ensure we don't
    # fall back to DIGEST/dumps. Ollama will queue these internally.

    sender = _extract_sender(email)
    prompt = (
        f"From: {sender}\n"
        f"Subject: {email.get('subject', 'No Subject')}\n\n"
        "Classify this email for a personal assistant triage. Answer with exactly one word:\n"
        "URGENT - High priority. Human-to-human messages, travel, security, fraud, or direct replies.\n"
        "DIGEST - Medium priority. Newsletters you actively read, job alerts, or meaningful updates you care about.\n"
        "NOISE - Low priority. Automated marketing, coupons, social media notifications, bulk receipts, or junk.\n\n"
        "Strict Rule: If it looks like a mass-email or automated notification, it is NOISE unless it's clearly an update you requested.\n\n"
        "Verdict:"
    )

    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 10, "temperature": 0}}
    ).encode()

    try:
        # 15s timeout for classification
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with inference_lock, urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            verdict = resp.get("response", "").strip().upper()
            if "URGENT" in verdict:
                return "URGENT"
            if "NOISE" in verdict:
                return "NOISE"
            return "DIGEST"
    except Exception as e:
        print(f"⚠️ Classification error: {e}", flush=True)
        return "DIGEST"  # Default to digest on error


def _should_escalate(
    verdict: str,
    topic: str,
    subject: str,
    priority_topics: list,
) -> tuple:
    """Pure function: decide whether a DIGEST verdict should escalate to URGENT.

    Phase 2.2 — Cross-Channel Relevance. Extracted from tick() so it can be
    tested directly without wiring up a full heartbeat tick.

    Args:
        verdict:         Current classification ("URGENT", "DIGEST", "NOISE").
        topic:           Normalized topic string extracted from the email.
        subject:         Original email subject line.
        priority_topics: Combined list of active threads + pinned topics
                         (pre-loaded once per tick, not fetched here).

    Returns:
        (new_verdict, new_subject) — verdict is unchanged unless escalated.
    """
    if verdict != "DIGEST" or not topic:
        return verdict, subject

    norm_topic = _normalize_topic(topic)
    matching = next((t for t in priority_topics if t.get("topic") == norm_topic), None)

    if matching:
        prefix = "📌 [Pinned Topic" if matching.get("pinned") else "🔥 [Active Thread"
        new_subject = f"{prefix}: {topic}] {subject}"
        print(f"🚀 Escalating DIGEST→URGENT for active thread: {topic}", flush=True)
        return "URGENT", new_subject

    return verdict, subject


def _synthesize_digest(items: list[dict], model: str = "llama3.2:latest") -> str:
    """Ask Ollama to turn the digest into a conversational story."""
    if not items:
        return "No new updates."

    # Prepare the list for the LLM
    digest_lines = []
    noise_senders = []
    for item in items:
        if item["verdict"] == "DIGEST":
            digest_lines.append(f"- {item['sender']}: {item['subject']}")
        else:
            noise_senders.append(item["sender"])

    # Deduplicate noise senders
    noise_senders = sorted(list(set(noise_senders)))

    prompt = (
        "You are Bregger, Dan's personal assistant. Group these emails from the last hour into a conversational summary.\n\n"
        "NOTABLE EMAILS (Include details):\n" + "\n".join(digest_lines) + "\n\n"
        "NOISE SENDERS (Just mention names if any):\n" + ", ".join(noise_senders) + "\n\n"
        "Write 2-4 sentences summarizing. Group similar things. Lead with notable ones. Be friendly but concise."
    )

    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 150, "temperature": 0.7}}
    ).encode()

    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with inference_lock, urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            summary = resp.get("response", "").strip()
            if summary:
                return f"📥 **Inbox Recap**\n\n{summary}"
            return ""
    except Exception as e:
        print(f"⚠️ [synthesize_digest] LLM synthesis failed: {e}", flush=True)
        return ""  # Fallback


# ---------------------------------------------------------------------------
# Quiet Hours
# ---------------------------------------------------------------------------


def is_quiet_hours() -> bool:
    hour = datetime.now().hour
    if QUIET_HOUR_START > QUIET_HOUR_END:
        # Wraps midnight — e.g. 23:00–08:00
        return hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END
    return QUIET_HOUR_START <= hour < QUIET_HOUR_END


# ---------------------------------------------------------------------------
# Seen-ID tracking (avoid duplicate alerts)
# ---------------------------------------------------------------------------


def _seen_ids(db_path: Path) -> set:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS heartbeat_seen (
                    email_id TEXT PRIMARY KEY,
                    seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor = conn.execute("SELECT email_id FROM heartbeat_seen")
            return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        print(f"⚠️ [seen_ids] Failed to load seen email IDs: {e}", flush=True)
        return set()


def _mark_seen(db_path: Path, email_id: str):
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO heartbeat_seen (email_id) VALUES (?)", (email_id,))
    except Exception as e:
        print(f"⚠️ [mark_seen] Failed to mark email {email_id} as seen: {e}", flush=True)


# ---------------------------------------------------------------------------
# Task Layer V1 (check_tasks)
# ---------------------------------------------------------------------------


def check_tasks(notifier: TelegramNotifier, db_path: Path):
    """Fire scheduled tasks, nudge paused ones, and expire stale tasks."""

    def _log_task_trace(trace_id, intent, plan_data):
        """Log a task event directly to the traces table."""
        try:
            with sqlite3.connect(db_path) as c:
                c.execute(
                    "INSERT INTO traces (id, intent, plan, status) VALUES (?, ?, ?, ?)",
                    (trace_id, intent, json.dumps(plan_data), "completed"),
                )
        except Exception:
            pass  # Best-effort — don't crash the heartbeat

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Check if the active slot is occupied
            slot_occupied = (
                conn.execute("SELECT id FROM tasks WHERE status='awaiting_reply' LIMIT 1").fetchone() is not None
            )

            # 1. Fire Scheduled tasks
            scheduled = conn.execute(
                "SELECT id, goal, urgency FROM tasks WHERE status = 'scheduled' AND due <= datetime('now')"
            ).fetchall()
            for row in scheduled:
                task_id = row["id"]
                goal = row["goal"]
                urgency = row["urgency"] or "normal"

                if slot_occupied and urgency != "critical":
                    # Slot is busy — skip this promotion, heartbeat will retry next tick
                    continue

                # Promote to awaiting_reply (taking the slot)
                if slot_occupied and urgency == "critical":
                    # Critical preemption: demote current slot holder
                    conn.execute(
                        "UPDATE tasks SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply'"
                    )
                    slot_occupied = False  # Slot is now free

                msg = f"\u23f0 Scheduled Task:\n{goal}\n[task:{task_id}]"
                notifier.send(msg)
                conn.execute(
                    "UPDATE tasks SET status='awaiting_reply', nudge_count=1, last_nudged_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task_id,),
                )
                slot_occupied = True  # This task now holds the slot
                print(f"\u23f0 Fired scheduled task: {task_id}", flush=True)
                _log_task_trace(f"task_fire_{task_id}", "task_fired", {"task_id": task_id, "goal": goal[:100]})

            # 2. Nudge Paused tasks (only if slot is free)
            intervals = {"critical": 4, "normal": 24, "low": 168}  # hours
            max_nudges = {"critical": 6, "normal": 3, "low": 1}

            paused = conn.execute(
                "SELECT id, goal, urgency, nudge_count, last_nudged_at FROM tasks WHERE status = 'paused'"
            ).fetchall()
            for row in paused:
                task_id = row["id"]
                urgency = row["urgency"] or "normal"
                nudge_count = row["nudge_count"] or 0
                max_n = max_nudges.get(urgency, 3)

                if nudge_count >= max_n:
                    continue

                # Slot check: suppress nudges if occupied (unless critical preemption)
                if slot_occupied and urgency != "critical":
                    continue

                hours_wait = intervals.get(urgency, 24)

                should_nudge = False
                if not row["last_nudged_at"]:
                    should_nudge = True
                else:
                    # SQLite CURRENT_TIMESTAMP is UTC
                    last_nudge = datetime.fromisoformat(row["last_nudged_at"].replace("Z", ""))
                    hours_since = (datetime.utcnow() - last_nudge).total_seconds() / 3600
                    if hours_since >= hours_wait:
                        should_nudge = True

                if should_nudge:
                    # Critical preemption
                    if slot_occupied and urgency == "critical":
                        conn.execute(
                            "UPDATE tasks SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply'"
                        )
                        slot_occupied = False

                    msg = f"\u23f3 Pending Task:\n{row['goal']}\n(Reply to resume, or ignore)\n[task:{task_id}]"
                    notifier.send(msg)
                    conn.execute(
                        "UPDATE tasks SET status='awaiting_reply', nudge_count=nudge_count+1, last_nudged_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (task_id,),
                    )
                    slot_occupied = True
                    print(f"\u23f3 Nudged task: {task_id} (promoted to awaiting_reply)", flush=True)

            # 3. Expire stale tasks
            expired = conn.execute(
                "SELECT id, goal FROM tasks WHERE status='paused' AND updated_at < datetime('now', '-7 days')"
            ).fetchall()
            for row in expired:
                _log_task_trace(
                    f"task_expire_{row['id']}", "task_expired", {"task_id": row["id"], "goal": row["goal"][:100]}
                )
            conn.execute(
                "UPDATE tasks SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE status='paused' AND updated_at < datetime('now', '-7 days')"
            )
            # Expire stale awaiting_reply (slot held > 24h)
            stale_awaiting = conn.execute(
                "SELECT id, goal FROM tasks WHERE status='awaiting_reply' AND updated_at < datetime('now', '-1 day')"
            ).fetchall()
            for row in stale_awaiting:
                _log_task_trace(
                    f"task_expire_{row['id']}", "task_expired", {"task_id": row["id"], "goal": row["goal"][:100]}
                )
            conn.execute(
                "UPDATE tasks SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply' AND updated_at < datetime('now', '-1 day')"
            )

    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"⚠️ Error in check_tasks: {e}", flush=True)


# ---------------------------------------------------------------------------
# Reflection Loop (Intelligence Layer)
# ---------------------------------------------------------------------------

_DEADLINE_WORDS = {
    "deadline",
    "renewal",
    "expiry",
    "expires",
    "due",
    "overdue",
    "payment",
    "invoice",
    "certificate",
    "registration",
}


def should_propose(entity: str, topic: str, freq: int) -> dict | None:
    """Deterministic rule engine for V1 proposals."""
    if freq >= 5:
        return {"goal": f"Follow up with {entity} about {topic}", "urgency": "normal"}
    if freq >= 3 and any(w in topic.lower() for w in _DEADLINE_WORDS):
        return {"goal": f"Check status of {topic} for {entity}", "urgency": "normal"}
    return None


def _synthesize_reflection(patterns: list[dict], beliefs: list[dict], model: str = "llama3.2:latest") -> dict | None:
    """
    LLM-based reflection synthesis (Phase 1.75 Fix 3).
    Given signal frequency patterns and user beliefs, ask the model what's
    worth surfacing. Returns {goal, urgency, reasoning} or None.
    """
    if not patterns:
        return None

    # Build pattern summary
    pattern_lines = []
    for p in patterns[:8]:  # Cap at 8 to stay within token budget
        pattern_lines.append(f"- {p['entity_text']} + {p['topic_hint']}: {p['freq']} mentions this week")

    # Build belief context (compact)
    belief_lines = []
    for b in beliefs[:10]:
        belief_lines.append(f"- {b.get('key', '?')}: {b.get('value', '?')}")

    prompt = (
        "You are a personal assistant reviewing a week of signals.\n"
        "Below are topics that appeared multiple times across email and chat.\n"
        "Decide if any are worth bringing to the user's attention.\n\n"
        "SIGNAL PATTERNS:\n" + "\n".join(pattern_lines) + "\n\n"
    )
    if belief_lines:
        prompt += "USER CONTEXT:\n" + "\n".join(belief_lines) + "\n\n"

    prompt += (
        "Propose when you can cite a specific deadline, an unanswered thread, or a time-sensitive pattern.\n"
        "A good proposal looks like: 'Follow up with Sarah about the board deck — she sent feedback 3 days ago with no reply.'\n"
        "If nothing has a clear, actionable reason to surface, respond with exactly: NONE\n\n"
        "If something IS worth surfacing, respond with JSON:\n"
        '{"goal": "Follow up with Sarah about the board deck — feedback pending 3 days", "urgency": "normal"}\n\n'
        "JSON or NONE:"
    )

    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 200, "temperature": 0}}
    ).encode()

    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with inference_lock, urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            raw = resp.get("response", "").strip()

        if "NONE" in raw.upper() and len(raw) < 20:
            print("🧠 [reflect] LLM says nothing worth surfacing", flush=True)
            return None

        # Parse JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?", "", raw)
            raw = re.sub(r"```$", "", raw.strip())

        result = json.loads(raw.strip())
        if result.get("goal"):
            print(f"🧠 [reflect] LLM proposal: {result['goal']}", flush=True)
            return result
        return None

    except Exception as e:
        print(f"⚠️ [reflect] LLM synthesis failed: {e} — falling back to frequency rules", flush=True)
        return None


def reflect(notifier: TelegramNotifier, db_path: Path, model: str = "llama3.2:latest"):
    """Reflection loop: detect signal patterns → LLM synthesis → propose tasks (gated by user)."""
    import json
    import time
    import uuid

    t0 = time.time()
    trace_id = f"reflect_{uuid.uuid4().hex[:8]}"
    patterns_scanned = 0
    proposals_sent = 0
    synthesis_used = "none"

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Guard 1: skip if single active slot is occupied
            if conn.execute("SELECT 1 FROM tasks WHERE status='awaiting_reply'").fetchone():
                return

            # Step 1: Aggregate active signals (filtering NULLs to avoid grouping noise)
            patterns = conn.execute("""
                SELECT entity_text, topic_hint, COUNT(*) as freq
                FROM signals
                WHERE proposal_status = 'active'
                  AND entity_text IS NOT NULL
                  AND topic_hint IS NOT NULL
                  AND timestamp > datetime('now', '-7 days')
                GROUP BY entity_text, topic_hint
                HAVING COUNT(*) >= 3
                ORDER BY freq DESC
            """).fetchall()

            patterns_scanned = len(patterns)
            if not patterns:
                return

            # Step 2: Load beliefs for context
            beliefs = []
            with contextlib.suppress(Exception):
                beliefs = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT key, value FROM beliefs WHERE valid_until IS NULL LIMIT 10"
                    ).fetchall()
                ]

            # Step 3: LLM synthesis (Phase 1.75 Fix 3)
            # Feed patterns + beliefs to the model. Falls back to frequency rules on failure.
            pattern_dicts = [dict(row) for row in patterns]
            llm_proposal = _synthesize_reflection(pattern_dicts, beliefs, model=model)

            if llm_proposal:
                synthesis_used = "llm"
                goal = llm_proposal["goal"]
                urgency = llm_proposal.get("urgency", "normal")
                # Use first pattern's entity/topic for signal marking
                primary_entity = patterns[0]["entity_text"]
                primary_topic = patterns[0]["topic_hint"]
            else:
                # Fallback: deterministic frequency rules (original V1 behavior)
                goal = None
                primary_entity = None
                primary_topic = None
                for row in pattern_dicts:
                    entity = row["entity_text"]
                    topic = row["topic_hint"]
                    freq = row["freq"]

                    # Dedup against existing active tasks
                    existing = conn.execute(
                        "SELECT 1 FROM tasks WHERE goal LIKE '%' || ? || '%' AND goal LIKE '%' || ? || '%' "
                        "AND status NOT IN ('done','expired','cancelled')",
                        (entity, topic),
                    ).fetchone()
                    if existing:
                        continue

                    proposal = should_propose(entity, topic, freq)
                    if proposal:
                        synthesis_used = "frequency"
                        goal = proposal["goal"]
                        urgency = proposal["urgency"]
                        primary_entity = entity
                        primary_topic = topic
                        break

            if not goal:
                return

            # Step 4: Dedup against existing tasks (LLM path only —
            # the frequency fallback loop already deduplicates inline)
            if synthesis_used == "llm" and primary_entity and primary_topic:
                existing = conn.execute(
                    "SELECT 1 FROM tasks WHERE goal LIKE '%' || ? || '%' AND goal LIKE '%' || ? || '%' "
                    "AND status NOT IN ('done','expired','cancelled')",
                    (primary_entity, primary_topic),
                ).fetchone()
                if existing:
                    return

            # Step 5: Create task + notify
            task_id = str(uuid.uuid4())

            # Enforce single slot
            conn.execute("UPDATE tasks SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply'")

            conn.execute(
                """
                INSERT INTO tasks (id, goal, status, exit_type, urgency, context_compressed, scratchpad_json, origin, trace_id)
                VALUES (?, ?, 'awaiting_reply', 'ask_user', ?, '', '[]', 'reflection', ?)
            """,
                (task_id, goal, urgency, trace_id),
            )

            # Update signals
            if primary_entity and primary_topic:
                conn.execute(
                    "UPDATE signals SET proposal_status='proposed' "
                    "WHERE entity_text = ? AND topic_hint = ? AND proposal_status='active'",
                    (primary_entity, primary_topic),
                )

            # Notify via Telegram
            msg = f"💡 {goal}\n[task:{task_id}]"
            notifier.send(msg)
            proposals_sent = 1
            print(f"💡 Reflection proposed ({synthesis_used}): {goal}", flush=True)

    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"⚠️ Error in reflect: {e}", flush=True)

    # Step 6: Trace (best effort)
    duration_ms = int((time.time() - t0) * 1000)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO traces (id, intent, plan, status) VALUES (?, ?, ?, ?)",
                (
                    trace_id,
                    "reflection",
                    json.dumps(
                        {
                            "patterns_scanned": patterns_scanned,
                            "proposals_sent": proposals_sent,
                            "synthesis": synthesis_used,
                            "duration_ms": duration_ms,
                        }
                    ),
                    "completed",
                ),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main heartbeat tick
# ---------------------------------------------------------------------------


def tick(
    skills_dir: Path, db_path: Path, notifier: TelegramNotifier, rules: RuleEngine, model: str = "llama3.2:latest"
):
    if is_quiet_hours():
        print("🌙 Quiet hours — skipping heartbeat tick", flush=True)
        return

    print(f"💓 Heartbeat tick at {datetime.now().strftime('%H:%M')}", flush=True)

    # --- Task check ---------------------------------------------------------
    check_tasks(notifier, db_path)

    # --- Email check --------------------------------------------------------
    emails = check_email(skills_dir)
    email_rules = rules.load_rules("email_alert")
    seen = _seen_ids(db_path)
    triage_rules = rules.load_triage_rules()  # Load once per tick, not per email

    # ── Batch Topic Extraction (Phase 1.75 Fix 1) ────────────────
    # One LLM call for all emails in the tick, instead of regex per email.
    # Falls back to regex if LLM fails (Rule 16 — graceful degradation).
    batch_topics = _batch_extract_topics(emails, model=model)

    # ── Batch Email Body Summarization ──────────────────────────
    # Fetch bodies and generate LLM summaries for all emails in this tick.
    # Runs BEFORE the per-email loop so summaries are available when logging signals.
    from xibi.heartbeat.email_body import (
        find_himalaya, fetch_raw_email, parse_email_body,
        compact_body, summarize_email_body
    )

    try:
        himalaya_bin = find_himalaya()
    except FileNotFoundError as e:
        print(f"⚠️ {e} Skipping body summarization for this tick.", flush=True)
        himalaya_bin = None

    body_summaries = {}  # email_id -> {status, summary, model, duration_ms}
    _summary_start = time.time()

    if himalaya_bin:
        for email in emails:
            eid = str(email.get("id", ""))
            if not eid:
                continue

            # 1. Fetch raw RFC 5322
            raw, err = fetch_raw_email(himalaya_bin, eid)
            if err or not raw:
                body_summaries[eid] = {"status": "fetch_error", "summary": "[no body content]", "error": err}
                continue

            # 2. Parse MIME → text body
            body = parse_email_body(raw)
            if not body or len(body.strip()) < 20:
                body_summaries[eid] = {"status": "empty", "summary": "[no body content]"}
                continue

            # 3. Compact (strip signatures, disclaimers, truncate)
            compacted = compact_body(body)

            # 4. LLM summarize
            sender = _extract_sender(email)
            subject = email.get("subject", "No Subject")
            result = summarize_email_body(compacted, sender, subject, model=model)
            body_summaries[eid] = result

        _summary_elapsed = int((time.time() - _summary_start) * 1000)
        if emails:
            print(f"📝 Summarized {len([v for v in body_summaries.values() if v.get('status') == 'success'])}/{len(emails)} emails in {_summary_elapsed}ms", flush=True)
            if _summary_elapsed > 45000:
                print(f"⚠️ Summarization budget exceeded: {_summary_elapsed}ms for {len(emails)} emails", flush=True)

    # ── Pre-load cross-channel data (Phase 2.2 — hoisted above loop) ────
    # Fetched once per tick, not per DIGEST email. Prevents N×2 SQL queries
    # inside the loop when multiple emails are DIGEST in the same tick.
    tick_active_threads = _get_active_threads(db_path)
    tick_pinned_topics = _get_pinned_topics(db_path)
    tick_priority_topics = tick_active_threads + tick_pinned_topics

    # ── Log batch extraction metrics to traces (Rule 15 — Observability) ──
    _llm_extracted = 0
    _regex_fallback = 0

    for email in emails:
        email_id = str(email.get("id", ""))
        sender = _extract_sender(email)
        subject = email.get("subject", "No Subject")

        # ── Log Signal (all emails, not just new ones) ───────────────
        # Use LLM-extracted topic if available, fall back to regex
        if email_id in batch_topics:
            bt = batch_topics[email_id]
            topic = bt.get("topic")
            entity_text = bt.get("entity_text")
            entity_type = bt.get("entity_type")
            _llm_extracted += 1
        else:
            topic, entity_text, entity_type = rules.extract_topic_from_subject(subject)
            _regex_fallback += 1

        summary_data = body_summaries.get(email_id, {})
        summary_text = summary_data.get("summary")

        # Sender trust assessment
        sender_addr = _extract_sender_addr(email)
        sender_name = _extract_sender_name(email)
        # Use owner's email for self-detection if available in RuleEngine or config
        owner_email = None
        # In this script, RuleEngine is instantiated with db_path and config can be retrieved
        # but let's keep it simple and just use the contact graph for now as initially planned.
        trust = assess_sender_trust(sender_addr, sender_name, db_path)

        rules.log_signal(
            source="email",
            topic_hint=topic,
            entity_text=entity_text,
            entity_type=entity_type,
            content_preview=f"{sender}: {subject}",
            ref_id=email_id,
            ref_source="email",
            summary=summary_text,
            summary_model=summary_data.get("model"),
            summary_ms=summary_data.get("duration_ms"),
            sender_trust=trust.tier,
            sender_contact_id=trust.contact_id,
        )

        if not email_id or email_id in seen:
            continue  # Skip triage/alerts for already-seen emails

        # ── User-declared triage rules
        sender_lower = sender.lower()
        rule_verdict = None

        # 1. Zero-cost Pre-filter (Option B) for obvious automated emails
        auto_noise_patterns = ["noreply@", "no-reply@", "notifications@", "newsletter@", "automated@", "mailer-daemon@"]
        for p in auto_noise_patterns:
            if p in sender_lower:
                rule_verdict = "NOISE"
                print(f"🤖 Pre-filter: Auto-marked {sender} as NOISE", flush=True)
                break

        # 2. User-declared triage rules (always overrides pre-filter if specific match exists)
        if not rule_verdict:
            for entity_key, rule_status in triage_rules.items():
                if entity_key in sender_lower:
                    rule_verdict = rule_status
                    print(f"📋 Triage rule matched: '{entity_key}' → {rule_status} for {sender}", flush=True)
                    break

        # Determine if we should ping or digest — skip LLM if rule or pre-filter matched
        verdict = rule_verdict if rule_verdict else classify_email(email, model=model)

        # ── Cross-Channel Escalation Check ──
        if verdict == "DIGEST" and topic:
            verdict, subject = _should_escalate(verdict, topic, subject, tick_priority_topics)

        rules.log_triage(email_id, sender, subject, verdict)

        if verdict == "DEFER":
            print(f"⏳ Ollama busy, deferring triage for {email_id}", flush=True)
            continue  # Try again next tick (we won't mark as seen)

        if verdict == "URGENT":
            alert = rules.evaluate_email(email, email_rules, sender_trust=trust)
            if alert:
                notifier.send(alert)
                print(f"📬 URGENT: Alert sent for email {email_id}", flush=True)
        else:
            # DIGEST or NOISE (both go to triage_log, which is now the queue)
            print(f"📥 {verdict}: Logged email {email_id}", flush=True)

        _mark_seen(db_path, email_id)

    # --- Log batch extraction metrics ----------------------------------------
    if emails:
        try:
            with sqlite3.connect(db_path) as conn:
                import uuid as _uuid

                conn.execute(
                    "INSERT INTO traces (id, intent, plan, status) VALUES (?, ?, ?, ?)",
                    (
                        f"batch_extract_{_uuid.uuid4().hex[:8]}",
                        "batch_extract",
                        json.dumps(
                            {
                                "total_emails": len(emails),
                                "llm_extracted": _llm_extracted,
                                "regex_fallback": _regex_fallback,
                                "success_rate_pct": round(_llm_extracted / len(emails) * 100, 1) if emails else 0,
                            }
                        ),
                        "completed",
                    ),
                )
        except Exception:
            pass

    # --- Reflection Loop ----------------------------------------------------
    # Run intelligence cycle after emails have been ingested
    reflect(notifier, db_path, model=model)


def digest_tick(notifier: TelegramNotifier, rules: RuleEngine, model: str = "llama3.2:latest", force: bool = False):
    """Compile and send the hourly digest base on triage_log watermark."""
    if is_quiet_hours():
        return

    items = rules.get_digest_items()
    if not items:
        if force:
            notifier.send("📥 Recap — no new emails triaged since last update. All quiet!")
            rules.update_watermark()
        return

    # Filter out pure noise for hourly updates unless force=True
    has_notable = any(i["verdict"] == "DIGEST" for i in items)
    if not has_notable and not force:
        print(f"🔇 Skipping hourly digest: {len(items)} items are all NOISE", flush=True)
        return

    print(f"📦 Sending summary ({len(items)} items)", flush=True)

    # Try conversational synthesis
    summary = _synthesize_digest(items, model=model)

    if not summary:
        # Fallback to Quick Mode
        lines = ["📥 **Email recap** *(quick mode)*"]
        notable = [i for i in items if i["verdict"] == "DIGEST"]
        noise = [i["sender"] for i in items if i["verdict"] == "NOISE"]

        if notable:
            lines.append("Notable:")
            for item in notable[:15]:
                lines.append(f"• {item['sender']}: {item['subject']}")

        if noise:
            unique_noise = sorted(list(set(noise)))
            lines.append(f"\nOther: {', '.join(unique_noise[:20])}")

        summary = "\n".join(lines)

    notifier.send(summary)
    rules.log_background_event(summary, "inbox_digest")
    rules.update_watermark()


def _run_memory_decay(db_path: Path):
    """Mark ledger rows as expired if past their decay_days."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("""
                UPDATE ledger
                SET status = 'expired'
                WHERE decay_days IS NOT NULL
                  AND (status IS NULL OR status != 'expired')
                  AND julianday('now') - julianday(created_at) > decay_days
            """)
            if cursor.rowcount > 0:
                print(f"🧹 Memory Decay: Expired {cursor.rowcount} stale ledger rows", flush=True)
    except Exception as e:
        # Avoid crashing if the decay_days column isn't created yet or other SQL error
        if "no such column: decay_days" not in str(e):
            print(f"⚠️ Error running memory decay: {e}", flush=True)


def _normalize_topic(topic: str | None) -> str | None:
    """Consolidates fragmented topics (e.g. scheduling -> schedule)."""
    if not topic:
        return None

    # 1. Lowercase + cleanup
    t = topic.lower().replace("_", " ").strip()

    # 2. Stopwords
    stopwords = {"my", "the", "a", "an", "this", "our", "your", "on", "for"}
    words = [w for w in t.split() if w not in stopwords]
    if not words:
        return t  # fallback to raw if we stripped everything

    t = " ".join(words)

    # 3. Simple Stemming (suffix stripping)
    suffixes = ["ing", "s"]
    for suffix in suffixes:
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            t = t[: -len(suffix)]
            break

    # 4. Synonym Mapping
    synonyms = {
        "calendar": "schedule",
        "schedul": "schedule",
        "schedular": "schedule",
        "mail": "email",
        "inbox": "email",
        "message": "chat",
        "telegr": "chat",
        "work": "job",
        "doc": "document",
    }
    return synonyms.get(t, t)


# _get_active_threads and _get_pinned_topics are now the canonical implementations
# in bregger_utils.py, imported at the top of this file.
# Local definitions removed to eliminate the dual-implementation smell (code review #2).


def _synthesize_threads(threads: list, model: str) -> str:
    if not threads:
        return ""

    thread_lines = [f"- {t['topic']} ({t['count']} signals)" for t in threads]

    prompt = (
        "You are Bregger, a personal assistant. Synthesize this list of active "
        "topics from the user's signals this week into a short, natural language summary. "
        "Keep it to 2-3 sentences. E.g. 'Your active threads this week include your apartment search and 2 emails about Acme Corp.'\n\n"
        "THREADS:\n" + "\n".join(thread_lines)
    )

    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 100, "temperature": 0.7}}
    ).encode()

    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with inference_lock, urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            summary = resp.get("response", "").strip()
            if summary:
                return f"🧠 **Active Threads**\n\n{summary}"
            return ""
    except Exception as e:
        print(f"⚠️ [synthesize_threads] LLM synthesis failed: {e}", flush=True)
        return ""


def reflection_tick(notifier: TelegramNotifier, rules: RuleEngine, db_path: Path, model: str = "llama3.2:latest"):
    """Nightly reflection loop: decay memory, surface active threads, propose one action.

    Three steps:
    1. Memory decay — expire stale ledger rows.
    2. Active Threads summary — LLM synthesizes a human-friendly digest of hot topics.
    3. reflect() — LLM reasons over signal patterns and proposes at most ONE task,
       only when it can cite a specific deadline, unanswered thread, or time-sensitive
       pattern. Replaces the old hardcoded per-topic proposal loop.
    """
    import uuid as _uuid

    # Step 1: Memory decay
    _run_memory_decay(db_path)

    # Step 2: Active Threads summary
    threads = _get_active_threads(db_path)
    if threads:
        print(f"🧠 Found {len(threads)} active threads. Synthesizing...", flush=True)
        digest = _synthesize_threads(threads, model=model)
        if digest:
            notifier.send(digest)
            rules.log_background_event(digest, "reflection")

    # Step 3: Single LLM-reasoned proposal (replaces hardcoded per-topic spam loop)
    reflect(notifier, db_path, model=model)

    # Trace
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO traces (id, intent, plan, status) VALUES (?, ?, ?, ?)",
                (str(_uuid.uuid4()), "reflection_tick", "{}", "completed"),
            )
    except Exception as e:
        print(f"⚠️ Failed to log reflection_tick trace: {e}", flush=True)


def recap_tick(notifier: TelegramNotifier, rules: RuleEngine, model: str = "llama3.2:latest"):
    """AM/PM Recap logic."""
    now = datetime.now()
    # Check if we've sent a digest in the last 8 hours (approx morning/afternoon window)
    if not rules.was_digest_sent_since(now.replace(hour=now.hour - 8 if now.hour >= 8 else 0)):
        digest_tick(notifier, rules, model=model, force=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_secrets(workdir: Path):
    for candidate in [
        workdir / "secrets.env",
        Path(os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "bregger_deployment")))
        / "secrets.env",
    ]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
            print(f"🔑 Secrets loaded from {candidate}", flush=True)
            return


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".bregger" / "config.json"
    if not config_path.exists():
        print(f"❌ Config not found at {config_path}. Run 'bregger init' first.", flush=True)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    workdir = config_path.parent
    db_path = workdir / "data" / "bregger.db"
    skills_dir = workdir / "skills"

    _load_secrets(workdir)

    # Telegram
    token = os.environ.get("BREGGER_TELEGRAM_TOKEN", "")
    if not token:
        print("❌ BREGGER_TELEGRAM_TOKEN not set. Heartbeat cannot send notifications.", flush=True)
        sys.exit(1)

    allowed_chats = [c.strip() for c in os.environ.get("BREGGER_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()]
    if not allowed_chats:
        print("⚠️ BREGGER_TELEGRAM_ALLOWED_CHAT_IDS not set — notifications will be broadcast to no one.", flush=True)

    notifier = TelegramNotifier(token, allowed_chats)
    rules = RuleEngine(db_path)
    rules._ensure_triage_tables()

    interval_secs = POLL_INTERVAL_MINUTES * 60
    ticks_per_hour = 60 // POLL_INTERVAL_MINUTES
    tick_count = 0

    llm_conf = config.get("llm", {})
    triage_model = llm_conf.get("triage_model") or llm_conf.get("model", "llama3.2:latest")

    print(f"💓 Bregger Heartbeat started — polling every {POLL_INTERVAL_MINUTES} min")
    print(f"🤖 Triage model: {triage_model}")
    print(f"🌙 Quiet hours: {QUIET_HOUR_START}:00 – {QUIET_HOUR_END}:00", flush=True)

    while True:
        try:
            tick(skills_dir, db_path, notifier, rules, model=triage_model)

            # Run digest summary every ~1 hour
            tick_count += 1
            now = datetime.now()

            # AM Recap (9:00 - 9:15) or PM Recap (18:00 - 18:15)
            # Use small windows to avoid double-firing within the 15min tick
            is_recap_window = (now.hour == 9 and now.minute < 16) or (now.hour == 18 and now.minute < 16)

            if is_recap_window:
                recap_tick(notifier, rules, model=triage_model)
                # If we just did a recap, we count it as a digest cycle
                tick_count = 0
            elif tick_count >= ticks_per_hour:
                digest_tick(notifier, rules, model=triage_model)
                tick_count = 0

            # Reflection (7:00 - 7:15 AM)
            is_reflection_window = now.hour == 7 and now.minute < 16
            if is_reflection_window:
                sent_today = False
                try:
                    with sqlite3.connect(db_path) as conn:
                        cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_reflection_at'")
                        row = cursor.fetchone()
                        if row and row[0].startswith(now.strftime("%Y-%m-%d")):
                            sent_today = True
                except Exception:
                    pass

                if not sent_today:
                    reflection_tick(notifier, rules, db_path, model=triage_model)
                    try:
                        with sqlite3.connect(db_path) as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_reflection_at', CURRENT_TIMESTAMP)"
                            )
                    except Exception:
                        pass

        except Exception as e:
            print(f"❌ Heartbeat tick error: {e}", flush=True)
        time.sleep(interval_secs)


if __name__ == "__main__":
    main()
