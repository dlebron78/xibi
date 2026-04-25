"""step-105: Telegram inline-button confirmation channel for email sends.

Covers `_extract_pending_draft_id`, `_email_confirmation_keyboard`, and the
button-tap dispatcher `_handle_email_button` (send / discard / revise / defer)
plus the auth, stale-draft, and SMTP-failure branches.
"""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.channels.telegram import TelegramAdapter
from xibi.db.migrations import SchemaManager
from xibi.router import Config
from xibi.skills.registry import SkillRegistry
from xibi.types import ReActResult, Step


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


def _step(num, tool, output, tool_input=None):
    return Step(step_num=num, tool=tool, tool_input=tool_input or {}, tool_output=output)


def _result(steps):
    return ReActResult(answer="ok", steps=steps, exit_reason="finish", duration_ms=0)


# ── _extract_pending_draft_id ────────────────────────────────────────────────


def test_extract_draft_id_from_draft_email_step(adapter):
    r = _result([_step(1, "draft_email", {"status": "success", "draft_id": "DR_abc"})])
    assert adapter._extract_pending_draft_id(r) == "DR_abc"


def test_extract_draft_id_from_reply_email_step(adapter):
    r = _result([_step(1, "reply_email", {"status": "success", "draft_id": "DR_xyz"})])
    assert adapter._extract_pending_draft_id(r) == "DR_xyz"


def test_extract_draft_id_returns_none_when_absent(adapter):
    r = _result([_step(1, "list_emails", {"status": "success"})])
    assert adapter._extract_pending_draft_id(r) is None


def test_extract_draft_id_returns_latest_when_multiple(adapter):
    r = _result(
        [
            _step(1, "draft_email", {"status": "success", "draft_id": "DR_old"}),
            _step(2, "draft_email", {"status": "success", "draft_id": "DR_new"}),
        ]
    )
    assert adapter._extract_pending_draft_id(r) == "DR_new"


def test_extract_draft_id_skips_failed_steps(adapter):
    r = _result(
        [
            _step(1, "draft_email", {"status": "error", "message": "boom"}),
            _step(2, "list_emails", {"status": "success"}),
        ]
    )
    assert adapter._extract_pending_draft_id(r) is None


# ── _email_confirmation_keyboard ─────────────────────────────────────────────


def test_keyboard_structure_2x2(adapter):
    kb = adapter._email_confirmation_keyboard("DR_abc")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert len(rows[0]) == 2 and len(rows[1]) == 2
    labels = [b["text"] for row in rows for b in row]
    assert labels == ["✅ Send", "❌ Discard", "✏️ Revise", "💾 Save"]


def test_keyboard_callback_data_format(adapter):
    kb = adapter._email_confirmation_keyboard("DR_abc")
    expected = {
        "✅ Send": "email_action:send:DR_abc",
        "❌ Discard": "email_action:discard:DR_abc",
        "✏️ Revise": "email_action:revise:DR_abc",
        "💾 Save": "email_action:defer:DR_abc",
    }
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert btn["callback_data"] == expected[btn["text"]]


# ── auth + parse guards ──────────────────────────────────────────────────────


def _callback(data, from_id=123, chat_id=123, message_id=99):
    return {
        "id": "cb1",
        "data": data,
        "from": {"id": from_id},
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
    }


def test_handle_button_unauthorized_chat_rejected(adapter, caplog):
    cb = _callback("email_action:send:DR_abc", from_id=999)
    with caplog.at_level("WARNING"):
        adapter._handle_email_button(cb)
    assert any("email_button_unauthorized" in r.message and "999" in r.message for r in caplog.records)
    assert not adapter.executor.execute.called


def test_handle_button_bad_data_format_rejected(adapter, caplog):
    cb = _callback("email_action:badformat")
    with caplog.at_level("WARNING"):
        adapter._handle_email_button(cb)
    assert any("email_button_bad_data" in r.message for r in caplog.records)
    assert not adapter.executor.execute.called


# ── send happy path ──────────────────────────────────────────────────────────


def test_handle_send_calls_confirm_then_send(adapter):
    adapter.executor.execute.side_effect = [
        {"status": "success", "draft_id": "DR_abc"},  # confirm_draft
        {"status": "success", "message": "sent"},  # send_email
    ]
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))
    calls = [c.args[0] for c in adapter.executor.execute.call_args_list]
    assert calls == ["confirm_draft", "send_email"]


def test_handle_send_audit_logged(adapter):
    """confirm_draft is YELLOW and must write an access_log audit row."""
    adapter.executor.execute.side_effect = [
        {"status": "success", "draft_id": "DR_abc"},
        {"status": "success", "message": "sent"},
    ]
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))

    with sqlite3.connect(adapter.db_path) as conn:
        row = conn.execute(
            "SELECT chat_id, effective_tier FROM access_log WHERE chat_id='tool:confirm_draft'"
        ).fetchone()
    assert row is not None
    assert row[1] == "yellow"


def test_discard_draft_audit_logged(adapter):
    """Condition #1: discard_draft must be YELLOW and produce an audit row."""
    adapter.executor.execute.return_value = {"status": "success", "message": "ok"}
    adapter._handle_email_button(_callback("email_action:discard:DR_abc"))

    with sqlite3.connect(adapter.db_path) as conn:
        row = conn.execute("SELECT effective_tier FROM access_log WHERE chat_id='tool:discard_draft'").fetchone()
    assert row is not None
    assert row[0] == "yellow"


def test_handle_send_stale_draft_message(adapter):
    """confirm_draft fails — message edits to 'Already actioned', send_email never runs."""
    adapter.executor.execute.return_value = {
        "status": "error",
        "message": "draft already in status 'sent'",
    }
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))
    assert adapter.executor.execute.call_count == 1
    edit_calls = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Already actioned" in c.args[1]["text"] for c in edit_calls)


def test_handle_send_smtp_failure_reverts_and_re_renders_buttons(adapter):
    adapter.executor.execute.side_effect = [
        {"status": "success", "draft_id": "DR_abc"},
        {"status": "error", "message": "SMTP boom"},
    ]
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))

    edit_text_calls = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Send failed" in c.args[1]["text"] for c in edit_text_calls)

    rerender = [
        c
        for c in adapter._api_call.call_args_list
        if c.args[0] == "editMessageReplyMarkup" and c.args[1]["reply_markup"].get("inline_keyboard")
    ]
    assert len(rerender) == 1
    rendered = rerender[0].args[1]["reply_markup"]["inline_keyboard"]
    assert any(btn["callback_data"] == "email_action:send:DR_abc" for row in rendered for btn in row)


# ── discard ──────────────────────────────────────────────────────────────────


def test_handle_discard_marks_status_discarded(adapter):
    adapter.executor.execute.return_value = {"status": "success", "message": "ok"}
    adapter._handle_email_button(_callback("email_action:discard:DR_abc"))
    assert adapter.executor.execute.call_args.args[0] == "discard_draft"
    edit_calls = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("Discarded" in c.args[1]["text"] for c in edit_calls)


# ── revise ───────────────────────────────────────────────────────────────────


def test_handle_revise_no_tool_call(adapter):
    adapter._handle_email_button(_callback("email_action:revise:DR_abcd1234"))
    assert not adapter.executor.execute.called


def test_handle_revise_edits_message(adapter):
    adapter._handle_email_button(_callback("email_action:revise:DR_abcd1234"))
    edit_calls = [c for c in adapter._api_call.call_args_list if c.args[0] == "editMessageText"]
    assert any("What changes?" in c.args[1]["text"] and "DR_abcd1" in c.args[1]["text"] for c in edit_calls)
    strip_calls = [
        c
        for c in adapter._api_call.call_args_list
        if c.args[0] == "editMessageReplyMarkup" and c.args[1]["reply_markup"] == {"inline_keyboard": []}
    ]
    assert strip_calls, "buttons should be stripped on revise"


# ── defer (Save for later) ───────────────────────────────────────────────────


def test_handle_defer_no_tool_call(adapter):
    adapter._handle_email_button(_callback("email_action:defer:DR_abc"))
    assert not adapter.executor.execute.called


def test_handle_defer_logs_warning(adapter, caplog):
    with caplog.at_level("WARNING"):
        adapter._handle_email_button(_callback("email_action:defer:DR_abc"))
    assert any("draft_deferred" in r.message and "DR_abc" in r.message for r in caplog.records)


def test_handle_defer_status_unchanged(adapter):
    """Defer must NOT touch the ledger — no executor call, no DB mutation."""
    # Seed a pending draft in ledger
    with sqlite3.connect(adapter.db_path) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content, entity, status) "
            "VALUES (?, 'draft_email', '{}', 'x@y.com', 'pending')",
            ("DR_abc",),
        )
        conn.commit()

    adapter._handle_email_button(_callback("email_action:defer:DR_abc"))

    with sqlite3.connect(adapter.db_path) as conn:
        status = conn.execute("SELECT status FROM ledger WHERE id=?", ("DR_abc",)).fetchone()[0]
    assert status == "pending"


# ── race / span attribution ──────────────────────────────────────────────────


def test_double_tap_send_only_one_smtp(adapter):
    """If confirm_draft returns success once and 'already' on the second tap,
    send_email runs exactly once across both taps."""
    adapter.executor.execute.side_effect = [
        # First tap
        {"status": "success", "draft_id": "DR_abc"},
        {"status": "success", "message": "sent"},
        # Second tap — confirm_draft now returns error (status no longer pending)
        {"status": "error", "message": "draft already in status 'sent'"},
    ]
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))

    tools = [c.args[0] for c in adapter.executor.execute.call_args_list]
    assert tools.count("send_email") == 1


def test_buttons_attach_when_draft_in_steps(adapter):
    """Whitebox: send_message receives reply_markup when extract finds a draft."""
    adapter.send_message = MagicMock()
    result = _result([_step(1, "draft_email", {"status": "success", "draft_id": "DR_abc"})])

    with (
        patch("xibi.channels.telegram.is_chitchat", return_value=False),
        patch("xibi.channels.telegram.react_run", return_value=result),
    ):
        adapter._handle_text(123, "send daniel hi")

    assert adapter.send_message.called
    kwargs = adapter.send_message.call_args.kwargs
    assert kwargs.get("reply_markup") is not None
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "email_action:send:DR_abc"


def test_buttons_omit_when_no_draft_in_steps(adapter):
    adapter.send_message = MagicMock()
    result = _result([_step(1, "list_emails", {"status": "success"})])

    with (
        patch("xibi.channels.telegram.is_chitchat", return_value=False),
        patch("xibi.channels.telegram.react_run", return_value=result),
    ):
        adapter._handle_text(123, "what's in my inbox")

    assert adapter.send_message.called
    assert adapter.send_message.call_args.kwargs.get("reply_markup") is None


def test_button_tap_span_emitted_with_attributes(adapter):
    adapter.executor.execute.side_effect = [
        {"status": "success", "draft_id": "DR_abc"},
        {"status": "success", "message": "sent"},
    ]
    adapter._handle_email_button(_callback("email_action:send:DR_abc"))

    with sqlite3.connect(adapter.db_path) as conn:
        row = conn.execute(
            "SELECT attributes FROM spans WHERE operation='telegram.button_tap' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    attrs = json.loads(row[0])
    assert attrs["action"] == "send"
    assert attrs["outcome"] == "success"
    assert attrs["draft_id"] == "DR_abc"[:8]


def test_button_tap_span_emitted_on_unauthorized(adapter):
    """Condition #7: unauthorized rejection must still emit a button_tap span."""
    cb = _callback("email_action:send:DR_abc", from_id=999)
    adapter._handle_email_button(cb)

    with sqlite3.connect(adapter.db_path) as conn:
        row = conn.execute(
            "SELECT attributes FROM spans WHERE operation='telegram.button_tap' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    attrs = json.loads(row[0])
    assert attrs["outcome"] == "unauthorized"
