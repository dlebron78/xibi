"""Tier 2 backfill CLI — re-run open-shape fact extraction on a single signal.

Step-112. Usage:

    python -m xibi.heartbeat.tier2_backfill --signal-id <id> [--force]

Looks up the signal by id, dispatches via :class:`Tier2ExtractorRegistry` for
the signal's ``source``, runs the extractor (which fetches its own body via
the source-appropriate primitive — himalaya for email, MCP for future
sources, etc.), and writes the resulting ``extracted_facts`` back to the
parent row. If ``extracted_facts.digest_items`` is non-empty, the CLI fans
out child rows the same way the live poller path does — with synthetic
per-item ref_ids for idempotent re-runs against the existing 72h
``(ref_source, ref_id)`` dedup window.

Honors ``--force`` to overwrite an already-populated ``extracted_facts``;
without it, signals that already have facts are left alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from xibi.db import migrate as run_migrations
from xibi.db import open_db
from xibi.heartbeat.tier2_extractors import Tier2ExtractorRegistry
from xibi.router import load_config

logger = logging.getLogger(__name__)


def _resolve_db_path(config_path: str) -> Path:
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = {}
    db = cfg.get("db_path") if isinstance(cfg, dict) else None
    return Path(db).expanduser() if db else Path.home() / ".xibi" / "data" / "xibi.db"


def _resolve_model(config_path: str) -> str:
    """Resolve the fast text model from config — same path as the live poller."""
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = {}
    return (
        cfg.get("models", {}).get("text", {}).get("fast", {}).get("model", "gemma4:e4b")
        if isinstance(cfg, dict)
        else "gemma4:e4b"
    )


def _fetch_signal(conn: sqlite3.Connection, signal_id: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return dict(row) if row else None


def _backfill_one(db_path: Path, signal_id: str, model: str, force: bool) -> int:
    """Backfill a single signal. Returns 0 on success, non-zero on error."""
    with open_db(db_path) as conn:
        signal = _fetch_signal(conn, signal_id)

    if signal is None:
        logger.error("signal id=%s not found", signal_id)
        return 2

    if signal.get("extracted_facts") and not force:
        logger.info("signal id=%s already has extracted_facts; pass --force to overwrite", signal_id)
        return 0

    source = signal.get("source")
    if not source:
        logger.error("signal id=%s has no source field", signal_id)
        return 3

    extractor = Tier2ExtractorRegistry.get(str(source))
    if extractor is None:
        logger.error(
            "tier2 skipped: no registered extractor for source=%s (registered: %s)",
            source,
            Tier2ExtractorRegistry.sources(),
        )
        return 4

    try:
        facts = extractor(signal, None, model)
    except Exception as exc:
        logger.error("tier2 skipped: extractor raised for signal_id=%s err=%s", signal_id, exc)
        return 5

    if facts is None:
        logger.warning("tier2 skipped: summary failed for signal_id=%s", signal_id)
        return 0

    facts_json = json.dumps(facts)

    with open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE signals SET extracted_facts = ? WHERE id = ?",
            (facts_json, signal_id),
        )

        # Fan out children if this is a digest envelope.
        items = facts.get("digest_items") or []
        is_digest_parent = bool(facts.get("is_digest_parent")) and len(items) > 0
        if is_digest_parent:
            _fanout_children(conn, signal, facts, items)

    logger.info(
        "tier2 ok: signal_id=%s type=%s facts_keys=%d digest_items=%d",
        signal_id,
        facts.get("type"),
        len(facts),
        len(items) if is_digest_parent else 0,
    )
    return 0


def _fanout_children(
    conn: sqlite3.Connection,
    parent: dict,
    parent_facts: dict,
    items: list,
) -> None:
    """Insert per-item child rows. Mirrors the poller's fan-out logic.

    Child rows inherit ``ref_source`` from the parent (condition #10 of
    step-112's TRR Record). Synthetic ref_ids ``<parent_ref_id>:<index>``
    keep re-runs idempotent via the existing 72h ``(ref_source, ref_id)``
    dedup window in :mod:`xibi.alerting.rules`.
    """
    parent_ref_id = str(parent.get("ref_id") or "")
    parent_ref_source = parent.get("ref_source")

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        child_ref_id = f"{parent_ref_id}:{idx}"

        # Idempotency check — skip if the child already exists for this
        # (ref_source, ref_id) tuple within the dedup window.
        cursor = conn.execute(
            "SELECT 1 FROM signals WHERE ref_source = ? AND ref_id = ?",
            (parent_ref_source, child_ref_id),
        )
        if cursor.fetchone():
            continue

        item_fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        preview_bits: list[str] = []
        for key in ("title", "company", "location", "url", "comp_range", "match_reason"):
            val = item_fields.get(key) if isinstance(item_fields, dict) else None
            if val:
                preview_bits.append(f"{key}={val}")
        child_preview = " | ".join(preview_bits) or str(item)[:280]
        if len(child_preview) > 280:
            child_preview = child_preview[:277] + "..."

        conn.execute(
            """
            INSERT INTO signals (
                source, topic_hint, entity_text, entity_type, content_preview,
                ref_id, ref_source,
                summary, summary_model, summary_ms,
                sender_trust, sender_contact_id, classification_reasoning, deep_link_url,
                metadata, received_via_account, received_via_email_alias,
                extracted_facts, parent_ref_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parent.get("source"),
                str(item.get("type") or parent_facts.get("type") or ""),
                parent.get("entity_text"),
                parent.get("entity_type"),
                child_preview,
                child_ref_id,
                parent_ref_source,
                None,
                None,
                None,
                parent.get("sender_trust"),
                parent.get("sender_contact_id"),
                None,
                parent.get("deep_link_url"),
                None,
                parent.get("received_via_account"),
                parent.get("received_via_email_alias"),
                json.dumps(item),
                parent_ref_id,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xibi.heartbeat.tier2_backfill")
    parser.add_argument("--signal-id", required=True, help="signals.id of the row to backfill")
    parser.add_argument("--force", action="store_true", help="overwrite existing extracted_facts")
    parser.add_argument("--config", default="config.json", help="path to config.json (default: ./config.json)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable INFO-level logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    db_path = _resolve_db_path(args.config)
    if not db_path.exists():
        logger.error("db not found at %s", db_path)
        return 1

    # Ensure migrations are applied so the column exists.
    run_migrations(db_path)

    model = _resolve_model(args.config)
    return _backfill_one(db_path, args.signal_id, model, args.force)


if __name__ == "__main__":
    sys.exit(main())
