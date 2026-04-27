"""Tests for accounts.backfill_contacts_origin (step-110, C2)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db
from xibi.skills.accounts.handler import backfill_contacts_origin


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    return p


def _seed_contact_no_origin(db_path, contact_id, email):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, signal_count) VALUES (?, ?, ?, 0)",
            (contact_id, email, email),
        )


def _log_signal(db_path, ref_id, entity_text, account):
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="t",
            entity_text=entity_text,
            entity_type="person",
            content_preview="c",
            ref_id=ref_id,
            ref_source="email",
            received_via_account=account,
        )


def test_idempotent_noop(db_path):
    out = backfill_contacts_origin({"_db_path": str(db_path)})
    assert out["status"] == "success"
    assert out["updated"] == 0


def test_oldest_wins(db_path):
    _seed_contact_no_origin(db_path, "c-1", "sarah@example.com")
    # Two signals: oldest one is afya, newer is personal.
    _log_signal(db_path, "old-sig", "sarah@example.com", "afya")
    _log_signal(db_path, "new-sig", "sarah@example.com", "personal")

    out = backfill_contacts_origin({"_db_path": str(db_path)})
    assert out["updated"] == 1

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT account_origin, seen_via_accounts FROM contacts WHERE id = ?", ("c-1",)).fetchone()
    assert row[0] == "afya"
    assert json.loads(row[1]) == ["afya"]


def test_skips_when_no_signal_history(db_path):
    _seed_contact_no_origin(db_path, "c-2", "ghost@example.com")
    out = backfill_contacts_origin({"_db_path": str(db_path)})
    assert out["updated"] == 0
    assert out["skipped"] == 1


def test_idempotent_after_run(db_path):
    _seed_contact_no_origin(db_path, "c-3", "x@example.com")
    _log_signal(db_path, "s1", "x@example.com", "afya")
    out1 = backfill_contacts_origin({"_db_path": str(db_path)})
    assert out1["updated"] == 1
    out2 = backfill_contacts_origin({"_db_path": str(db_path)})
    assert out2["updated"] == 0
