from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask

from xibi.dashboard import DashboardConfig, create_app, get_system_health


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fixture to create a temporary SQLite database with required tables and some data."""
    db_file = tmp_path / "test_xibi.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME DEFAULT '2026-03-25 00:00:00',
                description TEXT
            );
            INSERT INTO schema_version (version, description) VALUES (1, 'Initial');

            CREATE TABLE traces (
                id TEXT PRIMARY KEY,
                model TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                steps_detail TEXT,
                shadow_tier TEXT
            );

            CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                mode TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                ref_id TEXT,
                classification TEXT,
                content_preview TEXT NOT NULL
            );
        """)
        conn.commit()
    return db_file


@pytest.fixture
def client(db_path: Path):
    config = DashboardConfig(db_path=db_path)
    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_create_app_returns_flask(db_path: Path):
    config = DashboardConfig(db_path=db_path)
    app = create_app(config)
    assert isinstance(app, Flask)


def test_health_ok(client, db_path: Path):
    # Seed a trace
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO traces (id, model, created_at, status) VALUES (?, ?, ?, ?)",
            ("t1", "test-model", "2026-03-25 02:14:00", "completed"),
        )
        conn.commit()

    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["last_trace"] == "2026-03-25 02:14:00"
    assert data["model"] == "test-model"
    assert "cpu_percent" in data
    assert "ram_used_mb" in data
    assert "uptime_seconds" in data


def test_health_db_missing():
    config = DashboardConfig(db_path=Path("/nonexistent/db.sqlite"))
    app = create_app(config)
    with app.test_client() as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "degraded"
        assert "error" in data


def test_trends_empty(client):
    response = client.get("/api/trends")
    assert response.status_code == 200
    data = response.get_json()
    assert data["labels"] == []
    assert data["counts"] == []


def test_trends_data(client, db_path: Path):
    # Use fixed dates relative to "now" for test reliability
    # The query uses UTC now - 30 days
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    d1 = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    d2 = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("hi", "hello", d1),
        )
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("hi2", "hello2", d1),
        )
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("bye", "goodbye", d2),
        )
        conn.commit()

    response = client.get("/api/trends")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["labels"]) == 2
    assert len(data["counts"]) == 2
    assert data["counts"] == [2, 1]


def test_errors_empty(client):
    response = client.get("/api/errors")
    assert response.status_code == 200
    assert response.get_json() == []


def test_errors_data(tmp_path: Path):
    # Create DB with error/query columns
    db_file = tmp_path / "error_data.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE traces (id TEXT PRIMARY KEY, created_at DATETIME, query TEXT, error TEXT, model TEXT)"
        )
        conn.execute(
            "INSERT INTO traces (id, created_at, query, error, model) VALUES (?, ?, ?, ?, ?)",
            ("t1", "2026-03-25 01:00:00", "q1", "err1", "m1"),
        )
        conn.execute(
            "INSERT INTO traces (id, created_at, query, error, model) VALUES (?, ?, ?, ?, ?)",
            ("t2", "2026-03-25 02:00:00", "q2", "err2", "m2"),
        )
        conn.commit()

    config = DashboardConfig(db_path=db_file)
    app = create_app(config)
    with app.test_client() as client:
        response = client.get("/api/errors")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 2
        assert data[0]["error"] == "err2"  # Ordered by created_at DESC


def test_recent_conversations(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO conversation_history (user_message, bot_response) VALUES (?, ?)", (f"u{i}", f"b{i}")
            )
        conn.commit()

    response = client.get("/api/recent")
    assert response.status_code == 200
    data = response.get_json()
    # 5 rows * 2 messages each = 10, but API limits to 10 entries.
    assert len(data) == 10


def test_shadow_stats_no_column(tmp_path: Path):
    # Create DB without shadow_tier column
    db_file = tmp_path / "no_shadow.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE traces (id TEXT PRIMARY KEY, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")

    config = DashboardConfig(db_path=db_file)
    app = create_app(config)
    with app.test_client() as client:
        response = client.get("/api/shadow")
        assert response.status_code == 200
        data = response.get_json()
        assert data["total"] == 0
        assert "note" in data
        assert data["note"] == "shadow_tier column not present"


def test_shadow_stats_with_data(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO traces (id, shadow_tier) VALUES (?, ?)", ("s1", "direct"))
        conn.execute("INSERT INTO traces (id, shadow_tier) VALUES (?, ?)", ("s2", "hint"))
        conn.execute("INSERT INTO traces (id, shadow_tier) VALUES (?, ?)", ("s3", "other"))
        conn.commit()

    response = client.get("/api/shadow")
    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 3
    assert data["direct_hits"] == 1
    assert data["hint_hits"] == 1
    assert data["misses"] == 1


def test_signals_empty(client):
    response = client.get("/api/signals")
    assert response.status_code == 200
    assert response.get_json() == {"signals": [], "active_threads": []}


def test_signals_data(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, ref_id, classification, content_preview) VALUES (?, ?, ?, ?)",
            ("email", "e1", "URGENT", "pre1"),
        )
        conn.execute(
            "INSERT INTO signals (source, ref_id, classification, content_preview) VALUES (?, ?, ?, ?)",
            ("email", "e2", "DIGEST", "pre2"),
        )
        conn.execute(
            "INSERT INTO signals (source, ref_id, classification, content_preview) VALUES (?, ?, ?, ?)",
            ("email", "e3", "NOISE", "pre3"),
        )
        conn.commit()

    response = client.get("/api/signals")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["signals"]) == 3
    assert data["signals"][0]["classification"] == "URGENT"


def test_signal_pipeline_empty(client):
    response = client.get("/api/signal_pipeline")
    assert response.status_code == 200
    assert response.get_json() == {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}


def test_signal_pipeline_grouped(client, db_path: Path):
    # The new implementation uses by_urgency if classification/urgency column exists
    # In the fixture, signals table has 'classification' column but not 'urgency'
    # Actually get_signal_pipeline rewrite uses 'urgency' and 'action_type' columns specifically.
    # Let's check the fixture again.
    # classification is there, but get_signal_pipeline(conn) checks for urgency.

    with sqlite3.connect(db_path) as conn:
        # Add urgency column if missing (fixture only has classification)
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN urgency TEXT")
        except sqlite3.OperationalError:
            pass

        conn.execute(
            "INSERT INTO signals (source, urgency, content_preview) VALUES (?, ?, ?)",
            ("email", "high", "pre1"),
        )
        conn.execute(
            "INSERT INTO signals (source, urgency, content_preview) VALUES (?, ?, ?)",
            ("email", "high", "pre2"),
        )
        conn.execute(
            "INSERT INTO signals (source, urgency, content_preview) VALUES (?, ?, ?)",
            ("email", "normal", "pre3"),
        )
        conn.commit()

    response = client.get("/api/signal_pipeline")
    assert response.status_code == 200
    data = response.get_json()
    assert data["by_urgency"]["high"] == 2
    assert data["by_urgency"]["normal"] == 1


def test_root_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"<html" in response.data.lower()


def test_health_check_detects_missing_db(tmp_path):
    mock_config = {"models": {}, "providers": {}}
    result = get_system_health(db_path=tmp_path / "nonexistent.db", config=mock_config)
    assert result["database"].startswith("error")
    assert result["status"] == "degraded"


def test_health_check_healthy_after_init(tmp_path, monkeypatch):
    # Setup a mock workdir
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "data").mkdir()
    db_path = workdir / "data" / "xibi.db"
    config_path = workdir / "config.json"

    # Initialize DB
    from xibi.db.migrations import migrate

    migrate(db_path)

    # Create a minimal valid config
    config = {"models": {"text": {"fast": {"provider": "mock", "model": "m1"}}}, "providers": {"mock": {}}}
    config_path.write_text(json.dumps(config))

    # Mock get_model to return something for our mock provider
    monkeypatch.setattr("xibi.dashboard.app.get_model", lambda **kwargs: MagicMock())

    result = get_system_health(db_path=db_path, config=config)
    print(f"DEBUG: result={result}")  # Debug
    assert result["database"] == "ok"
    assert result["schema"] == "ok"
    assert result["llm_provider"] == "ok"
    assert result["status"] == "healthy"


def test_full_health_endpoint(client, db_path, monkeypatch):
    # Mock get_model to avoid actual LLM calls
    monkeypatch.setattr("xibi.dashboard.app.get_model", lambda **kwargs: MagicMock())
    # Mock load_config
    monkeypatch.setattr("xibi.router.load_config", lambda *args: {"models": {}, "providers": {}})

    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert "database" in data
    assert "schema" in data
    assert "llm_provider" in data
    assert "status" in data


def test_inference_stats_returns_structure(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inference_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                operation TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                response_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                degraded INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO inference_events (role, provider, model, operation, prompt_tokens, response_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fast", "mock", "m1", "op1", 10, 20, 0.001),
        )
        conn.execute(
            "INSERT INTO inference_events (role, provider, model, operation, prompt_tokens, response_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("think", "mock", "m2", "op2", 50, 100, 0.005),
        )
        conn.commit()

    response = client.get("/api/inference")
    assert response.status_code == 200
    data = response.get_json()
    assert "last_24h_tokens" in data
    assert "last_24h_cost_usd" in data
    assert "by_role_7d" in data
    assert "recent" in data
    assert len(data["recent"]) == 2
    assert data["last_24h_tokens"] == 180
    assert abs(data["last_24h_cost_usd"] - 0.006) < 1e-6


def test_inference_stats_empty_table(client, db_path: Path):
    # Ensure table doesn't exist
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS inference_events")
        conn.commit()

    response = client.get("/api/inference")
    assert response.status_code == 200
    assert response.get_json() == {"error": "no data"}


def test_trust_records_computes_failure_rate(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trust_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                specialty TEXT NOT NULL,
                effort TEXT NOT NULL,
                audit_interval INTEGER NOT NULL DEFAULT 5,
                consecutive_clean INTEGER NOT NULL DEFAULT 0,
                total_outputs INTEGER NOT NULL DEFAULT 0,
                total_failures INTEGER NOT NULL DEFAULT 0,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(specialty, effort)
            )
            """
        )
        conn.execute(
            "INSERT INTO trust_records (specialty, effort, total_outputs, total_failures) VALUES (?, ?, ?, ?)",
            ("text", "fast", 10, 2),
        )
        conn.commit()

    response = client.get("/api/trust")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 1
    assert data[0]["failure_rate_pct"] == 20.0


def test_audit_results_returns_latest(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audited_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                cycles_reviewed INTEGER NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT 1.0,
                nudges_flagged INTEGER NOT NULL DEFAULT 0,
                missed_signals INTEGER NOT NULL DEFAULT 0,
                false_positives INTEGER NOT NULL DEFAULT 0,
                findings_json TEXT NOT NULL DEFAULT '[]',
                model_used TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO audit_results (audited_at, quality_score, findings_json) VALUES (?, ?, ?)",
            ("2026-03-25 00:00:00", 0.7, '["bad"]'),
        )
        conn.execute(
            "INSERT INTO audit_results (audited_at, quality_score, findings_json) VALUES (?, ?, ?)",
            ("2026-03-25 01:00:00", 0.9, '["good"]'),
        )
        conn.commit()

    response = client.get("/api/audit")
    assert response.status_code == 200
    data = response.get_json()
    assert data["latest"]["quality_score"] == 0.9
    assert data["latest"]["findings_json"] == ["good"]
    assert len(data["history"]) == 2
    assert data["history"][0]["quality_score"] == 0.7  # Oldest first in history


def test_spans_returns_waterfall(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL UNIQUE,
                parent_span_id TEXT,
                operation TEXT NOT NULL,
                component TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                attributes TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO spans (trace_id, span_id, operation, component, start_ms, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "s1", "op1", "react", 1000, 100),
        )
        conn.execute(
            "INSERT INTO spans (trace_id, span_id, operation, component, start_ms, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "s2", "op2", "tool", 1100, 200),
        )
        conn.commit()

    response = client.get("/api/spans")
    assert response.status_code == 200
    data = response.get_json()
    assert data["trace_id"] == "t1"
    assert len(data["spans"]) == 2
    assert data["total_duration_ms"] == 300
    assert data["spans"][0]["offset_ms"] == 0
    assert data["spans"][1]["offset_ms"] == 100


def test_spans_empty_returns_gracefully(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS spans")
        conn.commit()
    response = client.get("/api/spans")
    assert response.status_code == 200
    assert response.get_json() == {"trace_id": None, "spans": []}


def test_observation_cycles_returns_list(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observation_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                last_signal_id INTEGER NOT NULL DEFAULT 0,
                signals_processed INTEGER NOT NULL DEFAULT 0,
                actions_taken TEXT NOT NULL DEFAULT '[]',
                role_used TEXT NOT NULL DEFAULT 'review',
                degraded INTEGER NOT NULL DEFAULT 0,
                error_log TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO observation_cycles (started_at, signals_processed, error_log) VALUES (?, ?, ?)",
            ("2026-03-25 00:00:00", 5, '["err1", "err2"]'),
        )
        conn.commit()

    response = client.get("/api/cycles")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 1
    assert data[0]["error_count"] == 2


def test_recent_prefers_session_turns(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                query TEXT NOT NULL,
                answer TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("old user", "old bot", "2026-03-24 00:00:00"),
        )
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer, created_at) VALUES (?, ?, ?, ?, ?)",
            ("turn1", "sess1", "new query", "new answer", "2026-03-25 00:00:00"),
        )
        conn.commit()

    response = client.get("/api/recent")
    assert response.status_code == 200
    data = response.get_json()
    # Should have 2 entries from session_turns
    assert len(data) == 2
    assert data[0]["content"] == "new query"
    assert data[1]["content"] == "new answer"
