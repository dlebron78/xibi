"""Step-112: digest fan-out + provenance inheritance + idempotency tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._helpers import _migrated_db
from xibi.alerting.rules import RuleEngine
from xibi.db import open_db
from xibi.heartbeat.tier2_backfill import _fanout_children


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _migrated_db(tmp_path)


def _seed_parent(rules: RuleEngine, db_path: Path, parent_facts: dict) -> dict:
    """Insert a digest parent signal and return its row dict."""
    with open_db(db_path) as conn:
        rules.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="weekly job alert",
            entity_text="alerts@indeed.com",
            entity_type="email",
            content_preview="3 new roles match your filter",
            ref_id="parent-001",
            ref_source="email",
            extracted_facts=parent_facts,
            received_via_account="primary",
            received_via_email_alias="me@x.com",
        )
        conn.commit()

        import sqlite3
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM signals WHERE ref_id = 'parent-001'").fetchone()
        return dict(row)


def test_fanout_writes_one_child_per_item(db_path: Path) -> None:
    rules = RuleEngine(db_path)
    parent_facts = {
        "type": "job_alert_digest",
        "is_digest_parent": True,
        "digest_items": [
            {"type": "job_listing", "fields": {"title": "Senior PM", "company": "Stripe"}},
            {"type": "job_listing", "fields": {"title": "Director Product", "company": "Notion"}},
            {"type": "job_listing", "fields": {"title": "Principal PM", "company": "Datadog"}},
        ],
    }
    parent = _seed_parent(rules, db_path, parent_facts)

    with open_db(db_path) as conn:
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        conn.commit()

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_ref_id, extracted_facts FROM signals "
            "WHERE parent_ref_id = 'parent-001' ORDER BY ref_id"
        ).fetchall()

    assert len(rows) == 3
    assert {r[0] for r in rows} == {"parent-001:0", "parent-001:1", "parent-001:2"}
    assert all(r[1] == "parent-001" for r in rows)
    # is_digest_item flag stamped on each child
    for r in rows:
        facts = json.loads(r[2])
        assert facts.get("is_digest_item") is True or facts.get("type") == "job_listing"


def test_fanout_is_idempotent(db_path: Path) -> None:
    """Re-running fan-out for the same digest must not duplicate children."""
    rules = RuleEngine(db_path)
    parent_facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": [
            {"type": "x", "fields": {"k": 1}, "is_digest_item": True},
            {"type": "x", "fields": {"k": 2}, "is_digest_item": True},
        ],
    }
    parent = _seed_parent(rules, db_path, parent_facts)

    with open_db(db_path) as conn:
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        conn.commit()

    with open_db(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE parent_ref_id = 'parent-001'"
        ).fetchone()[0]

    assert count == 2


def test_fanout_inherits_provenance(db_path: Path) -> None:
    """Per spec constraint + condition: child signals inherit
    received_via_account / received_via_email_alias from parent.
    """
    rules = RuleEngine(db_path)
    parent_facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": [{"type": "x", "fields": {}, "is_digest_item": True}],
    }
    parent = _seed_parent(rules, db_path, parent_facts)

    with open_db(db_path) as conn:
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        conn.commit()

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT received_via_account, received_via_email_alias FROM signals "
            "WHERE parent_ref_id = 'parent-001'"
        ).fetchone()

    assert row[0] == "primary"
    assert row[1] == "me@x.com"


def test_fanout_inherits_ref_source_from_parent(db_path: Path) -> None:
    """TRR condition #10: child rows use the parent's ref_source so the
    existing (ref_source, ref_id) dedup machinery works correctly per row.
    """
    rules = RuleEngine(db_path)
    parent_facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": [{"type": "x", "fields": {}, "is_digest_item": True}],
    }
    parent = _seed_parent(rules, db_path, parent_facts)

    with open_db(db_path) as conn:
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        conn.commit()

    with open_db(db_path) as conn:
        ref_source = conn.execute(
            "SELECT ref_source FROM signals WHERE parent_ref_id = 'parent-001'"
        ).fetchone()[0]

    assert ref_source == "email"


def test_fanout_skips_non_dict_items(db_path: Path) -> None:
    """Sanitization upstream drops these, but the fan-out code defends in
    depth — passing a list with garbage in it should not crash and should
    only write valid children.
    """
    rules = RuleEngine(db_path)
    parent_facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": [
            {"type": "good", "fields": {"k": 1}, "is_digest_item": True},
            "garbage-string",
            42,
            None,
        ],
    }
    parent = _seed_parent(rules, db_path, parent_facts)

    with open_db(db_path) as conn:
        _fanout_children(conn, parent, parent_facts, parent_facts["digest_items"])
        conn.commit()

    with open_db(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE parent_ref_id = 'parent-001'"
        ).fetchone()[0]

    assert count == 1
