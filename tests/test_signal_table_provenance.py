"""Tests for migration 40 + signals.received_via_account write paths."""

from __future__ import annotations

import sqlite3

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    return p


def test_migration_applied(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    assert "received_via_account" in cols
    assert "received_via_email_alias" in cols


def test_new_signals_carry_account(db_path):
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Q3 review",
            entity_text="manager@afya.fit",
            entity_type="person",
            content_preview="Re: Q3",
            ref_id="email-1",
            ref_source="email",
            received_via_account="afya",
            received_via_email_alias="lebron@afya.fit",
        )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT received_via_account, received_via_email_alias FROM signals WHERE ref_id = ?",
            ("email-1",),
        ).fetchone()
    assert row == ("afya", "lebron@afya.fit")


def test_old_signals_show_null_until_backfill(db_path):
    """Pre-step-110 signals (written before kwargs were threaded) show NULL."""
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Old signal",
            entity_text="nobody",
            entity_type="person",
            content_preview="something",
            ref_id="email-old",
            ref_source="email",
            # received_via_account omitted — defaults to None
        )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT received_via_account FROM signals WHERE ref_id = ?",
            ("email-old",),
        ).fetchone()
    assert row[0] is None


def test_calendar_signals_have_null_provenance(db_path):
    """Calendar-derived signals always pass NULL for provenance per spec."""
    from xibi.heartbeat.calendar_poller import _log_calendar_signal

    sig = {
        "source": "calendar",
        "ref_id": "evt-1",
        "ref_source": "calendar",
        "topic_hint": "1:1 with manager",
        "timestamp": "2026-04-27T10:00:00",
        "content_preview": "1:1 at 10am",
        "summary": "1:1",
        "urgency": "MEDIUM",
        "entity_type": "person",
        "entity_text": "manager",
        "env": "test",
    }
    _log_calendar_signal(db_path, sig)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT received_via_account, received_via_email_alias FROM signals WHERE ref_id = ?",
            ("evt-1",),
        ).fetchone()
    assert row == (None, None)
