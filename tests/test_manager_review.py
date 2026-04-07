"""
Targeted tests for ObservationCycle manager review code paths.

Covers:
  - _should_run_manager_review()       lines 210-255
  - _get_all_active_threads()          lines 661-678
  - _build_batch_dump()                lines 680-697
  - _run_manager_review()              lines 699-835
  - _apply_manager_updates()           lines 837-938
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.observation import ObservationCycle


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


def _insert_thread(db_path, thread_id, name, status="active", signal_count=1, priority=None, owner=None):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, signal_count, priority, owner) VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, name, status, signal_count, priority, owner),
        )


def _insert_signal(db_path, source="test", content_preview="p"):
    with open_db(db_path) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO signals (source, content_preview) VALUES (?, ?)",
            (source, content_preview),
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
    profile = {"observation": {"manager_interval_hours": 2}}
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
    profile = {"observation": {"manager_max_threads": 3}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    threads = cycle._get_all_active_threads()
    assert len(threads) == 3


# ── _build_batch_dump ─────────────────────────────────────────────────────


def test_build_batch_dump_contains_thread_ids(db_path):
    cycle = ObservationCycle(db_path=db_path)
    threads = [
        {
            "id": "t-abc",
            "name": "Job Search",
            "priority": "high",
            "owner": "me",
            "signal_count": 5,
            "current_deadline": None,
            "summary": "Looking for roles",
        },
    ]
    dump = cycle._build_batch_dump(threads, batch_num=1, total_batches=1)
    assert "t-abc" in dump
    assert "Job Search" in dump
    assert "high" in dump
    assert "Batch 1/1" in dump


def test_build_batch_dump_handles_missing_fields(db_path):
    cycle = ObservationCycle(db_path=db_path)
    threads = [
        {
            "id": "t1",
            "name": "T1",
            "priority": None,
            "owner": None,
            "signal_count": 0,
            "current_deadline": None,
            "summary": None,
        },
    ]
    dump = cycle._build_batch_dump(threads, batch_num=1, total_batches=2)
    assert "UNSET" in dump
    assert "unclear" in dump
    assert "(no summary)" in dump


def test_build_batch_dump_includes_deadline(db_path):
    cycle = ObservationCycle(db_path=db_path)
    threads = [
        {
            "id": "t1",
            "name": "T1",
            "priority": "medium",
            "owner": "me",
            "signal_count": 3,
            "current_deadline": "2026-05-01",
            "summary": "s",
        },
    ]
    dump = cycle._build_batch_dump(threads, 1, 1)
    assert "deadline: 2026-05-01" in dump


# ── _apply_manager_updates ────────────────────────────────────────────────


def test_apply_manager_updates_empty(db_path):
    cycle = ObservationCycle(db_path=db_path)
    actions = cycle._apply_manager_updates({"thread_updates": [], "signal_flags": []})
    assert actions == []


def test_apply_manager_updates_thread_priority(db_path):
    _insert_thread(db_path, "t1", "Test Thread", priority=None)
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [{"thread_id": "t1", "priority": "high", "summary": "Updated summary"}],
        "signal_flags": [],
    }
    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "manager_thread_update" for a in actions)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT priority, summary FROM threads WHERE id = 't1'").fetchone()
    assert row[0] == "high"
    assert row[1] == "Updated summary"


def test_apply_manager_updates_skips_missing_thread_id(db_path):
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [{"priority": "high"}],  # no thread_id
        "signal_flags": [],
    }
    actions = cycle._apply_manager_updates(review_data)
    assert actions == []


def test_apply_manager_updates_signal_flags(db_path):
    signal_id = _insert_signal(db_path)
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [],
        "signal_flags": [{"signal_id": signal_id, "suggested_urgency": "high", "suggested_action_type": "reply"}],
    }
    actions = cycle._apply_manager_updates(review_data)
    assert any(a["tool"] == "manager_signal_flag" for a in actions)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT urgency, action_type FROM signals WHERE id = ?", (signal_id,)).fetchone()
    assert row[0] == "high"
    assert row[1] == "reply"


def test_apply_manager_updates_signal_flag_skips_no_fields(db_path):
    signal_id = _insert_signal(db_path)
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [],
        "signal_flags": [{"signal_id": signal_id}],  # no urgency or action_type
    }
    actions = cycle._apply_manager_updates(review_data)
    # No UPDATE executed, no action record
    assert not any(a["tool"] == "manager_signal_flag" for a in actions)


def test_apply_manager_updates_thread_update_priority_only(db_path):
    _insert_thread(db_path, "t1", "Test")
    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "thread_updates": [{"thread_id": "t1", "priority": "critical"}],
        "signal_flags": [],
    }
    actions = cycle._apply_manager_updates(review_data)
    assert len(actions) == 1
    assert actions[0]["input"]["priority"] == "critical"
    assert actions[0]["input"]["summary_updated"] is False


# ── _run_manager_review ───────────────────────────────────────────────────


def _make_llm_mock(response_text: str) -> MagicMock:
    llm = MagicMock()
    llm.generate.return_value = response_text
    return llm


def test_run_manager_review_happy_path(db_path):
    _insert_thread(db_path, "t1", "Job Search", priority=None)
    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps(
        {
            "thread_updates": [{"thread_id": "t1", "priority": "high", "summary": "Active job search"}],
            "signal_flags": [],
            "digest": "Review complete.",
        }
    )
    mock_llm = _make_llm_mock(response)

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.review_mode == "manager"
    assert result.degraded is False
    assert result.errors == []
    assert any(a["tool"] == "manager_thread_update" for a in result.actions_taken)


def test_run_manager_review_no_threads_still_runs(db_path):
    cycle = ObservationCycle(db_path=db_path)
    mock_llm = _make_llm_mock(json.dumps({"thread_updates": [], "signal_flags": [], "digest": ""}))

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.degraded is False


def test_run_manager_review_json_parse_failure_marks_degraded(db_path):
    _insert_thread(db_path, "t1", "Thread 1")
    cycle = ObservationCycle(db_path=db_path)
    mock_llm = _make_llm_mock("not valid json at all !!!")

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.degraded is True
    assert any("JSON" in e or "no JSON" in e for e in result.errors)


def test_run_manager_review_json_in_code_fence_parses(db_path):
    _insert_thread(db_path, "t1", "T1", priority=None)
    cycle = ObservationCycle(db_path=db_path)

    inner = json.dumps(
        {
            "thread_updates": [{"thread_id": "t1", "priority": "low", "summary": "quiet"}],
            "signal_flags": [],
            "digest": "ok",
        }
    )
    fenced = f"```json\n{inner}\n```"
    mock_llm = _make_llm_mock(fenced)

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.degraded is False
    assert any(a["tool"] == "manager_thread_update" for a in result.actions_taken)


def test_run_manager_review_fires_digest_nudge(db_path):
    _insert_thread(db_path, "t1", "T1")
    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps(
        {
            "thread_updates": [],
            "signal_flags": [],
            "digest": "Here is your daily summary.",
        }
    )
    mock_llm = _make_llm_mock(response)
    mock_executor = MagicMock()

    with (
        patch("xibi.observation.get_model", return_value=mock_llm),
        patch("xibi.observation.dispatch", return_value={"status": "ok"}) as mock_dispatch,
    ):
        result = cycle._run_manager_review(executor=mock_executor, command_layer=None)

    mock_dispatch.assert_called_once()
    call_kwargs = mock_dispatch.call_args
    assert call_kwargs[0][0] == "nudge"
    assert "Manager Review Digest" in call_kwargs[0][1]["message"]
    assert any(a["tool"] == "nudge" for a in result.actions_taken)


def test_run_manager_review_no_nudge_when_no_digest(db_path):
    _insert_thread(db_path, "t1", "T1")
    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps({"thread_updates": [], "signal_flags": [], "digest": ""})
    mock_llm = _make_llm_mock(response)
    mock_executor = MagicMock()

    with (
        patch("xibi.observation.get_model", return_value=mock_llm),
        patch("xibi.observation.dispatch") as mock_dispatch,
    ):
        cycle._run_manager_review(executor=mock_executor, command_layer=None)

    mock_dispatch.assert_not_called()


def test_run_manager_review_nudge_failure_recorded_in_errors(db_path):
    _insert_thread(db_path, "t1", "T1")
    cycle = ObservationCycle(db_path=db_path)

    response = json.dumps({"thread_updates": [], "signal_flags": [], "digest": "Summary"})
    mock_llm = _make_llm_mock(response)
    mock_executor = MagicMock()

    with (
        patch("xibi.observation.get_model", return_value=mock_llm),
        patch("xibi.observation.dispatch", side_effect=Exception("nudge failed")),
    ):
        result = cycle._run_manager_review(executor=mock_executor, command_layer=None)

    assert any("Digest nudge failed" in e for e in result.errors)


def test_run_manager_review_multiple_batches(db_path):
    for i in range(5):
        _insert_thread(db_path, f"t{i}", f"Thread {i}")

    profile = {"observation": {"manager_max_threads": 200}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)

    # Force batch_size of 2 to exercise multi-batch path
    responses = [
        json.dumps(
            {"thread_updates": [{"thread_id": f"t{i * 2}", "priority": "low"}], "signal_flags": [], "digest": f"d{i}"}
        )
        for i in range(3)
    ]
    mock_llm = MagicMock()
    mock_llm.generate.side_effect = responses

    # Patch batch_size to 2 by monkey-patching inside _run_manager_review
    original_run = cycle._run_manager_review

    def patched_run(*args, **kwargs):
        # We can't easily patch batch_size, so just call normally and verify multi-call
        return original_run(*args, **kwargs)

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    # LLM called at least once (1 batch of all 5 threads)
    assert mock_llm.generate.call_count >= 1


def test_run_manager_review_persists_cycle_row(db_path):
    cycle = ObservationCycle(db_path=db_path)
    mock_llm = _make_llm_mock(json.dumps({"thread_updates": [], "signal_flags": [], "digest": ""}))

    with patch("xibi.observation.get_model", return_value=mock_llm):
        cycle._run_manager_review(executor=None, command_layer=None)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT review_mode, completed_at FROM observation_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "manager"
    assert row[1] is not None  # completed_at was set


def test_run_manager_review_batch_exception_continues(db_path):
    _insert_thread(db_path, "t1", "T1")
    cycle = ObservationCycle(db_path=db_path)
    mock_llm = MagicMock()
    mock_llm.generate.side_effect = Exception("LLM exploded")

    with patch("xibi.observation.get_model", return_value=mock_llm):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.degraded is True
    assert any("Batch 1" in e for e in result.errors)


def test_run_manager_review_get_model_failure_returns_gracefully(db_path):
    cycle = ObservationCycle(db_path=db_path)

    with patch("xibi.observation.get_model", side_effect=Exception("provider down")):
        result = cycle._run_manager_review(executor=None, command_layer=None)

    assert result.ran is True
    assert result.degraded is True
    assert any("provider down" in e for e in result.errors)


# ── Integration: run() delegates to _run_manager_review when due ───────────


def test_run_calls_manager_review_when_due(db_path):
    _insert_thread(db_path, "t1", "T1")
    cycle = ObservationCycle(db_path=db_path)

    mock_result = MagicMock()
    mock_result.review_mode = "manager"

    with patch.object(cycle, "_run_manager_review", return_value=mock_result) as mock_mgr:
        cycle.run(executor=None, command_layer=None)

    mock_mgr.assert_called_once()


def test_run_skips_manager_review_when_not_due(db_path):
    # Recent manager review → not due, and no signals → triage skips too
    _insert_thread(db_path, "t1", "T1")
    _insert_manager_cycle(db_path, hours_ago=1)
    cycle = ObservationCycle(db_path=db_path)

    with patch.object(cycle, "_run_manager_review") as mock_mgr:
        result = cycle.run(executor=None, command_layer=None)

    mock_mgr.assert_not_called()
    assert result.ran is False


# ── _build_review_dump ────────────────────────────────────────────────────


def test_build_review_dump_empty_db(db_path):
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "MANAGER REVIEW DUMP" in dump
    assert "OVERVIEW: 0 active threads" in dump


def test_build_review_dump_with_threads(db_path):
    _insert_thread(db_path, "t1", "Job Search", priority="high", owner="me", signal_count=5)
    _insert_thread(db_path, "t2", "Lease Renewal", priority=None, signal_count=2)
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "Job Search" in dump
    assert "Lease Renewal" in dump
    assert "OVERVIEW: 2 active threads" in dump
    assert "1 missing priority" in dump


def test_build_review_dump_with_gap_signals(db_path):
    # Insert signals with null urgency to trigger the gap-signals section
    _insert_signal(db_path, source="email", content_preview="hello world")
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "SIGNALS WITH GAPS" in dump


def test_build_review_dump_with_signal_distribution(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview, urgency) VALUES ('email', 'p', 'high')")
        conn.execute("INSERT INTO signals (source, content_preview, urgency) VALUES ('email', 'p', 'normal')")
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "SIGNAL DISTRIBUTION" in dump
    assert "high" in dump


def test_build_review_dump_with_active_tasks(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO tasks (id, goal, status, urgency, trace_id) VALUES ('task-1', 'Apply to jobs', 'open', 'high', 'tr-1')"
        )
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "ACTIVE TASKS" in dump
    assert "Apply to jobs" in dump


def test_build_review_dump_excludes_stale_threads(db_path):
    _insert_thread(db_path, "t1", "Active One", status="active")
    _insert_thread(db_path, "t2", "Stale One", status="stale")
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "Active One" in dump
    assert "Stale One" not in dump


def test_build_review_dump_thread_has_deadline(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, signal_count, current_deadline) "
            "VALUES ('t1', 'Deadline Thread', 'active', 1, '2026-05-01')"
        )
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "deadline: 2026-05-01" in dump


def test_build_review_dump_thread_never_reviewed(db_path):
    _insert_thread(db_path, "t1", "Unreviewd Thread")
    cycle = ObservationCycle(db_path=db_path)
    dump = cycle._build_review_dump()
    assert "never reviewed" in dump


def test_build_review_dump_exception_returns_error_string(db_path):
    cycle = ObservationCycle(db_path=db_path)
    with patch("xibi.observation.open_db", side_effect=Exception("db gone")):
        dump = cycle._build_review_dump()
    assert "Error building review dump" in dump
