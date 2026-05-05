"""Unit tests for the universal trust gate (step-119)."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import sys

import xibi.security.trust_gate  # noqa: F401  -- ensure submodule is imported
from xibi.security.trust_gate import _reset_config_cache, trust_gate

_trust_gate_mod = sys.modules["xibi.security.trust_gate"]


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Force every test to load gate config from a known-empty path."""
    fake_path = tmp_path / "config.yaml"  # absent on disk -> defaults apply
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", fake_path)
    _reset_config_cache()
    yield
    _reset_config_cache()


def test_passthrough_returns_input_unchanged():
    raw = "Hello, world! ${var} <system> stays untouched."
    assert trust_gate(raw, source="test", mode="content") == raw


def test_none_returns_empty_string():
    assert trust_gate(None, source="test") == ""


def test_empty_string_returns_empty_string():
    assert trust_gate("", source="test") == ""


def test_emits_debug_log_on_default_config(caplog):
    caplog.set_level(logging.DEBUG, logger="xibi.security.trust_gate")
    trust_gate("payload", source="email_subject", mode="metadata")
    rec = next(r for r in caplog.records if "trust_gate" in r.getMessage())
    msg = rec.getMessage()
    assert "source=email_subject" in msg
    assert "mode=metadata" in msg
    assert "length=7" in msg
    assert rec.levelno == logging.DEBUG


def test_disabled_skips_logging_and_passes_through(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("trust_gate:\n  enabled: false\n")
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", cfg)
    _reset_config_cache()

    caplog.set_level(logging.DEBUG, logger="xibi.security.trust_gate")
    out = trust_gate("payload", source="x", mode="content")
    assert out == "payload"
    assert not any("trust_gate" in r.getMessage() for r in caplog.records)


def test_log_level_off(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('trust_gate:\n  enabled: true\n  log_level: "off"\n')
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", cfg)
    _reset_config_cache()

    caplog.set_level(logging.DEBUG, logger="xibi.security.trust_gate")
    assert trust_gate("payload", source="x") == "payload"
    assert not any("trust_gate" in r.getMessage() for r in caplog.records)


def test_log_level_info(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("trust_gate:\n  log_level: info\n")
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", cfg)
    _reset_config_cache()

    caplog.set_level(logging.INFO, logger="xibi.security.trust_gate")
    trust_gate("payload", source="x")
    rec = next(r for r in caplog.records if "trust_gate" in r.getMessage())
    assert rec.levelno == logging.INFO


def test_default_enabled_when_section_absent(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("other_namespace:\n  foo: bar\n")  # no trust_gate key
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", cfg)
    _reset_config_cache()

    caplog.set_level(logging.DEBUG, logger="xibi.security.trust_gate")
    assert trust_gate("payload", source="x") == "payload"
    assert any("trust_gate" in r.getMessage() for r in caplog.records)


def test_unknown_keys_ignored_gracefully(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("trust_gate:\n  enabled: true\n  sanitize:\n    mode: shadow\n")  # future-PR keys
    monkeypatch.setattr(_trust_gate_mod, "CONFIG_PATH", cfg)
    _reset_config_cache()
    assert trust_gate("payload", source="x") == "payload"


def test_never_raises_on_binary_unicode_huge():
    assert trust_gate("\x00\x01\x02\x7f\xff", source="bin") == "\x00\x01\x02\x7f\xff"
    assert trust_gate("zalgo: t̶͉̲̱̘̄͠ḛ̷͉̥̟̓s̷̢̩̦̃̔̾t̸̲̱̯͊", source="zalgo")
    huge = "x" * (1024 * 1024)
    assert trust_gate(huge, source="huge") == huge


def test_never_raises_on_internal_failure(monkeypatch, caplog):
    """If logger.debug somehow blows up, the gate falls open and returns text."""

    def boom(*_a, **_kw):
        raise RuntimeError("logger ate it")

    monkeypatch.setattr(_trust_gate_mod.logger, "debug", boom)
    caplog.set_level(logging.DEBUG)
    assert trust_gate("payload", source="x") == "payload"


# ---------- Call-site coverage ----------


def test_mcp_client_calls_gate():
    """MCPClient.call_tool must funnel the success-path result through trust_gate."""
    from xibi.mcp.client import MCPClient, MCPServerConfig

    client = MCPClient(MCPServerConfig(name="srv", command=["x"]))
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()
    client.process.poll.return_value = None
    client.process.stdout.readline.return_value = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hello"}], "isError": False}}
    )

    with (
        patch("select.select", return_value=([client.process.stdout], [], [])),
        patch("xibi.mcp.client.trust_gate", wraps=lambda t, **kw: t) as mock_gate,
    ):
        result = client.call_tool("tool1", {"arg": "val"})

    assert result == {"status": "ok", "result": "hello"}
    mock_gate.assert_called_once_with("hello", source="mcp:srv/tool1", mode="content")


def test_calendar_poller_calls_gate(tmp_path, monkeypatch):
    """poll_calendar_signals routes title and attendee_name through the gate."""
    from datetime import datetime, timedelta, timezone

    import xibi.heartbeat.calendar_poller as cp_mod

    db_path = tmp_path / "test.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, ref_id TEXT,
                ref_source TEXT, topic_hint TEXT, timestamp TEXT, content_preview TEXT,
                summary TEXT, urgency TEXT, entity_type TEXT, entity_text TEXT, env TEXT,
                deep_link_url TEXT, received_via_account TEXT, received_via_email_alias TEXT);
            CREATE TABLE processed_messages (source TEXT, ref_id TEXT, processed_at TEXT,
                PRIMARY KEY (source, ref_id));
            """
        )

    start_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(cp_mod, "load_calendar_config", lambda: [{"label": "p", "calendar_id": "x"}])
    monkeypatch.setattr(
        cp_mod,
        "gcal_request",
        lambda *_a, **_kw: {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Meeting <Sarah>",
                    "start": {"dateTime": start_iso},
                    "attendees": [{"email": "sarah@other.com", "displayName": "Sarah"}],
                }
            ]
        },
    )

    calls = []

    def spy(text, *, source="", mode="content"):
        calls.append({"text": text, "source": source, "mode": mode})
        return text if text else ""

    monkeypatch.setattr(cp_mod, "trust_gate", spy)
    cp_mod.poll_calendar_signals(db_path)

    sources = [c["source"] for c in calls]
    assert "calendar_title" in sources
    assert "calendar_attendee" in sources
    for c in calls:
        assert c["mode"] == "metadata"


def test_email_poller_calls_gate_for_sender_subject_body(monkeypatch):
    """_process_email_signals routes sender/subject as metadata and body as content."""
    import asyncio

    import xibi.heartbeat.poller as poller_mod

    calls = []

    def spy(text, *, source="", mode="content"):
        calls.append({"source": source, "mode": mode, "text": text})
        return text if text else ""

    monkeypatch.setattr(poller_mod, "trust_gate", spy)
    # Halt the function right after the gate calls fire by raising in the next
    # downstream dependency we hit (sender-trust assessment). We do not care
    # about the rest of the email pipeline -- this is a wiring test.
    monkeypatch.setattr(poller_mod, "assess_sender_trust", MagicMock(side_effect=RuntimeError("stop")))
    # find_himalaya is imported lazily inside the function; patching the
    # source module short-circuits the body-fetch branch.
    import xibi.heartbeat.email_body as email_body_mod

    monkeypatch.setattr(email_body_mod, "find_himalaya", MagicMock(side_effect=FileNotFoundError))

    fake_self = MagicMock()
    fake_self.config = {}
    raw_signals = [
        {
            "ref_id": "msg-1",
            "entity_text": "Sarah",
            "topic_hint": "Project update",
            "metadata": {"email": {"from": {"addr": "sarah@example.com", "name": "Sarah"}}},
        }
    ]

    async def run():
        try:
            await poller_mod.HeartbeatPoller._process_email_signals(
                fake_self, raw_signals, seen_ids=set(), triage_rules={}, email_rules=[]
            )
        except RuntimeError:
            pass

    asyncio.run(run())

    sources = [c["source"] for c in calls]
    assert "email_sender" in sources
    assert "email_subject" in sources
    sender_call = next(c for c in calls if c["source"] == "email_sender")
    subject_call = next(c for c in calls if c["source"] == "email_subject")
    assert sender_call["mode"] == "metadata"
    assert subject_call["mode"] == "metadata"
    assert sender_call["text"] == "Sarah"
    assert subject_call["text"] == "Project update"


def test_checklist_calls_gate_for_tool_results_and_prev_outputs(monkeypatch, tmp_path):
    """execute_checklist gates MCP tool results into scoped_input and prev_out into prompt."""
    import xibi.subagent.checklist as cl_mod
    from xibi.db.migrations import migrate
    from xibi.subagent.routing import RoutedResponse
    from xibi.subagent.runtime import spawn_subagent

    db_path = tmp_path / "trust_gate.db"
    migrate(db_path)

    fake_client = MagicMock()
    fake_client.call_tool.return_value = {"status": "ok", "result": "TOOL_OUTPUT"}
    monkeypatch.setattr(cl_mod, "_get_mcp_client", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(cl_mod, "_close_mcp_clients", lambda *_a, **_kw: None)

    monkeypatch.setattr("xibi.subagent.routing.load_config", lambda *_a, **_kw: {})

    def fake_call(*_a, **_kw):
        return RoutedResponse(
            content='{"prev": "data"}',
            model_id="test",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr("xibi.subagent.checklist.ModelRouter.call", fake_call)

    calls = []

    def spy(text, *, source="", mode="content"):
        calls.append({"text": text, "source": source, "mode": mode})
        return text if text else ""

    monkeypatch.setattr(cl_mod, "trust_gate", spy)

    checklist = [
        {"skill_name": "skill1", "model": "haiku", "trust": "L1", "prompt": "p1"},
        {
            "skill_name": "skill2",
            "model": "haiku",
            "trust": "L1",
            "prompt": "p2",
            "tools": [{"server": "srv", "tool": "search", "inject_as": "results"}],
        },
    ]
    spawn_subagent(
        agent_id="trust-test",
        trigger="manual",
        trigger_context={},
        scoped_input={},
        checklist=checklist,
        budget={"max_calls": 5, "max_cost_usd": 1.0, "max_duration_s": 60},
        db_path=db_path,
        mcp_configs=[{"name": "srv", "command": ["x"]}],
    )

    sources = [c["source"] for c in calls]
    assert "subagent_tool:srv/search" in sources
    assert any(s.startswith("subagent_step:") for s in sources)
    step_call = next(c for c in calls if c["source"].startswith("subagent_step:"))
    assert step_call["text"] == json.dumps({"prev": "data"})
    assert step_call["mode"] == "content"
