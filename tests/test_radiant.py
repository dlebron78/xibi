from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate
from xibi.radiant import Radiant, COST_PER_TOKEN, _nudge_state


@pytest.fixture
def db_path(tmp_path: Path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


# --- record() tests ---


def test_record_ollama_zero_cost(db_path: Path):
    radiant = Radiant(db_path)
    radiant.record(
        role="fast",
        provider="ollama",
        model="qwen3.5:4b",
        operation="test",
        prompt_tokens=1000,
        response_tokens=500,
        duration_ms=100,
    )

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT cost_usd FROM inference_events")
        row = cursor.fetchone()
        assert row[0] == 0.0


def test_record_gemini_flash_cost(db_path: Path):
    radiant = Radiant(db_path)
    provider = "gemini"
    model = "gemini-2.5-flash"
    prompt_tokens = 10000
    response_tokens = 2000

    radiant.record(
        role="fast",
        provider=provider,
        model=model,
        operation="test",
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        duration_ms=100,
    )

    rates = COST_PER_TOKEN[(provider, model)]
    expected_cost = (prompt_tokens * rates["input"] + response_tokens * rates["output"]) / 1_000_000

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT cost_usd FROM inference_events")
        row = cursor.fetchone()
        assert pytest.approx(row[0], abs=1e-6) == expected_cost


def test_record_prefix_matching(db_path: Path):
    radiant = Radiant(db_path)
    provider = "openai"
    model = "gpt-4o-2024-08-06"  # Should match "gpt-4o"
    prompt_tokens = 1000
    response_tokens = 500

    radiant.record(
        role="fast",
        provider=provider,
        model=model,
        operation="test",
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        duration_ms=100,
    )

    rates = COST_PER_TOKEN[("openai", "gpt-4o")]
    expected_cost = (prompt_tokens * rates["input"] + response_tokens * rates["output"]) / 1_000_000

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT cost_usd FROM inference_events")
        row = cursor.fetchone()
        assert pytest.approx(row[0], abs=1e-6) == expected_cost


def test_record_unknown_provider_zero_cost(db_path: Path):
    radiant = Radiant(db_path)
    radiant.record(
        role="fast",
        provider="unknown_provider",
        model="unknown_model",
        operation="test",
        prompt_tokens=1000,
        response_tokens=500,
        duration_ms=100,
    )

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT cost_usd FROM inference_events")
        row = cursor.fetchone()
        assert row[0] == 0.0


def test_record_never_raises(caplog):
    radiant = Radiant(Path("/non/existent/path/xibi.db"))
    # Should not raise
    radiant.record(
        role="fast",
        provider="ollama",
        model="model",
        operation="test",
        prompt_tokens=1000,
        response_tokens=500,
        duration_ms=100,
    )
    assert "Radiant: failed to record inference event" in caplog.text


# --- daily_cost() tests ---


def test_daily_cost_today(db_path: Path):
    radiant = Radiant(db_path)
    radiant.record("fast", "gemini", "gemini-2.5-flash", "test", 1000000, 0, 100)  # cost = 0.075
    radiant.record("fast", "gemini", "gemini-2.5-flash", "test", 0, 1000000, 100)  # cost = 0.30
    assert radiant.daily_cost() == 0.375


def test_daily_cost_yesterday(db_path: Path):
    radiant = Radiant(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO inference_events (role, provider, model, operation, cost_usd, recorded_at) VALUES (?, ?, ?, ?, ?, datetime('now', '-1 day'))",
            ("fast", "gemini", "gemini-2.5-flash", "test", 1.0),
        )
    radiant.record("fast", "gemini", "gemini-2.5-flash", "test", 1000000, 0, 100)  # cost = 0.075
    assert radiant.daily_cost() == 0.075


def test_daily_cost_empty(db_path: Path):
    radiant = Radiant(db_path)
    assert radiant.daily_cost() == 0.0


# --- ceiling_status() tests ---


def test_ceiling_status_under_80pct(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 10.0})
    radiant.record("fast", "gemini", "gemini-2.5-flash", "test", 10000000, 0, 100)  # cost = 0.75 (7.5%)
    status = radiant.ceiling_status()
    assert status["used_today"] == 0.75
    assert status["pct"] == 0.075
    assert status["warn"] is False
    assert status["throttle"] is False


def test_ceiling_status_at_80pct(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0})
    radiant.record("fast", "openai", "gpt-4o", "test", 320000, 0, 100)  # cost = 0.8
    status = radiant.ceiling_status()
    assert status["pct"] == 0.8
    assert status["warn"] is True
    assert status["throttle"] is False


def test_ceiling_status_at_100pct(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0})
    radiant.record("fast", "openai", "gpt-4o", "test", 400000, 0, 100)  # cost = 1.0
    status = radiant.ceiling_status()
    assert status["pct"] == 1.0
    assert status["warn"] is True
    assert status["throttle"] is True


def test_ceiling_status_default_ceiling(db_path: Path):
    radiant = Radiant(db_path, profile={})
    assert radiant.cost_ceiling_daily == 5.0


# --- summary() tests ---


def test_summary_structure(db_path: Path):
    radiant = Radiant(db_path)
    summary = radiant.summary()
    assert "inference_by_role" in summary
    assert "daily_costs" in summary
    assert "degradation_events" in summary
    assert "ceiling" in summary
    assert "observation_cycle_stats" in summary


def test_summary_inference_by_role(db_path: Path):
    radiant = Radiant(db_path)
    radiant.record("fast", "ollama", "m1", "op", 10, 20, 100)
    radiant.record("think", "ollama", "m1", "op", 30, 40, 100)
    radiant.record("review", "ollama", "m1", "op", 50, 60, 100)

    summary = radiant.summary()
    assert summary["inference_by_role"]["fast"]["count"] == 1
    assert summary["inference_by_role"]["fast"]["total_tokens"] == 30
    assert summary["inference_by_role"]["think"]["count"] == 1
    assert summary["inference_by_role"]["think"]["total_tokens"] == 70
    assert summary["inference_by_role"]["review"]["count"] == 1
    assert summary["inference_by_role"]["review"]["total_tokens"] == 110


def test_summary_daily_costs_last_7_days(db_path: Path):
    radiant = Radiant(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO inference_events (role, provider, model, operation, cost_usd, recorded_at) VALUES (?, ?, ?, ?, ?, datetime('now', '-2 days'))",
            ("fast", "ollama", "m1", "op", 1.0),
        )
        conn.execute(
            "INSERT INTO inference_events (role, provider, model, operation, cost_usd, recorded_at) VALUES (?, ?, ?, ?, ?, datetime('now', '-1 day'))",
            ("fast", "ollama", "m1", "op", 2.0),
        )
    radiant.record("fast", "ollama", "m1", "op", 0, 0, 100)  # cost 0, today

    summary = radiant.summary()
    assert len(summary["daily_costs"]) == 3


def test_summary_degradation_events(db_path: Path):
    radiant = Radiant(db_path)
    radiant.record("fast", "ollama", "m1", "op", 0, 0, 100, degraded=True)
    radiant.record("fast", "ollama", "m1", "op", 0, 0, 100, degraded=True)
    radiant.record("fast", "ollama", "m1", "op", 0, 0, 100, degraded=False)

    summary = radiant.summary()
    assert summary["degradation_events"] == 2


def test_summary_observation_cycle_stats(db_path: Path):
    radiant = Radiant(db_path)
    with sqlite3.connect(db_path) as conn:
        actions1 = json.dumps([{"tool": "nudge"}, {"tool": "create_task"}])
        actions2 = json.dumps([{"tool": "nudge"}])
        conn.execute(
            "INSERT INTO observation_cycles (started_at, completed_at, actions_taken) VALUES (datetime('now'), datetime('now'), ?)",
            (actions1,),
        )
        conn.execute(
            "INSERT INTO observation_cycles (started_at, completed_at, actions_taken) VALUES (datetime('now'), datetime('now'), ?)",
            (actions2,),
        )

    summary = radiant.summary()
    assert summary["observation_cycle_stats"]["total_cycles"] == 2
    assert summary["observation_cycle_stats"]["nudges_issued"] == 2
    assert summary["observation_cycle_stats"]["tasks_created"] == 1


# --- check_and_nudge() tests ---


@pytest.fixture(autouse=True)
def reset_nudge_state():
    _nudge_state["warn_sent"] = ""
    _nudge_state["throttle_sent"] = ""


def test_check_and_nudge_no_action_below_80(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 10.0, "allowed_chat_ids": [123]})
    adapter = MagicMock()
    radiant.check_and_nudge(adapter)
    adapter.send_message.assert_not_called()


def test_check_and_nudge_warn_sends_message(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0, "allowed_chat_ids": [123]})
    radiant.record("fast", "openai", "gpt-4o", "test", 350000, 0, 100)  # cost = 0.875
    adapter = MagicMock()
    radiant.check_and_nudge(adapter)
    adapter.send_message.assert_called_once()
    assert "cost alert" in adapter.send_message.call_args[0][1]


def test_check_and_nudge_throttle_sends_message(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0, "allowed_chat_ids": [123]})
    radiant.record("fast", "openai", "gpt-4o", "test", 450000, 0, 100)  # cost = 1.125
    adapter = MagicMock()
    radiant.check_and_nudge(adapter)
    adapter.send_message.assert_called_once()
    assert "ceiling reached" in adapter.send_message.call_args[0][1]


def test_check_and_nudge_deduplication(db_path: Path):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0, "allowed_chat_ids": [123]})
    radiant.record("fast", "openai", "gpt-4o", "test", 350000, 0, 100)  # cost = 0.875
    adapter = MagicMock()
    radiant.check_and_nudge(adapter)
    radiant.check_and_nudge(adapter)
    assert adapter.send_message.call_count == 1


def test_check_and_nudge_never_raises(db_path: Path, caplog):
    radiant = Radiant(db_path, profile={"cost_ceiling_daily": 1.0, "allowed_chat_ids": [123]})
    radiant.record("fast", "openai", "gpt-4o", "test", 350000, 0, 100)
    adapter = MagicMock()
    adapter.send_message.side_effect = Exception("Telegram down")
    radiant.check_and_nudge(adapter)
    assert "Radiant: failed to check and nudge" in caplog.text
