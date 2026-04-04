from __future__ import annotations

import pytest
import sqlite3
import json
from pathlib import Path
from unittest.mock import MagicMock
from xibi.channels.telegram import TelegramAdapter
from xibi.db import open_db

def test_session_start_shows_blocked_actions(tmp_path):
    db_path = tmp_path / "test_xibi.db"
    # Setup access_log with source_bumped=1
    with open_db(db_path) as conn, conn:
        conn.execute("CREATE TABLE access_log (chat_id TEXT, authorized INTEGER, user_name TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, prev_step_source TEXT, source_bumped INTEGER, effective_tier TEXT)")
        conn.execute("CREATE TABLE session_turns (session_id TEXT, created_at DATETIME)")

        payload = json.dumps({"tool_input": {"to": "external@example.com"}})
        conn.execute(
            "INSERT INTO access_log (chat_id, authorized, user_name, prev_step_source, source_bumped, effective_tier) VALUES (?, ?, ?, ?, ?, ?)",
            ("tool:send_email", 1, payload, "mcp:gmail", 1, "red")
        )

    adapter = TelegramAdapter(MagicMock(), MagicMock(), token="fake", db_path=db_path)
    review = adapter._get_decision_review()
    assert "While you were away" in review
    assert "Held for review: send_email to external@example.com" in review

def test_session_start_empty_when_nothing_blocked(tmp_path):
    db_path = tmp_path / "test_xibi.db"
    with open_db(db_path) as conn, conn:
        conn.execute("CREATE TABLE access_log (chat_id TEXT, authorized INTEGER, user_name TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, prev_step_source TEXT, source_bumped INTEGER, effective_tier TEXT)")

    adapter = TelegramAdapter(MagicMock(), MagicMock(), token="fake", db_path=db_path)
    review = adapter._get_decision_review()
    assert review == ""
