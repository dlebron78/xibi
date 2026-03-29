from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.radiant import Radiant


@pytest.fixture
def db_path(tmp_path: Path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


@pytest.fixture
def radiant(db_path: Path):
    return Radiant(db_path, profile={"allowed_chat_ids": [123]})


@pytest.fixture
def adapter():
    return MagicMock()


def test_run_audit_no_cycles(radiant, adapter):
    with patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter)
        assert res["quality_score"] == 1.0
        assert res["cycles_reviewed"] == 0
        adapter.send_message.assert_not_called()


def test_run_audit_with_mocked_model(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
            ("2023-01-01 10:00:00", 5, json.dumps([{"tool": "nudge"}])),
        )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.return_value = {
        "quality_score": 0.85,
        "findings": [{"cycle_id": 1, "action_type": "nudge", "classification": "GOOD", "reason": "OK"}],
        "summary": "Good quality",
    }

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter)
        assert res["quality_score"] == 0.85
        assert res["cycles_reviewed"] == 1

        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM audit_results").fetchone()
            assert row is not None
            assert row["quality_score"] == 0.85


def test_run_audit_parse_failure(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
            ("2023-01-01 10:00:00", 5, json.dumps([{"tool": "nudge"}])),
        )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.side_effect = Exception("Parse error")

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter)
        assert res["quality_score"] == 1.0  # fallback

        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM audit_results").fetchone()
            assert row is not None
            assert row["quality_score"] == 1.0


def test_run_audit_alert_fires(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
            ("2023-01-01 10:00:00", 5, json.dumps([{"tool": "nudge"}])),
        )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.return_value = {
        "quality_score": 0.5,
        "findings": [{"cycle_id": 1, "action_type": "nudge", "classification": "OVER_NUDGE", "reason": "Bad"}],
        "summary": "Poor quality",
    }

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter)
        assert res["quality_score"] == 0.5
        adapter.send_message.assert_called_once()
        assert "audit alert" in adapter.send_message.call_args[0][1]


def test_run_audit_no_alert_above_threshold(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
            ("2023-01-01 10:00:00", 5, json.dumps([{"tool": "nudge"}])),
        )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.return_value = {"quality_score": 0.9, "findings": [], "summary": "Great"}

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        radiant.run_audit(adapter)
        adapter.send_message.assert_not_called()


def test_run_audit_dedup_same_day(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
            ("2023-01-01 10:00:00", 5, json.dumps([{"tool": "nudge"}])),
        )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.return_value = {"quality_score": 0.9, "findings": [], "summary": "Great"}

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        radiant.run_audit(adapter)
        assert mock_model.generate_structured.call_count == 1

        radiant.run_audit(adapter)
        assert mock_model.generate_structured.call_count == 1  # still 1


def test_run_audit_never_raises(radiant, adapter):
    # Using a non-existent DB path
    radiant.db_path = Path("/non/existent/path/db.sqlite")
    with patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter)
        assert res == {}


def test_run_audit_custom_lookback(radiant, adapter, db_path):
    with open_db(db_path) as conn, conn:
        for i in range(10):
            conn.execute(
                "INSERT INTO observation_cycles (completed_at, signals_processed, actions_taken) VALUES (?, ?, ?)",
                (f"2023-01-01 10:0{i}:00", i, "[]"),
            )

    mock_model = MagicMock()
    mock_model.provider = "test"
    mock_model.model = "test-model"
    mock_model.generate_structured.return_value = {"quality_score": 1.0, "findings": [], "summary": ""}

    with patch("xibi.radiant.get_model", return_value=mock_model), patch("xibi.radiant._audit_run_date", ""):
        res = radiant.run_audit(adapter, lookback=5)
        assert res["cycles_reviewed"] == 5


def test_summary_audit_empty(radiant):
    s = radiant.summary()
    assert "audit" in s
    assert s["audit"]["latest_score"] == 1.0
    assert s["audit"]["runs_total"] == 0


def test_summary_audit_with_results(radiant, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO audit_results (quality_score) VALUES (0.7)")
        conn.execute("INSERT INTO audit_results (quality_score) VALUES (0.8)")

    s = radiant.summary()
    assert s["audit"]["latest_score"] == 0.8
    assert s["audit"]["runs_total"] == 2


def test_summary_audit_cycles_since(radiant, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO audit_results (quality_score, audited_at) VALUES (1.0, '2023-01-01 10:00:00')")
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed) VALUES ('2023-01-01 11:00:00', 1)"
        )
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed) VALUES ('2023-01-01 12:00:00', 1)"
        )
        conn.execute(
            "INSERT INTO observation_cycles (completed_at, signals_processed) VALUES ('2023-01-01 13:00:00', 1)"
        )

    s = radiant.summary()
    assert s["audit"]["cycles_since_last_audit"] == 3
