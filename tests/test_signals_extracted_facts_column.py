"""Step-112: migration 42 + signal-write column round-trip tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests._helpers import _migrated_db
from xibi.alerting.rules import RuleEngine
from xibi.db import open_db
from xibi.db.migrations import SCHEMA_VERSION, SchemaManager


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _migrated_db(tmp_path)


def test_schema_version_is_42() -> None:
    assert SCHEMA_VERSION == 42


def test_migration_42_adds_extracted_facts_column(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    sm = SchemaManager(db)
    sm.migrate()

    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}

    assert "extracted_facts" in cols
    assert "parent_ref_id" in cols


def test_migration_42_creates_parent_ref_id_index(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    sm = SchemaManager(db)
    sm.migrate()

    with sqlite3.connect(db) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(signals)").fetchall()}

    assert "idx_signals_parent_ref_id" in indexes


def test_migration_42_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    sm = SchemaManager(db)
    applied = sm.migrate()
    applied_again = sm.migrate()

    assert SCHEMA_VERSION in applied
    assert applied_again == []


def test_log_signal_round_trips_extracted_facts(db_path: Path) -> None:
    rules = RuleEngine(db_path)
    facts = {
        "type": "flight_booking",
        "fields": {
            "carrier": "Frontier",
            "departure_date": "2026-05-13",
            "departure_airport": "DEN",
            "arrival_airport": "SFO",
            "pnr": "ABC123",
        },
    }
    rules.log_signal(
        source="email",
        topic_hint="Flight confirmation",
        entity_text="reservations@frontier.com",
        entity_type="email",
        content_preview="Your flight DEN-SFO is confirmed",
        ref_id="email-001",
        ref_source="email",
        extracted_facts=facts,
    )

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT extracted_facts FROM signals WHERE ref_id = 'email-001'"
        ).fetchone()

    assert row is not None
    assert json.loads(row[0]) == facts


def test_log_signal_with_conn_round_trips_extracted_facts(db_path: Path) -> None:
    rules = RuleEngine(db_path)
    facts = {"type": "interview", "fields": {"company": "Stripe"}}
    with open_db(db_path) as conn:
        rules.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Interview confirmation",
            entity_text="recruiter@stripe.com",
            entity_type="email",
            content_preview="Interview confirmed",
            ref_id="email-002",
            ref_source="email",
            extracted_facts=facts,
        )
        conn.commit()

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT extracted_facts FROM signals WHERE ref_id = 'email-002'"
        ).fetchone()

    assert row is not None
    assert json.loads(row[0]) == facts


def test_log_signal_default_keeps_extracted_facts_null(db_path: Path) -> None:
    """Backwards compat: existing call sites that don't pass the new kwarg
    must still produce rows where extracted_facts is NULL.
    """
    rules = RuleEngine(db_path)
    rules.log_signal(
        source="email",
        topic_hint="Plain email",
        entity_text="someone@example.com",
        entity_type="email",
        content_preview="Hello",
        ref_id="email-003",
        ref_source="email",
    )

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT extracted_facts, parent_ref_id FROM signals WHERE ref_id = 'email-003'"
        ).fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_log_signal_with_conn_writes_parent_ref_id(db_path: Path) -> None:
    rules = RuleEngine(db_path)
    with open_db(db_path) as conn:
        rules.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Job listing",
            entity_text="alerts@indeed.com",
            entity_type="email",
            content_preview="Senior PM at Stripe",
            ref_id="email-004:0",
            ref_source="email",
            extracted_facts={"type": "job_listing", "fields": {"title": "Senior PM"}},
            parent_ref_id="email-004",
        )
        conn.commit()

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT parent_ref_id FROM signals WHERE ref_id = 'email-004:0'"
        ).fetchone()

    assert row is not None
    assert row[0] == "email-004"
