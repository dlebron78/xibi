from __future__ import annotations

from unittest.mock import MagicMock

from xibi.command_layer import CommandLayer
from xibi.react import dispatch


SKILL_REGISTRY = [
    {
        "name": "skill1",
        "tools": [
            {"name": "send_email", "inputSchema": {"properties": {}, "required": []}},
            {"name": "get_weather", "inputSchema": {"properties": {}, "required": []}},
            {"name": "draft_email", "inputSchema": {"properties": {}, "required": []}},
        ],
    }
]


def test_dispatch_without_command_layer_fails_closed():
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok"}

    response = dispatch("get_weather", {}, SKILL_REGISTRY, executor=mock_executor, command_layer=None)

    assert response["status"] == "blocked"
    assert response.get("fail_closed") is True
    mock_executor.execute.assert_not_called()


def test_dispatch_red_tier_blocked_when_not_interactive():
    mock_executor = MagicMock()
    layer = CommandLayer(interactive=False)

    response = dispatch(
        "send_email",
        {"recipient": "x@y.com"},
        SKILL_REGISTRY,
        executor=mock_executor,
        command_layer=layer,
    )

    assert response["status"] == "blocked"
    mock_executor.execute.assert_not_called()


def test_dispatch_red_tier_permitted_when_interactive():
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "sent": True}
    layer = CommandLayer(interactive=True)

    response = dispatch(
        "send_email",
        {"recipient": "x@y.com"},
        SKILL_REGISTRY,
        executor=mock_executor,
        command_layer=layer,
    )

    assert response.get("status") == "ok"
    mock_executor.execute.assert_called_once()


def test_dispatch_green_tier_unchanged():
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "weather": "sunny"}
    layer = CommandLayer(interactive=False)

    response = dispatch("get_weather", {}, SKILL_REGISTRY, executor=mock_executor, command_layer=layer)

    # get_weather is unlisted → resolves GREEN under DEFAULT_TIER=GREEN
    assert response["status"] == "ok"
    mock_executor.execute.assert_called_once_with("get_weather", {})


def test_dispatch_yellow_tier_audited(tmp_path):
    import sqlite3

    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            authorized INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_name TEXT,
            prev_step_source TEXT,
            source_bumped INTEGER NOT NULL DEFAULT 0,
            base_tier TEXT,
            effective_tier TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "draft_id": "d1"}
    layer = CommandLayer(db_path=str(db_path), interactive=False)

    response = dispatch(
        "draft_email",
        {"thread_id": "t1", "category": "email", "refs": []},
        SKILL_REGISTRY,
        executor=mock_executor,
        command_layer=layer,
    )

    assert response["status"] == "ok"
    mock_executor.execute.assert_called_once()

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT chat_id, authorized, effective_tier FROM access_log"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "tool:draft_email"
    assert row[1] == 1
    assert "yellow" in row[2].lower()


def test_dispatch_check_raises_returns_blocked(monkeypatch):
    """If check() itself raises (bypassing its own try/except somehow), the
    fail-closed path inside check() handles it and dispatch observes a
    blocked result. This is the dispatch-level view of the C2 test.
    """
    mock_executor = MagicMock()
    layer = CommandLayer(interactive=True)

    def boom(*_a, **_kw):
        raise RuntimeError("dedup boom")

    monkeypatch.setattr("xibi.command_layer.CommandLayer._check_dedup", boom)

    response = dispatch(
        "get_weather",
        {},
        SKILL_REGISTRY,
        executor=mock_executor,
        command_layer=layer,
    )

    assert response["status"] == "blocked"
    assert "CommandLayer internal error" in response["message"]
    mock_executor.execute.assert_not_called()
