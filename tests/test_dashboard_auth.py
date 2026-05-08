"""Tests for the dashboard ``/api/*`` API key auth gate (step-122).

The dashboard's ``before_request`` hook captures
``XIBI_DASHBOARD_API_KEY`` at app-creation time and rejects every
``/api/*`` request whose ``X-API-Key`` header does not match. HTML
page routes (``/``, ``/caretaker``) and the legacy ``/health`` route
are exempt by path prefix so a plain browser can load the dashboard.

Behavior is **fail-closed**: an unset/empty key results in 401 on
every ``/api/*`` call. There is no empty-key bypass.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xibi.dashboard import DashboardConfig, create_app


TEST_KEY = "step122-dashboard-key"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Minimal SQLite DB so dashboard queries don't error before reaching auth."""
    db_file = tmp_path / "auth_test.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME DEFAULT '2026-03-25 00:00:00'
            );
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE traces (
                id TEXT PRIMARY KEY,
                model TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT
            );
            """
        )
    return db_file


def _app(db_path: Path):
    app = create_app(DashboardConfig(db_path=db_path))
    app.config["TESTING"] = True
    return app


def test_dashboard_auth_rejects_no_key(db_path: Path, monkeypatch):
    """API request without the header returns 401."""
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    with _app(db_path).test_client() as client:
        resp = client.get("/api/signals")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "unauthorized"}


def test_dashboard_auth_rejects_wrong_key(db_path: Path, monkeypatch):
    """API request with a header that doesn't match returns 401."""
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    with _app(db_path).test_client() as client:
        resp = client.get("/api/signals", headers={"X-API-Key": "not-the-key"})
    assert resp.status_code == 401


def test_dashboard_auth_accepts_correct_key(db_path: Path, monkeypatch):
    """API request with the correct header is allowed through to the route.

    Uses ``/api/health`` because it touches only ``traces`` and
    ``schema_version`` (both seeded in the fixture); other API routes
    would require additional table setup.
    """
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    with _app(db_path).test_client() as client:
        resp = client.get("/api/health", headers={"X-API-Key": TEST_KEY})
    assert resp.status_code == 200


def test_dashboard_auth_pages_exempt(db_path: Path, monkeypatch):
    """HTML page routes (``/``, ``/caretaker``) load without an API key.

    The browser cannot inject custom headers on a top-level navigation,
    so the page itself must be accessible. The page-side JS reads the
    server-rendered key and forwards it on its own XHR calls.
    """
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    with _app(db_path).test_client() as client:
        for path in ("/", "/caretaker"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} should be exempt"


def test_dashboard_auth_fail_closed_when_key_missing(db_path: Path, monkeypatch):
    """Unset / empty ``XIBI_DASHBOARD_API_KEY`` = 401 on every ``/api/*`` call.

    Even if a client guesses an empty string for the header, the
    ``not configured`` branch fires first; there is no empty-key bypass.
    """
    monkeypatch.delenv("XIBI_DASHBOARD_API_KEY", raising=False)
    with _app(db_path).test_client() as client:
        resp_no_header = client.get("/api/signals")
        resp_empty_header = client.get("/api/signals", headers={"X-API-Key": ""})
    assert resp_no_header.status_code == 401
    assert resp_empty_header.status_code == 401


def test_dashboard_auth_empty_string_env_fails_closed(db_path: Path, monkeypatch):
    """Empty-string env var is treated identically to unset (no bypass)."""
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", "")
    with _app(db_path).test_client() as client:
        resp_empty_header = client.get("/api/signals", headers={"X-API-Key": ""})
    # Empty string == empty string would compare equal, but the
    # ``not configured`` guard should reject before the comparison.
    assert resp_empty_header.status_code == 401


def test_dashboard_api_health_gated_by_key(db_path: Path, monkeypatch):
    """``/api/health`` follows the same gate as other API routes.

    Per condition 2: the only consumer of /api/health is the dashboard
    page itself (browser XHR), which forwards the injected key.
    Deploy/CI/systemd checks use ``systemctl is-active``, not HTTP.
    """
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    with _app(db_path).test_client() as client:
        unauth = client.get("/api/health")
        authed = client.get("/api/health", headers={"X-API-Key": TEST_KEY})
    assert unauth.status_code == 401
    assert authed.status_code == 200


def test_dashboard_eschtml_present_in_index(monkeypatch):
    """The rendered ``/`` page exposes ``escHtml`` so XHR-injected data is escaped.

    This is a structural sanity check: ``escHtml`` is the entry point
    the JS uses for every user-data interpolation into innerHTML. If it
    disappears or is renamed, every innerHTML site silently regresses.
    """
    monkeypatch.setenv("XIBI_DASHBOARD_API_KEY", TEST_KEY)
    monkeypatch.setattr(
        DashboardConfig,
        "__init__",
        DashboardConfig.__init__,
    )
    db = Path("/nonexistent/db.sqlite")  # page render doesn't touch DB
    with _app(db).test_client() as client:
        resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert "function escHtml(" in body
    # Sanity: API_KEY constant and fetch wrapper present too.
    assert "const API_KEY" in body
    assert "X-API-Key" in body
