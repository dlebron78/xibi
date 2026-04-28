"""Step-112: tier2_backfill CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._helpers import _migrated_db
from xibi.alerting.rules import RuleEngine
from xibi.db import open_db
from xibi.heartbeat.tier2_backfill import _backfill_one
from xibi.heartbeat.tier2_extractors import Tier2ExtractorRegistry


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _migrated_db(tmp_path)


def _seed_signal(db_path: Path, ref_id: str, source: str = "email") -> int:
    rules = RuleEngine(db_path)
    with open_db(db_path) as conn:
        rules.log_signal_with_conn(
            conn,
            source=source,
            topic_hint="seed",
            entity_text="seed@example.com",
            entity_type="email",
            content_preview="seed",
            ref_id=ref_id,
            ref_source=source,
        )
        conn.commit()
        row = conn.execute("SELECT id FROM signals WHERE ref_id = ?", (ref_id,)).fetchone()
        return int(row[0])


def test_backfill_unknown_signal_id_returns_error(db_path: Path) -> None:
    rc = _backfill_one(db_path, "999999", model="any", force=False)
    assert rc != 0


def test_backfill_no_extractor_for_source_returns_error(db_path: Path) -> None:
    sid = _seed_signal(db_path, "x-001", source="never_registered")
    rc = _backfill_one(db_path, str(sid), model="any", force=False)
    assert rc != 0


def test_backfill_runs_registered_extractor_and_updates_facts(db_path: Path) -> None:
    """Register a fake source whose extractor returns a deterministic dict;
    verify the parent row's extracted_facts reflects it.
    """

    @Tier2ExtractorRegistry.register("test_backfill_alpha")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {"type": "synthetic_event", "fields": {"id": signal["ref_id"]}}

    try:
        sid = _seed_signal(db_path, "alpha-001", source="test_backfill_alpha")
        rc = _backfill_one(db_path, str(sid), model="any", force=False)
        assert rc == 0

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT extracted_facts FROM signals WHERE id = ?", (sid,)
            ).fetchone()
        facts = json.loads(row[0])
        assert facts["type"] == "synthetic_event"
        assert facts["fields"]["id"] == "alpha-001"
    finally:
        Tier2ExtractorRegistry._registry.pop("test_backfill_alpha", None)


def test_backfill_skips_existing_facts_without_force(db_path: Path) -> None:
    """Without --force the backfill leaves an already-populated row alone."""

    @Tier2ExtractorRegistry.register("test_backfill_beta")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {"type": "new_value", "fields": {}}

    try:
        sid = _seed_signal(db_path, "beta-001", source="test_backfill_beta")
        # Seed pre-existing facts directly.
        with open_db(db_path) as conn, conn:
            conn.execute(
                "UPDATE signals SET extracted_facts = ? WHERE id = ?",
                (json.dumps({"type": "old_value", "fields": {}}), sid),
            )

        rc = _backfill_one(db_path, str(sid), model="any", force=False)
        assert rc == 0

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT extracted_facts FROM signals WHERE id = ?", (sid,)
            ).fetchone()
        facts = json.loads(row[0])
        assert facts["type"] == "old_value"  # unchanged
    finally:
        Tier2ExtractorRegistry._registry.pop("test_backfill_beta", None)


def test_backfill_force_overwrites_existing_facts(db_path: Path) -> None:
    @Tier2ExtractorRegistry.register("test_backfill_gamma")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {"type": "new_value", "fields": {}}

    try:
        sid = _seed_signal(db_path, "gamma-001", source="test_backfill_gamma")
        with open_db(db_path) as conn, conn:
            conn.execute(
                "UPDATE signals SET extracted_facts = ? WHERE id = ?",
                (json.dumps({"type": "old_value", "fields": {}}), sid),
            )

        rc = _backfill_one(db_path, str(sid), model="any", force=True)
        assert rc == 0

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT extracted_facts FROM signals WHERE id = ?", (sid,)
            ).fetchone()
        facts = json.loads(row[0])
        assert facts["type"] == "new_value"
    finally:
        Tier2ExtractorRegistry._registry.pop("test_backfill_gamma", None)


def test_backfill_fans_out_digest_children(db_path: Path) -> None:
    """A digest extractor result triggers the same fan-out the live poller
    does — same synthetic ref_id format, same parent_ref_id wiring.
    """

    @Tier2ExtractorRegistry.register("test_backfill_digest")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {
            "type": "digest",
            "is_digest_parent": True,
            "digest_items": [
                {"type": "x", "fields": {"k": 1}, "is_digest_item": True},
                {"type": "x", "fields": {"k": 2}, "is_digest_item": True},
            ],
        }

    try:
        sid = _seed_signal(db_path, "digest-001", source="test_backfill_digest")
        rc = _backfill_one(db_path, str(sid), model="any", force=False)
        assert rc == 0

        with open_db(db_path) as conn:
            children = conn.execute(
                "SELECT ref_id FROM signals WHERE parent_ref_id = 'digest-001' ORDER BY ref_id"
            ).fetchall()

        assert [r[0] for r in children] == ["digest-001:0", "digest-001:1"]
    finally:
        Tier2ExtractorRegistry._registry.pop("test_backfill_digest", None)


def test_backfill_returns_zero_when_extractor_returns_none(db_path: Path) -> None:
    """Marketing-class signals: extractor returns None, parent stays NULL,
    CLI exits cleanly (no error rc).
    """

    @Tier2ExtractorRegistry.register("test_backfill_none")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return None

    try:
        sid = _seed_signal(db_path, "none-001", source="test_backfill_none")
        rc = _backfill_one(db_path, str(sid), model="any", force=False)
        assert rc == 0

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT extracted_facts FROM signals WHERE id = ?", (sid,)
            ).fetchone()
        assert row[0] is None
    finally:
        Tier2ExtractorRegistry._registry.pop("test_backfill_none", None)
