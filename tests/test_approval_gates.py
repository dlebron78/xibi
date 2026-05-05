"""step-123: approval gates for outbound subagent actions.

Covers:
- enforce_trust() with the new global approval_required_tools list
- the Telegram notify-on-park path in checklist.py
- the l2_action: button handler (approve, reject, double-tap idempotency,
  unauthorized chat, decode failure)
- removal of the manager-review action_approvals authority
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.channels.telegram import TelegramAdapter
from xibi.db.migrations import SchemaManager
from xibi.router import Config
from xibi.skills.registry import SkillRegistry
from xibi.subagent import approval_config
from xibi.subagent.models import PendingL2Action, SubagentRun
from xibi.subagent.trust import enforce_trust

# ─────────────────────────────────────────────────────────────────────────────
# enforce_trust — pure function, no DB
# ─────────────────────────────────────────────────────────────────────────────


def test_enforce_trust_parks_listed_tool():
    out = {"actions": [{"tool": "send_email", "args": {"to": "a@b.com"}}]}
    clean, parked = enforce_trust(out, "run1", "step1", ["send_email"])
    assert len(parked) == 1
    assert parked[0].tool == "send_email"
    assert parked[0].args == {"to": "a@b.com"}
    assert parked[0].status == "PENDING"
    assert clean["parked_actions"] == [parked[0].id]


def test_enforce_trust_passes_unlisted_tool():
    out = {"actions": [{"tool": "read_file", "args": {"path": "/tmp/x"}}]}
    clean, parked = enforce_trust(out, "run1", "step1", ["send_email"])
    assert parked == []
    assert "parked_actions" not in clean
    assert clean["actions"] == out["actions"]


def test_enforce_trust_empty_list_parks_nothing():
    out = {"actions": [{"tool": "send_email", "args": {}}]}
    clean, parked = enforce_trust(out, "run1", "step1", [])
    assert parked == []
    assert "parked_actions" not in clean


def test_enforce_trust_no_actions_returns_clean_and_empty():
    out = {"status": "ok", "summary": "nothing to do"}
    clean, parked = enforce_trust(out, "run1", "step1", ["send_email"])
    assert parked == []
    assert clean is out  # Early-return branch — no copy needed.


def test_enforce_trust_mixed_actions():
    out = {
        "actions": [
            {"tool": "send_email", "args": {}},
            {"tool": "read_file", "args": {}},
            {"tool": "send_message", "args": {}},
        ]
    }
    clean, parked = enforce_trust(out, "run1", "step1", ["send_email", "send_message"])
    parked_tools = sorted(a.tool for a in parked)
    assert parked_tools == ["send_email", "send_message"]
    assert len(clean["parked_actions"]) == 2


def test_enforce_trust_skips_malformed_action_entries():
    out = {"actions": [{"tool": "send_email"}, "not_a_dict", {"no_tool_field": "x"}]}
    _, parked = enforce_trust(out, "run1", "step1", ["send_email"])
    assert len(parked) == 1


# ─────────────────────────────────────────────────────────────────────────────
# checklist.py notification path
# ─────────────────────────────────────────────────────────────────────────────


def test_notify_parked_action_sends_telegram_with_buttons():
    from xibi.subagent.checklist import _notify_parked_action

    action = PendingL2Action(
        id="aid-1",
        run_id="run-abcdef12",
        step_id="step-1",
        tool="send_email",
        args={"to": "bob@example.com", "subject": "hi"},
    )
    run = SubagentRun(
        id="run-abcdef12",
        agent_id="test",
        status="RUNNING",
        trigger="manual",
    )
    step = MagicMock(step_order=2)

    with patch("xibi.subagent.checklist.send_message_with_buttons") as send:
        _notify_parked_action(action, run, step)

    send.assert_called_once()
    msg, buttons = send.call_args.args[0], send.call_args.args[1]
    assert "send_email" in msg
    assert "bob@example.com" in msg
    assert any(b["callback_data"] == "l2_action:approve:aid-1" for b in buttons)
    assert any(b["callback_data"] == "l2_action:reject:aid-1" for b in buttons)


def test_notify_parked_action_swallows_telegram_failure(caplog):
    """A Telegram send failure must NOT un-park; row stays PENDING."""
    from xibi.subagent.checklist import _notify_parked_action

    action = PendingL2Action(
        id="aid-2", run_id="r", step_id="s", tool="send_email", args={}
    )
    run = SubagentRun(id="r", agent_id="a", status="RUNNING", trigger="manual")
    step = MagicMock(step_order=1)

    with (
        patch(
            "xibi.subagent.checklist.send_message_with_buttons",
            side_effect=RuntimeError("network down"),
        ),
        caplog.at_level("WARNING"),
    ):
        _notify_parked_action(action, run, step)

    assert any("action_park_notify_failed" in r.message for r in caplog.records)


def test_format_arg_value_truncates_long_strings():
    from xibi.subagent.checklist import _format_arg_value

    long = "x" * 500
    result = _format_arg_value(long, max_len=100)
    assert "chars" in result
    assert len(result) < len(long)


# ─────────────────────────────────────────────────────────────────────────────
# approval_config loader
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolate_approval_config(tmp_path, monkeypatch):
    fake_path = tmp_path / "config.yaml"
    monkeypatch.setattr(approval_config, "CONFIG_PATH", fake_path)
    approval_config._reset_cache()
    yield fake_path
    approval_config._reset_cache()


def test_approval_config_missing_file_returns_empty(isolate_approval_config):
    assert approval_config.get_approval_required_tools() == []


def test_approval_config_loads_required_tools(isolate_approval_config):
    isolate_approval_config.write_text(
        "approval_gates:\n  required_tools:\n    - send_email\n    - send_message\n"
    )
    tools = approval_config.get_approval_required_tools()
    assert tools == ["send_email", "send_message"]


def test_approval_config_missing_section_returns_empty(isolate_approval_config):
    isolate_approval_config.write_text("trust_gate:\n  enabled: true\n")
    assert approval_config.get_approval_required_tools() == []


def test_approval_config_caches_result(isolate_approval_config):
    isolate_approval_config.write_text(
        "approval_gates:\n  required_tools: [send_email]\n"
    )
    first = approval_config.get_approval_required_tools()
    isolate_approval_config.write_text(
        "approval_gates:\n  required_tools: [other]\n"
    )
    second = approval_config.get_approval_required_tools()
    # No cache reset between calls — cached value wins.
    assert first == second == ["send_email"]


# ─────────────────────────────────────────────────────────────────────────────
# Telegram l2_action button handler — DB integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def adapter(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    config = Config({"react_format": "json"})
    registry = MagicMock(spec=SkillRegistry)
    registry.find_skill_for_tool.return_value = "email"
    registry.get_tool_meta.return_value = {"name": "noop", "inputSchema": None}
    executor = MagicMock()
    with patch.dict(
        os.environ,
        {"XIBI_TELEGRAM_TOKEN": "fake_token", "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "123"},
    ):
        a = TelegramAdapter(
            config=config,
            skill_registry=registry,
            executor=executor,
            db_path=db_path,
        )
        a._api_call = MagicMock(return_value={"ok": True})
        return a


def _insert_pending(db_path, *, action_id="aid-1", tool="send_email", args=None, status="PENDING"):
    args = args or {"to": "bob@example.com"}
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO pending_l2_actions (id, run_id, step_id, tool, args, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (action_id, "run-1", "step-1", tool, json.dumps(args), status),
        )
    return action_id


def _row(db_path, action_id):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT status, reviewed_by, reviewed_at FROM pending_l2_actions WHERE id = ?",
            (action_id,),
        )
        return cur.fetchone()


def _callback(data, *, from_id=123, chat_id=123, message_id=99):
    return {
        "id": "cb1",
        "data": data,
        "from": {"id": from_id},
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
    }


def test_l2_action_keyboard_structure(adapter):
    kb = adapter._l2_action_keyboard("aid-1")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 2
    cbs = {b["text"]: b["callback_data"] for b in rows[0]}
    assert cbs["✅ Approve"] == "l2_action:approve:aid-1"
    assert cbs["❌ Reject"] == "l2_action:reject:aid-1"


def test_l2_action_approve_executes_and_marks_approved(adapter):
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))
    adapter.executor.execute.return_value = {"status": "success"}

    adapter._handle_l2_action_button(_callback(f"l2_action:approve:{aid}"))

    status, reviewed_by, reviewed_at = _row(adapter.db_path, aid)
    assert status == "APPROVED"
    assert reviewed_by == "telegram"
    assert reviewed_at  # ISO string set
    # Tool was executed via _invoke_button_action -> executor.execute
    assert adapter.executor.execute.called
    assert adapter.executor.execute.call_args.args[0] == "send_email"
    assert adapter.executor.execute.call_args.args[1] == {"to": "bob@example.com"}
    # Confirmation rendered, buttons stripped
    edits = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Approved and executed" in c.args[1]["text"] for c in edits)


def test_l2_action_reject_marks_rejected_no_execution(adapter):
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))

    adapter._handle_l2_action_button(_callback(f"l2_action:reject:{aid}"))

    status, reviewed_by, _ = _row(adapter.db_path, aid)
    assert status == "REJECTED"
    assert reviewed_by == "telegram"
    assert not adapter.executor.execute.called
    edits = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Rejected" in c.args[1]["text"] for c in edits)


def test_l2_action_double_tap_is_idempotent(adapter):
    """Second tap on the same button must NOT re-execute the tool."""
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))
    adapter.executor.execute.return_value = {"status": "success"}

    adapter._handle_l2_action_button(_callback(f"l2_action:approve:{aid}"))
    first_call_count = adapter.executor.execute.call_count

    # Second tap arrives — row is now APPROVED, must short-circuit
    adapter._handle_l2_action_button(_callback(f"l2_action:approve:{aid}"))

    assert adapter.executor.execute.call_count == first_call_count
    edits = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Already handled" in c.args[1]["text"] for c in edits)


def test_l2_action_already_rejected_not_re_executed(adapter):
    """Approve tap arriving after a Reject must not execute."""
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()), status="REJECTED")
    adapter._handle_l2_action_button(_callback(f"l2_action:approve:{aid}"))
    assert not adapter.executor.execute.called
    status, _, _ = _row(adapter.db_path, aid)
    assert status == "REJECTED"


def test_l2_action_unauthorized_chat_rejected(adapter, caplog):
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))
    cb = _callback(f"l2_action:approve:{aid}", from_id=999)
    with caplog.at_level("WARNING"):
        adapter._handle_l2_action_button(cb)
    assert any("l2_action_unauthorized" in r.message for r in caplog.records)
    # Row untouched
    status, _, _ = _row(adapter.db_path, aid)
    assert status == "PENDING"
    assert not adapter.executor.execute.called


def test_l2_action_bad_data_format(adapter, caplog):
    with caplog.at_level("WARNING"):
        adapter._handle_l2_action_button(_callback("l2_action:malformed"))
    assert any("l2_action_bad_data" in r.message for r in caplog.records)
    assert not adapter.executor.execute.called


def test_l2_action_not_found(adapter):
    adapter._handle_l2_action_button(_callback("l2_action:approve:does-not-exist"))
    edits = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("not found" in c.args[1]["text"].lower() for c in edits)
    assert not adapter.executor.execute.called


def test_l2_action_execution_failure_marks_status_but_reports_error(adapter):
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))
    adapter.executor.execute.return_value = {"status": "error", "message": "smtp boom"}

    adapter._handle_l2_action_button(_callback(f"l2_action:approve:{aid}"))

    # Row already marked APPROVED before execution attempt — irreversible flip.
    status, _, _ = _row(adapter.db_path, aid)
    assert status == "APPROVED"
    edits = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("execution failed" in c.args[1]["text"].lower() for c in edits)


def test_l2_action_callback_dispatches_to_handler(adapter):
    """_handle_callback routes l2_action: prefix to _handle_l2_action_button."""
    aid = _insert_pending(adapter.db_path, action_id=str(uuid.uuid4()))
    adapter.executor.execute.return_value = {"status": "success"}
    adapter._handle_callback(_callback(f"l2_action:approve:{aid}"))
    status, _, _ = _row(adapter.db_path, aid)
    assert status == "APPROVED"


# ─────────────────────────────────────────────────────────────────────────────
# Manager review can no longer write action approvals
# ─────────────────────────────────────────────────────────────────────────────


def test_manager_review_schema_omits_action_approvals():
    """The review-dump prompt template must not invite the LLM to act on
    pending actions. The action_approvals key was removed in step-123."""
    from xibi import observation

    src = Path(observation.__file__).read_text()
    # The prompt schema literal must no longer contain the action_approvals key.
    schema_block_start = src.index('"thread_updates":')
    schema_block_end = src.index('"digest"', schema_block_start)
    schema_section = src[schema_block_start:schema_block_end]
    assert '"action_approvals"' not in schema_section


def test_manager_apply_updates_does_not_touch_pending_actions(tmp_path):
    """Even if a stale prompt produces action_approvals, the apply path
    must NOT write to pending_l2_actions."""
    from xibi.observation import ObservationCycle

    db_path = tmp_path / "obs.db"
    SchemaManager(db_path).migrate()

    aid = _insert_pending(db_path, action_id="manager-test")

    engine = ObservationCycle.__new__(ObservationCycle)
    engine.db_path = db_path
    engine.config = MagicMock()

    # Even if the LLM (somehow) returned action_approvals, the new apply
    # path silently ignores them. The row must stay PENDING.
    engine._apply_manager_updates(
        {
            "thread_updates": [],
            "signal_flags": [],
            "topic_pins": [],
            "contact_updates": [],
            "action_approvals": [{"action_id": aid, "decision": "approve"}],
        }
    )
    status, reviewed_by, _ = _row(db_path, aid)
    assert status == "PENDING"
    assert reviewed_by is None
