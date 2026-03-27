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
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("hi", "hello", "2026-03-01 10:00:00"),
        )
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("hi2", "hello2", "2026-03-01 11:00:00"),
        )
        conn.execute(
            "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES (?, ?, ?)",
            ("bye", "goodbye", "2026-03-02 10:00:00"),
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
    assert response.get_json() == []


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
    assert len(data) == 3
    assert data[0]["classification"] == "URGENT"


def test_signal_pipeline_empty(client):
    response = client.get("/api/signal_pipeline")
    assert response.status_code == 200
    assert response.get_json() == {}


def test_signal_pipeline_grouped(client, db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, classification, content_preview) VALUES (?, ?, ?)",
            ("email", "URGENT", "pre1"),
        )
        conn.execute(
            "INSERT INTO signals (source, classification, content_preview) VALUES (?, ?, ?)",
            ("email", "URGENT", "pre2"),
        )
        conn.execute(
            "INSERT INTO signals (source, classification, content_preview) VALUES (?, ?, ?)",
            ("email", "DIGEST", "pre3"),
        )
        conn.commit()

    response = client.get("/api/signal_pipeline")
    assert response.status_code == 200
    data = response.get_json()
    assert data["URGENT"] == 2
    assert data["DIGEST"] == 1


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
    config = {
        "models": {
            "text": {
                "fast": {"provider": "mock", "model": "m1"}
            }
        },
        "providers": {
            "mock": {}
        }
    }
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
