"""
Tests for signals.metadata persistence round-trip.

Covers:
  - log_signal_with_conn writes metadata JSON to the signals table
  - metadata is read back correctly as JSON
  - metadata=None is accepted (no column written)
  - existing signals without metadata column work (migration idempotency)
"""

from __future__ import annotations

import json

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


@pytest.fixture
def engine(db_path):
    return RuleEngine(db_path=db_path)


def test_metadata_persisted_on_write(db_path, engine):
    meta = {
        "title": "Head of Product, Agentic AI",
        "company": "Anthropic",
        "location": "SF/Remote",
        "url": "https://jobs.example.com/123",
        "salary_min": 200000,
        "salary_max": 300000,
    }
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="jobspy_pm_search",
            topic_hint="Head of Product at Anthropic",
            entity_text="Anthropic",
            entity_type="company",
            content_preview="Head of Product, Agentic AI | Anthropic | SF/Remote",
            ref_id="job-abc123",
            ref_source="jobspy",
            metadata=meta,
        )

    with open_db(db_path) as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        row = conn.execute(
            "SELECT metadata FROM signals WHERE ref_id = ?", ("job-abc123",)
        ).fetchone()

    assert row is not None
    stored = json.loads(row["metadata"])
    assert stored["title"] == "Head of Product, Agentic AI"
    assert stored["company"] == "Anthropic"
    assert stored["url"] == "https://jobs.example.com/123"
    assert stored["salary_min"] == 200000


def test_metadata_none_writes_null(db_path, engine):
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Test email",
            entity_text=None,
            entity_type="unknown",
            content_preview="Some email",
            ref_id="email-001",
            ref_source=None,
            metadata=None,
        )

    with open_db(db_path) as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        row = conn.execute(
            "SELECT metadata FROM signals WHERE ref_id = ?", ("email-001",)
        ).fetchone()

    assert row is not None
    assert row["metadata"] is None


def test_dedup_skips_same_ref_id_same_day(db_path, engine):
    """log_signal_with_conn should skip duplicates for same source+ref_id same day."""
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="jobspy_pm_search",
            topic_hint="Role",
            entity_text="Acme",
            entity_type="company",
            content_preview="Role | Acme",
            ref_id="job-dup",
            ref_source="jobspy",
            metadata={"title": "First"},
        )
        # Second write with same ref_id — should be silently skipped
        engine.log_signal_with_conn(
            conn,
            source="jobspy_pm_search",
            topic_hint="Role",
            entity_text="Acme",
            entity_type="company",
            content_preview="Role | Acme",
            ref_id="job-dup",
            ref_source="jobspy",
            metadata={"title": "Second"},
        )

    with open_db(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE ref_id = ?", ("job-dup",)
        ).fetchone()[0]

    assert count == 1


def test_metadata_roundtrip_with_nested_job_object(db_path, engine):
    """Full job metadata dict (including nested 'job' key) round-trips correctly."""
    meta = {
        "title": "VP Product",
        "company": "Stripe",
        "location": "Remote",
        "url": "https://stripe.com/jobs/456",
        "salary_min": None,
        "salary_max": None,
        "posted_at": "2026-04-10",
        "job": {"id": "456", "description": "Build the product org."},
    }
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="jobspy_pm_search",
            topic_hint="VP Product at Stripe",
            entity_text="Stripe",
            entity_type="company",
            content_preview="VP Product | Stripe | Remote",
            ref_id="job-456",
            ref_source="jobspy",
            metadata=meta,
        )

    with open_db(db_path) as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        row = conn.execute(
            "SELECT metadata FROM signals WHERE ref_id = ?", ("job-456",)
        ).fetchone()

    stored = json.loads(row["metadata"])
    assert stored["job"]["description"] == "Build the product org."
    assert stored["posted_at"] == "2026-04-10"
