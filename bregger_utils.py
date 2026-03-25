"""
bregger_utils.py — Shared utilities for the Bregger framework.
"""

import threading
import sqlite3
from pathlib import Path

# ── Inference Mutex (Rule 19) ────────────────────────────────────────
# Shared lock ensuring only one LLM call runs at a time across all
# threads (chat, heartbeat, passive memory). Background threads queue
# behind active chat inference.  Import and use:
#   from bregger_utils import inference_lock
#   with inference_lock:
#       provider.generate(...)
inference_lock = threading.RLock()

def normalize_topic(topic: str | None) -> str | None:
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
            t = t[:-len(suffix)]
            break
            
    # 4. Synonym Mapping
    synonyms = {
        "calendar": "schedule",
        "schedul": "schedule",
        "schedular": "schedule",
        "mail": "email",
        "inbox": "email",
        "message": "chat",
        "presentation_deck": "presentation deck",
        "deck": "presentation deck"
    }
    return synonyms.get(t, t)

def get_active_threads(db_path: Path, window_days: int = 7, min_count: int = 2, limit: int = 7) -> list:
    """Return topics seen min_count+ times in the last window_days days.

    Single source of truth used by both bregger_core (prompt injection) and
    bregger_heartbeat (cross-channel escalation).  All aggregation is done in
    Python after fetching raw rows so normalize_topic() is applied uniformly
    and multi-source counts are correct (avoids the SQL GROUP BY / arbitrary
    source-column ambiguity issue).

    Returns a list of dicts: [{topic, count, sources: list[str], last_seen}]
    sorted by count descending.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT topic_hint, source, timestamp FROM signals "
                "WHERE topic_hint IS NOT NULL "
                "  AND timestamp > datetime('now', ?) "
                "  AND (env IS NULL OR env = 'production') ",
                (f"-{window_days} days",)
            )
            rows = cursor.fetchall()

        normalized: dict = {}
        for topic, source, ts in rows:
            norm = normalize_topic(topic)
            if not norm:
                continue
            if norm not in normalized:
                normalized[norm] = {"count": 0, "sources": set(), "last_seen": ts}
            normalized[norm]["count"] += 1
            normalized[norm]["sources"].add(source or "unknown")
            if ts > normalized[norm]["last_seen"]:
                normalized[norm]["last_seen"] = ts

        active = [
            {
                "topic": topic,
                "count": data["count"],
                "sources": sorted(data["sources"]),
                "last_seen": data["last_seen"],
            }
            for topic, data in normalized.items()
            if data["count"] >= min_count
        ]
        return sorted(active, key=lambda x: x["count"], reverse=True)[:limit]

    except Exception as e:
        print(f"⚠️ [get_active_threads] Error: {e}", flush=True)
        return []


def get_pinned_topics(db_path: Path) -> list:
    """Return pinned topics as [{topic, count: 100, pinned: True}].

    Single source of truth for pinned topic lookups used by both core and heartbeat.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("SELECT topic FROM pinned_topics")
            pinned = []
            for row in cursor.fetchall():
                raw = row[0]
                norm = normalize_topic(raw) or raw
                pinned.append({"topic": norm, "count": 100, "pinned": True})
            return pinned
    except Exception as e:
        print(f"⚠️ [get_pinned_topics] Error: {e}", flush=True)
        return []


def ensure_signals_schema(db_path) -> None:
    """Single source of truth for the signals table schema.

    Called by both bregger_core._ensure_signals_table() and bregger_heartbeat.log_signal()
    so the schema is defined in exactly one place. Any new columns must be added here only.

    Uses ALTER TABLE migrations (try/except) for columns added after the initial release,
    so existing live DBs upgrade in-place without data loss.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            # Base schema — 9 original columns
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source          TEXT NOT NULL,     -- 'chat' | 'email'
                    topic_hint      TEXT,              -- nullable, subject-matter clustering label
                    entity_text     TEXT,              -- nullable, person/org/project name
                    entity_type     TEXT,              -- 'person' | 'org' | 'project' | 'other'
                    content_preview TEXT NOT NULL,     -- truncated to 280 chars
                    ref_id          TEXT,              -- trace_id, email_id, or conversation row id
                    ref_source      TEXT               -- 'traces' | 'conversation_history' | 'email'
                )
            """)

            # Migration: reflection lifecycle columns (added 2026-03-22)
            # Migration: env column for test-data isolation (added 2026-03-23)
            for col_sql in [
                "ALTER TABLE signals ADD COLUMN proposal_status TEXT DEFAULT 'active'",
                "ALTER TABLE signals ADD COLUMN dismissed_at DATETIME",
                "ALTER TABLE signals ADD COLUMN env TEXT DEFAULT 'production'",
            ]:
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Backfill: ensure no NULLs hide signals from reflection queries
            conn.execute(
                "UPDATE signals SET proposal_status = 'active' WHERE proposal_status IS NULL"
            )
    except Exception as e:
        print(f"⚠️ [ensure_signals_schema] Failed: {e}", flush=True)


def parse_semantic_datetime(token: str, ref_tz: str = "America/New_York") -> __import__('datetime').datetime:
    """
    Parses semantic temporal tokens like 'tomorrow_1400' or 'friday_0930'.
    Falls back to strict ISO 8601 parsing if no semantic pattern matches.
    """
    import re
    from datetime import datetime, timedelta
    import zoneinfo

    try:
        tz = zoneinfo.ZoneInfo(ref_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("America/New_York")

    now = datetime.now(tz)
    
    # 1. Semantic parsing: format <day>_<HHMM>
    match = re.match(r'^([a-z]+)_(\d{4})$', token.lower())
    if match:
        day_str, time_str = match.groups()
        hour, minute = int(time_str[:2]), int(time_str[2:])
        
        target_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        if day_str == "today":
            pass
        elif day_str == "tomorrow":
            target_date += timedelta(days=1)
        else:
            # Day of week parsing: 'monday', 'tuesday', etc.
            days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if day_str in days_of_week:
                target_dow = days_of_week.index(day_str)
                current_dow = now.weekday()
                
                # Calculate days ahead: if today is target_dow, and time has passed, assume next week.
                days_ahead = target_dow - current_dow
                if days_ahead < 0 or (days_ahead == 0 and now >= target_date):
                    days_ahead += 7
                
                target_date += timedelta(days=days_ahead)
            else:
                raise ValueError(f"Unknown day format in semantic token: {day_str}")
                
        return target_date

    # 2. Strict ISO format fallback
    return datetime.fromisoformat(token.replace("Z", "+00:00")).astimezone(tz)
