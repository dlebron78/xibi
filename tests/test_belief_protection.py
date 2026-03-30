"""
Tests for step-38: Belief Protection — session source tagging and compress_to_beliefs filtering.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from xibi.session import SessionContext
from xibi.types import ReActResult, Step

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(tool_names: list[str]) -> ReActResult:
    """Build a minimal ReActResult whose steps call the given tools."""
    steps = []
    for i, name in enumerate(tool_names):
        step = MagicMock(spec=Step)
        step.tool = name
        step.step_num = i + 1
        step.error = None
        steps.append(step)
    result = MagicMock(spec=ReActResult)
    result.steps = steps
    result.answer = "ok"
    result.exit_reason = "finish"
    return result


def _make_skill_registry(tool_to_skill: dict[str, str]) -> MagicMock:
    """Return a mock SkillRegistry that maps tool names to skill names."""
    registry = MagicMock()
    registry.find_skill_for_tool.side_effect = lambda name: tool_to_skill.get(name)
    return registry


def _make_session(tmp_path: Path) -> SessionContext:
    """Create a real SessionContext backed by a migrated temp DB."""
    db_path = tmp_path / "test.db"
    from xibi.db.migrations import migrate
    migrate(db_path)
    return SessionContext("test-session-1", db_path)


# ---------------------------------------------------------------------------
# Source tagging — _detect_mcp_source
# ---------------------------------------------------------------------------

class TestDetectMcpSource:
    """Tests for TelegramAdapter._detect_mcp_source()."""

    def _make_adapter(self, tool_to_skill: dict[str, str]) -> Any:
        """Minimal TelegramAdapter stand-in with only the parts _detect_mcp_source needs."""
        from xibi.channels.telegram import TelegramAdapter
        adapter = object.__new__(TelegramAdapter)
        adapter.skill_registry = _make_skill_registry(tool_to_skill)
        return adapter

    def test_returns_user_when_no_mcp_tools(self):
        adapter = self._make_adapter({"recall": "memory_skill", "list_events": "calendar_skill"})
        result = _make_result(["recall", "list_events", "finish"])
        assert adapter._detect_mcp_source(result) == "user"

    def test_returns_mcp_tag_when_mcp_tool_called(self):
        adapter = self._make_adapter({"brave_search": "mcp_brave"})
        result = _make_result(["brave_search", "finish"])
        assert adapter._detect_mcp_source(result) == "mcp:brave"

    def test_multiple_servers_sorted_comma_separated(self):
        adapter = self._make_adapter({
            "gh_list_issues": "mcp_github",
            "brave_search": "mcp_brave",
        })
        result = _make_result(["gh_list_issues", "brave_search", "finish"])
        assert adapter._detect_mcp_source(result) == "mcp:brave,github"

    def test_finish_step_not_counted(self):
        adapter = self._make_adapter({})
        result = _make_result(["finish"])
        assert adapter._detect_mcp_source(result) == "user"

    def test_empty_steps_returns_user(self):
        adapter = self._make_adapter({})
        result = _make_result([])
        assert adapter._detect_mcp_source(result) == "user"

    def test_never_raises_on_registry_error(self):
        adapter = self._make_adapter({})
        adapter.skill_registry.find_skill_for_tool.side_effect = RuntimeError("kaboom")
        result = _make_result(["some_tool"])
        # Must not raise — defaults to "user"
        assert adapter._detect_mcp_source(result) == "user"

    def test_deduplicates_same_server(self):
        """Two calls to tools from the same MCP server → server appears once."""
        adapter = self._make_adapter({
            "brave_search": "mcp_brave",
            "brave_news": "mcp_brave",
        })
        result = _make_result(["brave_search", "brave_news"])
        assert adapter._detect_mcp_source(result) == "mcp:brave"


# ---------------------------------------------------------------------------
# Belief compression — source filter
# ---------------------------------------------------------------------------

class TestCompressToBeliefs:
    """Tests for compress_to_beliefs() source filtering."""

    def _seed_turns(self, db_path: Path, turns: list[dict]) -> None:
        """Insert rows directly into session_turns."""
        conn = sqlite3.connect(db_path)
        for t in turns:
            conn.execute(
                """
                INSERT INTO session_turns
                    (session_id, turn_id, query, answer, source, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (t["session_id"], t["turn_id"], t["query"], t["answer"], t["source"]),
            )
        conn.commit()
        conn.close()

    def test_skips_mcp_turns(self, tmp_path):
        session = _make_session(tmp_path)
        self._seed_turns(tmp_path / "test.db", [
            {"session_id": session.session_id, "turn_id": "t1",
             "query": "user question", "answer": "user answer", "source": "user"},
            {"session_id": session.session_id, "turn_id": "t2",
             "query": "mcp question", "answer": "injected mcp content", "source": "mcp:brave"},
        ])

        captured_prompt: list[str] = []

        mock_llm = MagicMock()
        mock_llm.generate.side_effect = lambda p, **_: (
            captured_prompt.append(p) or
            json.dumps({"beliefs": [{"key": "k", "value": "v", "confidence": 0.9}]})
        )

        with patch("xibi.session.get_model", return_value=mock_llm):
            session.compress_to_beliefs()

        assert mock_llm.generate.called
        prompt_text = captured_prompt[0]
        assert "user question" in prompt_text
        assert "injected mcp content" not in prompt_text

    def test_includes_only_user_turns(self, tmp_path):
        session = _make_session(tmp_path)
        self._seed_turns(tmp_path / "test.db", [
            {"session_id": session.session_id, "turn_id": "t1",
             "query": "first user q", "answer": "first user a", "source": "user"},
            {"session_id": session.session_id, "turn_id": "t2",
             "query": "mcp q", "answer": "mcp a", "source": "mcp:filesystem"},
            {"session_id": session.session_id, "turn_id": "t3",
             "query": "second user q", "answer": "second user a", "source": "user"},
        ])

        captured_prompt: list[str] = []

        mock_llm = MagicMock()
        mock_llm.generate.side_effect = lambda p, **_: (
            captured_prompt.append(p) or
            json.dumps({"beliefs": [{"key": "k", "value": "v", "confidence": 0.9}]})
        )

        with patch("xibi.session.get_model", return_value=mock_llm):
            session.compress_to_beliefs()

        prompt_text = captured_prompt[0]
        assert "first user q" in prompt_text
        assert "second user q" in prompt_text
        assert "mcp a" not in prompt_text

    def test_add_turn_source_persisted(self, tmp_path):
        """add_turn with source='mcp:brave' stores the correct value in the DB."""
        session = _make_session(tmp_path)
        result = _make_result([])
        result.answer = "hello"
        result.exit_reason = "finish"
        result.steps = []
        result.duration_ms = 100

        with patch("xibi.session.get_model"):
            session.add_turn("test query", result, source="mcp:brave")

        conn = sqlite3.connect(tmp_path / "test.db")
        row = conn.execute(
            "SELECT source FROM session_turns WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "mcp:brave"
