from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from xibi.db import open_db
from xibi.router import Config, get_model

logger = logging.getLogger(__name__)


@dataclass
class SignalIntel:
    signal_id: int
    action_type: str | None = None  # 'request' | 'reply' | 'fyi' | 'confirmation'
    urgency: str | None = None  # 'high' | 'medium' | 'low'
    direction: str | None = None  # 'inbound' | 'outbound'
    entity_org: str | None = None
    is_direct: int | None = None  # 1 or 0
    cc_count: int | None = None
    thread_id: str | None = None
    intel_tier: int = 0  # highest tier applied
    thread_id_hint: str | None = None  # Temporary hint for thread assignment


def extract_tier0(signal_row: dict) -> SignalIntel:
    """Pure Python, zero cost. Reads only from the signal dict."""
    intel = SignalIntel(signal_id=signal_row["id"])

    # Direction
    source = signal_row.get("source")
    ref_source = signal_row.get("ref_source")
    if ref_source in ("sent", "outbox"):
        intel.direction = "outbound"
    elif source in ("email", "chat"):
        intel.direction = "inbound"

    content = signal_row.get("content_preview", "")

    # CC Count
    cc_match = re.search(r"^CC:\s*(.*)$", content, re.MULTILINE | re.IGNORECASE)
    if cc_match:
        cc_line = cc_match.group(1)
        if cc_line.strip():
            intel.cc_count = len([addr for addr in cc_line.split(",") if addr.strip()])
        else:
            intel.cc_count = 0

    # Is Direct
    # Simplification: for this step, is_direct stays NULL unless the content_preview
    # explicitly contains a To: header line. Don't try to parse the user's address.
    to_match = re.search(r"^To:\s*(.*)$", content, re.MULTILINE | re.IGNORECASE)
    if to_match:
        # We don't know the user's address here, so we leave it as NULL per instructions
        # "If found and the preview contains the user's address (don't hardcode the address — return NULL if unknown), set 1."
        pass

    return intel


def extract_tier1_batch(signals: list[dict], config: Config | None) -> list[SignalIntel]:
    """Batch fast role call. One LLM call for up to 20 signals."""
    if not signals:
        return []

    batch = signals[:20]
    signal_lines = []
    for i, s in enumerate(batch):
        line = (
            f"[{i}] source={s.get('source')}, topic={s.get('topic_hint')}, preview={s.get('content_preview', '')[:120]}"
        )
        signal_lines.append(line)

    signals_block = "\n".join(signal_lines)
    prompt = f"""Extract structured intelligence for each signal below.

For each signal, output a JSON object with exactly these fields:
  "action_type": "request" | "reply" | "fyi" | "confirmation" | null
  "urgency": "high" | "medium" | "low"
  "direction": "inbound" | "outbound"
  "entity_org": organization name (string) or null
  "thread_id_hint": short snake_case identifier for grouping (≤30 chars, e.g. "acme_job_application") or null

Output a JSON array with one object per signal, in input order. No commentary.

SIGNALS:
{signals_block}
"""

    try:
        model = get_model(specialty="text", effort="fast", config=config)
        response = model.generate(prompt)

        # Simple JSON extraction in case there's markdown
        json_str = response.strip()
        if json_str.startswith("```json"):
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0].strip()
        elif json_str.startswith("```"):
            json_str = json_str.split("```", 1)[1].split("```", 1)[0].strip()

        data = json.loads(json_str)
        if not isinstance(data, list):
            raise ValueError("LLM response is not a list")

        results = []
        valid_actions = {"request", "reply", "fyi", "confirmation"}
        valid_urgencies = {"high", "medium", "low"}
        valid_directions = {"inbound", "outbound"}

        for i, item in enumerate(data):
            if i >= len(batch):
                break

            intel = SignalIntel(signal_id=batch[i]["id"], intel_tier=1)

            at = item.get("action_type")
            intel.action_type = at if at in valid_actions else None

            urg = item.get("urgency")
            intel.urgency = urg if urg in valid_urgencies else None

            direc = item.get("direction")
            intel.direction = direc if direc in valid_directions else None

            intel.entity_org = item.get("entity_org")
            hint = item.get("thread_id_hint")
            if hint and isinstance(hint, str):
                intel.thread_id_hint = hint[:30]

            results.append(intel)

        # Fill remaining if LLM returned fewer
        while len(results) < len(batch):
            results.append(SignalIntel(signal_id=batch[len(results)]["id"], intel_tier=1))

        return results

    except Exception as e:
        logger.error(f"extract_tier1_batch failed: {e}")
        return [SignalIntel(signal_id=s["id"], intel_tier=1) for s in batch]


def assign_threads(signals: list[dict], intels: list[SignalIntel], db_path: Path) -> list[SignalIntel]:
    """Python-only thread assignment. Mutates intels in-place."""
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Active threads updated in last 7 days
            active_threads = conn.execute(
                "SELECT * FROM threads WHERE status = 'active' AND updated_at > datetime('now', '-7 days')"
            ).fetchall()
            active_threads = [dict(t) for t in active_threads]

            # Map of thread_id -> list of entity_text (sender addresses) associated with it
            thread_entities: dict[str, set[str]] = {}
            for t in active_threads:
                tid = t["id"]
                rows = conn.execute("SELECT DISTINCT entity_text FROM signals WHERE thread_id = ?", (tid,)).fetchall()
                thread_entities[tid] = {r[0] for r in rows if r[0]}

        for sig, intel in zip(signals, intels, strict=False):
            topic_hint = (sig.get("topic_hint") or "").lower()
            entity_text = sig.get("entity_text")

            assigned_tid = None

            # 1. Exact sender+topic match
            for t in active_threads:
                tname = t["name"].lower()
                if (
                    topic_hint
                    and topic_hint in tname
                    and entity_text
                    and entity_text in thread_entities.get(t["id"], set())
                ):
                    assigned_tid = t["id"]
                    break

            # 2. Hint-based match
            if not assigned_tid and intel.thread_id_hint:
                hint_prefix = intel.thread_id_hint[:20]
                for t in active_threads:
                    if t["id"].startswith(f"thread-{hint_prefix}"):
                        assigned_tid = t["id"]
                        break

            # 3. Create new thread
            if not assigned_tid:
                hash8 = hashlib.md5(
                    ((sig.get("topic_hint") or "") + (sig.get("entity_text") or "")).encode()
                ).hexdigest()[:8]
                hint_part = (intel.thread_id_hint[:15] + "-") if intel.thread_id_hint else ""
                assigned_tid = f"thread-{hint_part}{hash8}"

                name = (sig.get("topic_hint") or sig.get("content_preview", ""))[:60]
                source_channels = json.dumps([sig["source"]])

                with open_db(db_path) as conn, conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO threads (id, name, source_channels, signal_count)
                        VALUES (?, ?, ?, 0)
                    """,
                        (assigned_tid, name, source_channels),
                    )

            intel.thread_id = assigned_tid

            # Update thread metadata
            with open_db(db_path) as conn, conn:
                conn.row_factory = sqlite3.Row
                t_row = conn.execute(
                    "SELECT source_channels, signal_count FROM threads WHERE id = ?", (assigned_tid,)
                ).fetchone()
                if t_row:
                    channels = json.loads(t_row["source_channels"])
                    if sig["source"] not in channels:
                        channels.append(sig["source"])

                    conn.execute(
                        """
                        UPDATE threads SET
                            signal_count = signal_count + 1,
                            source_channels = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """,
                        (json.dumps(channels), assigned_tid),
                    )

    except Exception as e:
        logger.error(f"assign_threads failed: {e}")

    return intels


def upsert_contact(email: str, display_name: str, organization: str | None, db_path: Path) -> str:
    """Upsert a contact. Returns the contact_id."""
    contact_id = "contact-" + hashlib.md5(email.lower().encode()).hexdigest()[:8]
    try:
        with open_db(db_path) as conn, conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute("SELECT organization FROM contacts WHERE id = ?", (contact_id,)).fetchone()

            if existing:
                if existing["organization"] is None and organization:
                    conn.execute(
                        """
                        UPDATE contacts SET
                            last_seen = CURRENT_TIMESTAMP,
                            signal_count = signal_count + 1,
                            organization = ?
                        WHERE id = ?
                    """,
                        (organization, contact_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE contacts SET
                            last_seen = CURRENT_TIMESTAMP,
                            signal_count = signal_count + 1
                        WHERE id = ?
                    """,
                        (contact_id,),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO contacts (id, display_name, email, organization, relationship, signal_count)
                    VALUES (?, ?, ?, ?, 'unknown', 1)
                """,
                    (contact_id, display_name, email, organization),
                )
    except Exception as e:
        logger.error(f"upsert_contact failed: {e}")

    return contact_id


def merge_intels(tier0: list[SignalIntel], tier1: list[SignalIntel]) -> list[SignalIntel]:
    """Combine tier0 and tier1 results."""
    merged = []
    for t0, t1 in zip(tier0, tier1, strict=False):
        # Use t1 as base if it exists (it has intel_tier=1)
        res = SignalIntel(signal_id=t0.signal_id, intel_tier=t1.intel_tier)

        res.action_type = t1.action_type if t1.action_type else t0.action_type
        res.urgency = t1.urgency if t1.urgency else t0.urgency
        res.direction = t1.direction if t1.direction else t0.direction
        res.entity_org = t1.entity_org if t1.entity_org else t0.entity_org
        res.is_direct = t1.is_direct if t1.is_direct is not None else t0.is_direct
        res.cc_count = t1.cc_count if t1.cc_count is not None else t0.cc_count
        res.thread_id_hint = t1.thread_id_hint

        merged.append(res)
    return merged


def enrich_signals(db_path: Path, config: Config | None, batch_size: int = 20) -> int:
    """Main entry point. Returns count of signals enriched. Never raises."""
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals WHERE intel_tier = 0 ORDER BY id ASC LIMIT ?", (batch_size,)
            ).fetchall()

        if not rows:
            return 0

        signals = [dict(r) for r in rows]

        # Tier 0 extraction (free)
        tier0_intels = [extract_tier0(s) for s in signals]

        # Tier 1 extraction (fast role batch call)
        tier1_intels = extract_tier1_batch(signals, config)

        # Merge: use tier1 fields where available, tier0 as fallback
        merged = merge_intels(tier0_intels, tier1_intels)

        # Thread assignment
        merged = assign_threads(signals, merged, db_path)

        # Contact upsert
        for sig in signals:
            entity = sig.get("entity_text", "")
            if entity and "@" in entity:
                upsert_contact(entity, entity, None, db_path)

        # Write back to DB
        with open_db(db_path) as conn, conn:
            for intel in merged:
                conn.execute(
                    """
                    UPDATE signals SET
                        action_type = ?, urgency = ?, direction = ?, entity_org = ?,
                        is_direct = ?, cc_count = ?, thread_id = ?, intel_tier = ?
                    WHERE id = ?
                """,
                    (
                        intel.action_type,
                        intel.urgency,
                        intel.direction,
                        intel.entity_org,
                        intel.is_direct,
                        intel.cc_count,
                        intel.thread_id,
                        intel.intel_tier,
                        intel.signal_id,
                    ),
                )

        return len(merged)

    except Exception as e:
        logger.error(f"enrich_signals failed: {e}", exc_info=True)
        return 0
