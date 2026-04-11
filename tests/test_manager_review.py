"""
Targeted tests for ObservationCycle manager review code paths.

Covers:
  - _should_run_manager_review()
  - _get_all_active_threads()
  - _run_manager_review()
  - _apply_manager_updates()
  - Enrichments: summaries, trust, contacts, pinned topics, reclassification.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.observation import ObservationCycle


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


def _insert_thread(db_path, thread_id, name, status="active", signal_count=1, priority=None, owner=None, summary=None):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, signal_count, priority, owner, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, name, status, signal_count, priority, owner, summary),
        )


def _insert_signal(db_path, source="test", content_preview="p", summary=None, sender_trust=None, sender_contact_id=None, urgency=None, topic_hint=None):
    with open_db(db_path) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO signals (source, content_preview, summary, sender_trust, sender_contact_id, urgency, topic_hint, ref_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, content_preview, summary, sender_trust, sender_contact_id, urgency, topic_hint, f"ref-{content_preview}"),
        )
        return cursor.lastrowid


def _insert_manager_cycle(db_path, hours_ago=1):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles "
            "(started_at, completed_at, last_signal_id, review_mode) "
            "VALUES (datetime('now', ?), datetime('now', ?), 0, 'manager')",
            (f"-{hours_ago + 1} hours", f"-{hours_ago} hours"),
        )


# ── _should_run_manager_review ─────────────────────────────────────────────


def test_should_run_manager_review_no_prior_with_threads(db_path):
    _insert_thread(db_path, "t1", "Thread 1")
    cycle = ObservationCycle(db_path=db_path)
    should, reason = cycle._should_run_manager_review()
    assert should is True
    assert "manager_initial" in reason


def test_should_run_manager_review_no_prior_no_threads(db_path):
    cycle = ObservationCycle(db_path=db_path)
    should, reason = cycle._should_run_manager_review()
    assert should is False
    assert "manager_skip" in reason


def test_should_run_manager_review_recent_cycle_skips(db_path):
    _insert_thread(db_path, "t1", "Thread 1")
    # Manager review ran 1 hour ago; default interval is 8 hours
    _insert_manager_cycle(db_path, hours_ago=1)
    cycle = ObservationCycle(db_path=db_path)
    should, reason = cycle._should_run_manager_review()
    assert should is False
    assert "manager_interval" in reason


def test_should_run_manager_review_elapsed_interval_triggers(db_path):
    _insert_thread(db_path, "t1", "Thread 1")
    # Manager review ran 10 hours ago; default interval is 8 hours
    _insert_manager_cycle(db_path, hours_ago=10)
    cycle = ObservationCycle(db_path=db_path)
    should, reason = cycle._should_run_manager_review()
    assert should is True
    assert "manager_due" in reason


def test_should_run_manager_review_custom_interval(db_path):
    _insert_thread(db_path, "t1", "Thread 1")
    _insert_manager_cycle(db_path, hours_ago=3)
    # Custom interval = 2 hours → 3 hours elapsed → should run
    profile = {"observation": {"manager_review": {"interval_hours": 2}}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    should, reason = cycle._should_run_manager_review()
    assert should is True


# ── _get_all_active_threads ────────────────────────────────────────────────


def test_get_all_active_threads_empty(db_path):
    cycle = ObservationCycle(db_path=db_path)
    threads = cycle._get_all_active_threads()
    assert threads == []


def test_get_all_active_threads_excludes_stale(db_path):
    _insert_thread(db_path, "t1", "Active", status="active")
    _insert_thread(db_path, "t2", "Stale", status="stale")
    cycle = ObservationCycle(db_path=db_path)
    threads = cycle._get_all_active_threads()
    assert len(threads) == 1
    assert threads[0]["id"] == "t1"


def test_get_all_active_threads_priority_order(db_path):
    _insert_thread(db_path, "tlo", "Low", priority="low", signal_count=10)
    _insert_thread(db_path, "thi", "High", priority="high", signal_count=1)
    _insert_thread(db_path, "tcr", "Critical", priority="critical", signal_count=1)
    _insert_thread(db_path, "tme", "Medium", priority="medium", signal_count=5)
    cycle = ObservationCycle(db_path=db_path)
    threads = cycle._get_all_active_threads()
    priorities = [t["priority"] for t in threads]
    assert priorities == ["critical", "high", "medium", "low"]


def test_get_all_active_threads_signal_count_tiebreak(db_path):
    _insert_thread(db_path, "t1", "Less", priority="high", signal_count=2)
    _insert_thread(db_path, "t2", "More", priority="high", signal_count=10)
    cycle = ObservationCycle(db_path=db_path)
    threads = cycle._get_all_active_threads()
    assert threads[0]["id"] == "t2"


def test_get_all_active_threads_max_threads_respected(db_path):
    for i in range(5):
        _insert_thread(db_path, f"t{i}", f"Thread {i}")
    profile = {"observation": {"manager_review": {"max_threads": 3}}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    threads = cycle._get_all_active_threads()
    assert len(threads) == 3


# ── _apply_manager_updates ────────────────────────────────────────────────


def test_apply_manager_updates_empty(db_path):
    cycle = ObservationCycle(db_path=db_path)
    actions = cycle._apply_manager_updates({"thread_updates": [], "signal_flags": []})
    assert actions == []


def test_apply_manager_updates_thread_all_fields(db_path):
    _insert_thread(db_path, "t1", "Test Thread", priority=None)
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [
            {
                "thread_id": "t1",
                "priority": "high",
                "summary": "Updated summary",
                "owner": "me",
                "deadline": "2026-12-25"
            }
        ],
        "signal_flags": [],
    }
    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "manager_thread_update" for a in actions)

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT priority, summary, owner, current_deadline FROM threads WHERE id = 't1'").fetchone()
    assert row["priority"] == "high"
    assert row["summary"] == "Updated summary"
    assert row["owner"] == "me"
    assert row["current_deadline"] == "2026-12-25"


def test_apply_manager_updates_topic_pins(db_path):
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "topic_pins": [
            {"topic": "Hot Topic", "action": "pin", "reason": "important"},
            {"topic": "Cold Topic", "action": "unpin", "reason": "over"}
        ]
    }
    # Pre-insert Cold Topic to test unpin
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO pinned_topics (topic) VALUES ('cold topic')")

    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "manager_topic_pin" for a in actions)

    with open_db(db_path) as conn:
        pinned = [r[0] for r in conn.execute("SELECT topic FROM pinned_topics").fetchall()]
    assert "hot topic" in pinned
    assert "cold topic" not in pinned


def test_apply_manager_updates_contact_enrichment(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO contacts (id, display_name) VALUES ('c1', 'Alice')")

    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "contact_updates": [
            {"contact_id": "c1", "relationship": "client", "organization": "Acme Corp"}
        ]
    }
    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "manager_contact_enrichment" for a in actions)

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT relationship, organization FROM contacts WHERE id = 'c1'").fetchone()
    assert row["relationship"] == "client"
    assert row["organization"] == "Acme Corp"


def test_apply_manager_updates_reclassify_urgent(db_path):
    signal_id = _insert_signal(db_path, urgency="low", content_preview="urgent-signal")
    with open_db(db_path) as conn, conn:
        # Get ref_id
        ref_id = conn.execute("SELECT ref_id FROM signals WHERE id = ?", (signal_id,)).fetchone()[0]
        conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES (?, 'DIGEST')", (ref_id,))

    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "signal_flags": [
            {
                "signal_id": signal_id,
                "suggested_urgency": "high",
                "reclassify_urgent": True,
                "reason": "Very important"
            }
        ]
    }
    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "late_nudge_queued" for a in actions)

    with open_db(db_path) as conn:
        urgency = conn.execute("SELECT urgency FROM signals WHERE id = ?", (signal_id,)).fetchone()[0]
        verdict = conn.execute("SELECT verdict FROM triage_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert urgency == "high"
    assert verdict == "URGENT"


# ── _build_review_dump ────────────────────────────────────────────────────


def test_build_review_dump_enriched(db_path):
    # Setup: thread, signal with summary and trust, contact, pinned topic
    _insert_thread(db_path, "t1", "Thread 1", priority="medium", summary="Existing summary")
    _insert_signal(db_path, summary="This is a summary", sender_trust="ESTABLISHED", sender_contact_id="c1", topic_hint="Work")
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO contacts (id, display_name, organization, relationship, outbound_count) VALUES ('c1', 'Bob', 'Acme', 'colleague', 5)")
        conn.execute("INSERT INTO pinned_topics (topic) VALUES ('urgent project')")

    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()

    assert "Existing summary" in dump
    assert "This is a summary" in dump
    assert "ESTABLISHED" in dump
    assert "Bob" in dump
    assert "Acme" in dump
    assert "colleague" in dump
    assert "you've emailed them 5x" in dump
    assert "URGENT PROJECT" in dump.upper()
    assert "RECENT SIGNALS" in dump


# ── _run_manager_review ───────────────────────────────────────────────────


def _make_llm_mock(response_text: str) -> MagicMock:
    llm = MagicMock()
    llm.generate.return_value = response_text
    return llm


def test_run_manager_review_late_nudge(db_path):
    signal_id = _insert_signal(db_path, topic_hint="Hot", summary="Summary", content_preview="hot-signal")
    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps({
        "thread_updates": [],
        "signal_flags": [{
            "signal_id": signal_id,
            "suggested_urgency": "high",
            "reclassify_urgent": True,
            "reason": "escalation"
        }],
        "digest": "Digest text"
    })
    mock_llm = _make_llm_mock(response)
    mock_executor = MagicMock()

    with (
        patch("xibi.observation.get_model", return_value=mock_llm),
        patch("xibi.observation.dispatch", return_value={"status": "ok"}) as mock_dispatch,
    ):
        result = cycle._run_manager_review(executor=mock_executor, command_layer=None)

    assert result.ran is True
    # Two nudges: one for digest, one for late alerts
    assert mock_dispatch.call_count == 2

    # Verify late nudge content
    late_nudge_call = [c for c in mock_dispatch.call_args_list if "Late Alerts" in c[0][1]["message"]][0]
    assert "Hot" in late_nudge_call[0][1]["message"]
    assert "escalation" in late_nudge_call[0][1]["message"]
    assert late_nudge_call[0][1]["category"] == "urgent"


def test_run_manager_review_all_new_fields(db_path):
    _insert_thread(db_path, "t1", "T1")
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO contacts (id, display_name) VALUES ('c1', 'Alice')")

    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps({
        "thread_updates": [{"thread_id": "t1", "priority": "high", "owner": "me"}],
        "signal_flags": [],
        "topic_pins": [{"topic": "new topic", "action": "pin", "reason": "rising"}],
        "contact_updates": [{"contact_id": "c1", "relationship": "vendor"}],
        "digest": "ok"
    })
    mock_llm = _make_llm_mock(response)

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert any(a["tool"] == "manager_thread_update" for a in result.actions_taken)
    assert any(a["tool"] == "manager_topic_pin" for a in result.actions_taken)
    assert any(a["tool"] == "manager_contact_enrichment" for a in result.actions_taken)
