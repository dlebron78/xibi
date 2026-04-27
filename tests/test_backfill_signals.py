"""Tests for accounts.backfill_signals_provenance (step-110, C2)."""

from __future__ import annotations

import sqlite3

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db
from xibi.skills.accounts.handler import backfill_signals_provenance


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    return p


def _seed_unprovenanced_signal(db_path, ref_id):
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="topic",
            entity_text="x",
            entity_type="person",
            content_preview="c",
            ref_id=ref_id,
            ref_source="email",
        )


def test_idempotent_noop_when_nothing_to_do(db_path, monkeypatch):
    # No signals → backfill returns 0 updated.
    out = backfill_signals_provenance({"_db_path": str(db_path)})
    assert out["status"] == "success"
    assert out["updated"] == 0
    assert out["skipped"] == 0


def test_skips_when_himalaya_returns_no_raw(db_path, monkeypatch):
    """When the source email can't be re-fetched, count as skipped, not failed."""
    _seed_unprovenanced_signal(db_path, "email-missing")

    monkeypatch.setattr("xibi.heartbeat.email_body.find_himalaya", lambda: "himalaya")
    monkeypatch.setattr(
        "xibi.heartbeat.email_body.fetch_raw_email",
        lambda *args, **kwargs: (None, "not found"),
    )

    out = backfill_signals_provenance({"_db_path": str(db_path)})
    assert out["status"] == "success"
    assert out["updated"] == 0
    assert out["skipped"] == 1


def test_updates_when_headers_resolvable(db_path, monkeypatch):
    """When fetch returns RFC 5322 with a To header that matches an account."""
    import json as _json

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', 'afya', '', ?, 'active')",
            ("acct-afya", _json.dumps({"email_alias": "lebron@afya.fit"})),
        )

    _seed_unprovenanced_signal(db_path, "email-resolvable")

    raw = "From: someone@example.com\r\nTo: lebron@afya.fit\r\nSubject: hi\r\n\r\nbody\r\n"

    monkeypatch.setattr("xibi.heartbeat.email_body.find_himalaya", lambda: "himalaya")
    monkeypatch.setattr(
        "xibi.heartbeat.email_body.fetch_raw_email",
        lambda *args, **kwargs: (raw, None),
    )

    out = backfill_signals_provenance({"_db_path": str(db_path)})
    assert out["updated"] == 1

    # Re-run is a no-op
    out2 = backfill_signals_provenance({"_db_path": str(db_path)})
    assert out2["updated"] == 0

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT received_via_account, received_via_email_alias FROM signals WHERE ref_id = ?",
            ("email-resolvable",),
        ).fetchone()
    assert row[0] == "afya"
    assert row[1] == "lebron@afya.fit"
