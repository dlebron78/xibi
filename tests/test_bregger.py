"""
tests/test_bregger.py — Bregger automated test suite.

Runs entirely locally: no Ollama, no NucBox, no network.
Uses BREGGER_MOCK_ROUTER=1 and an in-memory/temp SQLite database.

Run with:
    pytest tests/test_bregger.py -v
"""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# Point at project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["BREGGER_MOCK_ROUTER"] = "1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workdir(tmp_path):
    """Create a temp workdir with the Bregger DB schema + skills."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "bregger.db"

    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS beliefs (
                key TEXT,
                value TEXT,
                type TEXT,
                visibility TEXT DEFAULT 'user',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_until DATETIME,
                PRIMARY KEY (key, created_at)
            );
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                intent TEXT,
                plan TEXT,
                act_results TEXT,
                status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id TEXT PRIMARY KEY,
                category TEXT,
                content TEXT,
                entity TEXT,
                status TEXT,
                due TEXT,
                notes TEXT,
                decay_days INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pinned_topics (
                topic TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    # Copy skills dir into tmp so SkillRegistry can load
    skills_src = PROJECT_ROOT / "skills"
    skills_dst = tmp_path / "skills"
    shutil.copytree(skills_src, skills_dst)

    os.environ["BREGGER_WORKDIR"] = str(tmp_path)
    yield tmp_path
    del os.environ["BREGGER_WORKDIR"]


@pytest.fixture
def core(tmp_workdir):
    """BreggerCore instance backed by temp workdir."""
    # Write a minimal config.json
    config = {
        "llm": {"chat_model": "gemma2:9b", "triage_model": "gemma2:9b", "url": "http://localhost:11434/api/generate"}
    }
    config_path = tmp_workdir / "config.json"
    config_path.write_text(json.dumps(config))

    # Override skills path in env
    from bregger_core import BreggerCore

    c = BreggerCore(str(config_path))
    c.db_path = tmp_workdir / "data" / "bregger.db"
    c.registry.skills_dir = tmp_workdir / "skills"
    c.registry.load_skills()
    return c


# ---------------------------------------------------------------------------
# Group 1: Control Plane (KeywordRouter)
# ---------------------------------------------------------------------------


class TestControlPlane:
    def setup_method(self):
        from bregger_core import KeywordRouter

        self.router = KeywordRouter()

    def _intent(self, text):
        result = self.router.match(text)
        return result["intent"] if result else None

    # Status
    def test_ping(self):
        assert self._intent("ping") == "status_check"

    def test_status(self):
        assert self._intent("status") == "status_check"

    def test_are_you_running(self):
        # 'you up?' is not in KeywordRouter patterns — confirm it falls through
        assert self._intent("you up?") is None

    # Negative cases
    def test_search_email_does_not_match_search_web(self):
        """'search my emails' should NOT route to search_web (domain noun guard)."""
        result = self.router.match("search my emails")
        assert result is None or result.get("intent") != "search_web"

    def test_search_inbox_does_not_match_search_web(self):
        """'search my inbox' should NOT route to search_web."""
        result = self.router.match("search my inbox")
        assert result is None or result.get("intent") != "search_web"

    def test_search_reminder_does_not_match_search_web(self):
        """'search for reminders' should NOT route to search_web."""
        result = self.router.match("search for reminders")
        assert result is None or result.get("intent") != "search_web"

    def test_empty_input(self):
        assert self.router.match("") is None

    def test_ambiguous_does_not_match(self):
        assert self.router.match("what do you think about life?") is None


# ---------------------------------------------------------------------------
# Group 2: Executive (validate_plan)
# ---------------------------------------------------------------------------


class TestExecutive:
    def setup_method(self):
        from bregger_core import BreggerExecutive, SkillRegistry

        skills_dir = PROJECT_ROOT / "skills"
        self.registry = SkillRegistry(str(skills_dir))
        self.exec = BreggerExecutive(self.registry)
        self.exec.workdir = Path("/tmp")  # dummy workdir for injection tests

    def test_valid_email_list(self):
        assert self.exec.validate_plan({"skill": "email", "tool": "list_unread", "intent": "check_email"})

    def test_valid_email_send(self):
        assert self.exec.validate_plan({"skill": "email", "tool": "send_email", "intent": "send_email"})

    def test_valid_memory_remember(self):
        assert self.exec.validate_plan({"skill": "memory", "tool": "remember", "intent": "remember"})

    def test_valid_memory_recall(self):
        assert self.exec.validate_plan({"skill": "memory", "tool": "recall", "intent": "recall"})

    def test_valid_search(self):
        assert self.exec.validate_plan({"skill": "search", "tool": "search_searxng", "intent": "search_web"})

    def test_unknown_skill(self):
        assert not self.exec.validate_plan({"skill": "calendar", "tool": "create", "intent": "add_event"})

    def test_unknown_tool(self):
        assert not self.exec.validate_plan({"skill": "email", "tool": "delete_all", "intent": "nuke_inbox"})

    def test_no_skill_plan(self):
        assert self.exec.validate_plan({"skill": "none", "tool": "none", "intent": "greet"})

    def test_fallback_normalizes_to_none(self):
        plan = {"skill": "fallback", "tool": "none", "intent": "fallback"}
        assert self.exec.validate_plan(plan)
        assert plan["skill"] == "none"

    def test_workdir_injected_into_params(self, tmp_path):
        """BreggerExecutive injects _workdir into skill params at execution time."""
        from bregger_core import BreggerExecutive, SkillRegistry

        registry = SkillRegistry(str(PROJECT_ROOT / "skills"))
        exec_ = BreggerExecutive(registry)
        exec_.workdir = tmp_path
        # Plan that would call remember (we just check the injection, not the DB write)
        plan = {"skill": "memory", "tool": "remember", "parameters": {"content": "test injection"}}
        # The call will fail because tmp_path/data/bregger.db doesn't exist,
        # but the _workdir key must have been injected
        result = exec_.execute_plan(plan)
        # Error is expected (no DB), but it should NOT be a wrong-path error for ~/.bregger
        assert ".bregger" not in result.get("message", "")


# ---------------------------------------------------------------------------
# Group 3: Working Memory
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    def test_table_creation(self, tmp_workdir):
        """_ensure_conversation_history_table creates the table."""
        from bregger_core import BreggerCore

        config = {"llm": {"chat_model": "gemma2:9b", "url": "http://localhost:11434/api/generate"}}
        config_path = tmp_workdir / "config.json"
        config_path.write_text(json.dumps(config))
        c = BreggerCore(str(config_path))
        c.db_path = tmp_workdir / "data" / "bregger.db"
        c._ensure_conversation_history_table()
        with sqlite3.connect(c.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_history'"
            ).fetchall()
        assert len(rows) == 1

    def test_log_and_retrieve(self, core):
        core._log_conversation("Hello", "Hi there!")
        history = core._get_recent_history(n=10)
        assert "Hello" in history
        assert "Hi there!" in history

    def test_history_order_oldest_first(self, core):
        core._log_conversation("First message", "First response")
        core._log_conversation("Second message", "Second response")
        history = core._get_recent_history(n=10)
        # Should be oldest first: First message appears before Second message
        first_pos = history.find("First message")
        second_pos = history.find("Second message")
        assert first_pos != -1 and second_pos != -1
        assert first_pos < second_pos

    def test_history_limit(self, core):
        for i in range(15):
            core._log_conversation(f"msg {i}", f"resp {i}")
        history = core._get_recent_history(n=5)
        # n=5 fetches the 5 most recent by id DESC (msg 14..10), then reverses
        assert "msg 14" in history
        assert "msg 0" not in history

    def test_empty_history_returns_empty_string(self, core):
        assert core._get_recent_history(n=10) == ""

    def test_context_includes_history(self, core):
        core._log_conversation("What is 2+2?", "4")
        context = core._get_user_context()
        assert "RECENT CONVERSATION" in context
        assert "What is 2+2?" in context

    def test_message_truncation(self, core):
        long_msg = "x" * 2000
        long_resp = "y" * 5000
        core._log_conversation(long_msg, long_resp)  # should not raise
        with sqlite3.connect(core.db_path) as conn:
            row = conn.execute("SELECT user_message, bot_response FROM conversation_history").fetchone()
        assert len(row[0]) <= 1000
        assert len(row[1]) <= 2000


# ---------------------------------------------------------------------------
# Group 4: Skill Tools
# ---------------------------------------------------------------------------


class TestConversationRecall:
    def test_recall_empty_history(self, core):
        from skills.memory.tools import recall_conversation
        from xibi.db import init_workdir
        init_workdir(Path(core.db_path.parent.parent))

        # Test empty
        res = recall_conversation.run({"query": "apple", "_workdir": core.db_path.parent.parent.as_posix()})
        assert res["status"] == "success"
        assert len(res["turns"]) == 0

    def test_recall_keyword_match(self, core):
        import sqlite3
        from xibi.db import init_workdir
        init_workdir(Path(core.db_path.parent.parent))

        from skills.memory.tools import recall_conversation

        db_path = core.db_path.parent / "xibi.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES ('hello apple', 'hi', '2024-01-01 10:00:00')"
            )
            conn.execute(
                "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES ('hello banana', 'hi', '2024-01-02 10:00:00')"
            )

        res = recall_conversation.run({"query": "banana", "_workdir": core.db_path.parent.parent.as_posix()})
        assert res["status"] == "success"
        assert len(res["turns"]) == 1
        assert "banana" in res["turns"][0]["user_message"]

    def test_recall_limit(self, core):
        import sqlite3
        from xibi.db import init_workdir
        init_workdir(Path(core.db_path.parent.parent))

        from skills.memory.tools import recall_conversation

        db_path = core.db_path.parent / "xibi.db"
        with sqlite3.connect(db_path) as conn:
            for i in range(15):
                conn.execute(
                    f"INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES ('test orange {i}', 'hi', '2024-01-01 10:00:00')"
                )

        res = recall_conversation.run(
            {"query": "orange", "limit": 2, "_workdir": core.db_path.parent.parent.as_posix()}
        )
        assert res["status"] == "success"
        assert len(res["turns"]) == 2

    def test_recall_no_match(self, core):
        import sqlite3
        from xibi.db import init_workdir
        init_workdir(Path(core.db_path.parent.parent))

        from skills.memory.tools import recall_conversation

        with sqlite3.connect(core.db_path) as conn:
            conn.execute(
                "INSERT INTO conversation_history (user_message, bot_response, created_at) VALUES ('hello apple', 'hi', '2024-01-01 10:00:00')"
            )

        res = recall_conversation.run({"query": "zebra", "_workdir": core.db_path.parent.parent.as_posix()})
        assert res["status"] == "success"
        assert len(res["turns"]) == 0


class TestMemorySkill:
    def _db(self, tmp_workdir):
        return tmp_workdir / "data" / "xibi.db"

    def test_remember_basic(self, tmp_workdir):
        from skills.memory.tools import remember
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        result = remember.run({"content": "buy milk", "_workdir": str(tmp_workdir)})
        assert result["status"] == "success", result.get("message")

    def test_remember_with_category(self, tmp_workdir):
        from skills.memory.tools import remember
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        result = remember.run({"content": "Call Bob", "category": "task", "_workdir": str(tmp_workdir)})
        assert result["status"] == "success"
        db = self._db(tmp_workdir)
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT category FROM ledger WHERE content='Call Bob'").fetchone()
        assert row is not None
        assert row[0] == "task"

    def test_recall_all(self, tmp_workdir):
        from skills.memory.tools import recall, remember
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        remember.run({"content": "item one", "_workdir": str(tmp_workdir)})
        remember.run({"content": "item two", "_workdir": str(tmp_workdir)})
        result = recall.run({"_workdir": str(tmp_workdir)})
        assert result["status"] == "success"
        assert len(result["items"]) >= 2

    def test_recall_by_category(self, tmp_workdir):
        from skills.memory.tools import recall, remember
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        remember.run({"content": "design spec", "category": "idea", "_workdir": str(tmp_workdir)})
        remember.run({"content": "grocery run", "category": "task", "_workdir": str(tmp_workdir)})
        result = recall.run({"category": "idea", "_workdir": str(tmp_workdir)})
        assert all(i["category"] == "idea" for i in result["items"])

    def test_recall_by_keyword(self, tmp_workdir):
        from skills.memory.tools import recall, remember
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        remember.run({"content": "order milk from store", "_workdir": str(tmp_workdir)})
        result = recall.run({"query": "milk", "_workdir": str(tmp_workdir)})
        assert any("milk" in i["content"] for i in result["items"])

    def test_recall_empty(self, tmp_workdir):
        from skills.memory.tools import recall
        from xibi.db import init_workdir
        init_workdir(tmp_workdir)

        result = recall.run({"query": "xyznonexistent99", "_workdir": str(tmp_workdir)})
        assert result["status"] == "success"
        assert result["items"] == []


class TestEmailSkill:
    def test_send_email_missing_params(self):
        from skills.email.tools import send_email

        result = send_email.run({"to": "test@example.com"})  # missing subject + body
        assert result["status"] == "error"
        assert "subject" in result["message"] and "body" in result["message"]

    def test_send_email_no_himalaya(self, monkeypatch):
        """When himalaya is not installed, return a clear error."""
        import shutil as sh

        monkeypatch.setattr(sh, "which", lambda x: None)
        from skills.email.tools import send_email

        result = send_email.run({"to": "bob@example.com", "subject": "Hello", "body": "Test body"})
        assert result["status"] == "error"

    def test_executive_validates_send_email(self):
        from bregger_core import BreggerExecutive, SkillRegistry

        registry = SkillRegistry(str(PROJECT_ROOT / "skills"))
        exec_ = BreggerExecutive(registry)
        assert exec_.validate_plan({"skill": "email", "tool": "send_email", "intent": "send_email"})


# ---------------------------------------------------------------------------
# Group 5: Status Response
# ---------------------------------------------------------------------------


class TestStatusFormat:
    def test_status_shows_model(self, core):
        status = core._format_status()
        assert "gemma2:9b" in status

    def test_status_shows_skills(self, core):
        status = core._format_status()
        assert "email" in status or "memory" in status or "search" in status

    def test_status_shows_memory_count(self, core):
        core._log_conversation("hey", "hi")
        status = core._format_status()
        assert "Memory:" in status
        assert "1 turns" in status


# ---------------------------------------------------------------------------
# Group 6: ReAct Loop
# ---------------------------------------------------------------------------


class TestReActLoop:
    """Tests for the ReAct multi-step execution engine.

    All tests use MockRouter + scripted step sequences — no live LLM required.
    """

    # ── Step dataclass ────────────────────────────────────────────────

    def test_step_dataclass_defaults(self):
        """Step initializes with correct field defaults."""
        from bregger_core import Step

        s = Step(step_num=1, thought="thinking", tool="list_files", tool_input={})
        assert s.tool_output == {}
        assert s.escalated is False
        assert s.duration_ms == 0
        assert s.escalation_source == ""

    def test_step_full_text_contains_fields(self):
        from bregger_core import Step

        s = Step(
            step_num=2,
            thought="I need to read the file",
            tool="read_file",
            tool_input={"filepath": "bugs.md"},
            tool_output={"status": "success", "content": "file content here"},
        )
        text = s.full_text()
        assert "Step 2" in text
        assert "I need to read the file" in text
        assert "read_file" in text

    def test_step_one_line_summary_error(self):
        from bregger_core import Step

        s = Step(
            step_num=3,
            thought="",
            tool="send_email",
            tool_input={"to": "bad"},
            tool_output={"status": "error", "message": "Missing fields"},
        )
        summary = s.one_line_summary()
        assert "Step 3" in summary
        assert "send_email" in summary
        assert "ERROR" in summary

    # ── compress_scratchpad ───────────────────────────────────────────

    def test_compress_scratchpad_empty(self):
        from bregger_core import compress_scratchpad

        result = compress_scratchpad([], current_step=1)
        assert "No steps" in result

    def test_compress_scratchpad_recent_full(self):
        """Last 2 steps should be full text (contain 'Thought:')."""
        from bregger_core import Step, compress_scratchpad

        steps = [
            Step(1, "first", "list_files", {}, {"status": "success", "content": "a.md"}),
            Step(2, "second", "read_file", {"filepath": "a.md"}, {"status": "success", "content": "Hello"}),
        ]
        result = compress_scratchpad(steps, current_step=3)
        # Both steps are within last 2 (current_step - 1 = 2), so both should be full
        assert "Thought:" in result

    def test_compress_scratchpad_older_summary(self):
        """Step 1 should be one-liner when current_step=4."""
        from bregger_core import Step, compress_scratchpad

        steps = [
            Step(1, "old thought", "list_files", {}, {"status": "success", "content": "files here"}),
            Step(2, "mid thought", "read_file", {"filepath": "bugs.md"}, {"status": "success", "content": "bugs"}),
            Step(3, "recent thought", "recall", {"query": "name"}, {"status": "success", "items": []}),
        ]
        result = compress_scratchpad(steps, current_step=4)
        # Step 1 (step_num < current_step-1=3) should be compressed — no "Thought:" for it
        lines = result.split("\n\n")
        assert lines[0].startswith("Step 1:")  # one-liner
        assert "Thought:" in lines[-1]  # last step is full

    # ── is_repeat ────────────────────────────────────────────────────

    def test_is_repeat_detects_high_overlap(self):
        """Same tool + very similar input → repeat."""
        from bregger_core import Step, is_repeat

        prev = Step(1, "", "search", {"query": "what is the weather in miami today"}, {})
        curr = Step(2, "", "search", {"query": "what is the weather in miami"}, {})
        assert is_repeat(curr, [prev]) is True

    def test_is_repeat_different_tool(self):
        """Different tool → never a repeat."""
        from bregger_core import Step, is_repeat

        prev = Step(1, "", "search", {"query": "miami weather"}, {})
        curr = Step(2, "", "read_file", {"filepath": "miami.txt"}, {})
        assert is_repeat(curr, [prev]) is False

    def test_is_repeat_empty_scratchpad(self):
        from bregger_core import Step, is_repeat

        curr = Step(1, "", "search", {"query": "hello"}, {})
        assert is_repeat(curr, []) is False

    # ── is_confirmation ───────────────────────────────────────────────

    def test_is_confirmation_yes(self):
        from bregger_core import is_confirmation

        assert is_confirmation("yes") is True

    def test_is_confirmation_go_ahead(self):
        from bregger_core import is_confirmation

        assert is_confirmation("go ahead") is True

    def test_is_confirmation_send_it(self):
        from bregger_core import is_confirmation

        assert is_confirmation("ok send it") is True

    def test_is_confirmation_negative(self):
        from bregger_core import is_confirmation

        assert is_confirmation("what's the weather like?") is False

    def test_is_confirmation_cancel(self):
        from bregger_core import is_confirmation

        assert is_confirmation("no don't send it") is False

    # ── _get_tool_meta ────────────────────────────────────────────────

    def test_get_tool_meta_known(self, core):
        """list_files should be found and tagged with skill=filesystem."""
        meta = core._get_tool_meta("list_files")
        assert meta is not None
        assert meta["skill"] == "filesystem"
        assert meta["name"] == "list_files"

    def test_get_tool_meta_unknown(self, core):
        """Non-existent tool returns None."""
        meta = core._get_tool_meta("teleport_user")
        assert meta is None

    def test_get_tool_meta_recall(self, core):
        meta = core._get_tool_meta("recall")
        assert meta is not None
        assert meta["skill"] == "memory"

    # ── _validate_tool_input ──────────────────────────────────────────

    def test_validate_send_email_missing_to(self, core):
        meta = core._get_tool_meta("send_email") or {}
        err = core._validate_tool_input("send_email", {"subject": "Hi", "body": "Hello"}, meta)
        assert err is not None
        assert err["status"] == "error"
        assert "to" in err["message"].lower()

    def test_validate_send_email_valid(self, core):
        meta = core._get_tool_meta("send_email") or {}
        err = core._validate_tool_input("send_email", {"to": "bob@example.com", "subject": "Hi", "body": "Hello"}, meta)
        assert err is None

    def test_validate_read_file_missing_filepath(self, core):
        meta = core._get_tool_meta("read_file") or {}
        err = core._validate_tool_input("read_file", {}, meta)
        assert err is not None
        assert err["status"] == "error"

    def test_validate_read_file_valid(self, core):
        meta = core._get_tool_meta("read_file") or {}
        err = core._validate_tool_input("read_file", {"filepath": "/tmp/test.md"}, meta)
        assert err is None

    # ── _truncate_output ──────────────────────────────────────────────

    def test_truncate_output_large_content(self, core):
        from bregger_core import Step

        step = Step(1, "", "read_file", {}, {"status": "success", "content": "x" * 5000})
        meta = {"max_output_size": 100}
        core._truncate_output(step, meta)
        assert len(step.tool_output["content"]) <= 130  # 100 + "[truncated]" suffix
        assert "[truncated]" in step.tool_output["content"]

    def test_truncate_output_small_content_untouched(self, core):
        from bregger_core import Step

        step = Step(1, "", "read_file", {}, {"status": "success", "content": "small"})
        meta = {"max_output_size": 2048}
        core._truncate_output(step, meta)
        assert step.tool_output["content"] == "small"

    # ── Pending action gate ───────────────────────────────────────────

    def test_pending_action_confirmed(self, core, tmp_workdir):
        """After a pending action is stored, 'yes' executes it."""
        # Build a minimal plan that does nothing harmful (memory/recall)
        recall_plan = {
            "skill": "memory",
            "tool": "recall",
            "parameters": {"_workdir": str(tmp_workdir)},
            "output_type": "synthesis",
            "risk": "safe",
        }
        core._pending_action = recall_plan
        # Simulate confirmation — should execute the stored plan and clear the gate
        result = core.process_query("yes")
        assert core._pending_action is None
        assert result  # Got some response

    def test_pending_action_cleared_on_other_input(self, core):
        """Non-confirmation input clears the pending action without executing."""
        core._pending_action = {"skill": "email", "tool": "send_email", "parameters": {}}
        # Any non-confirmation clears it and falls through to ReAct loop
        # MockRouter will return a finish step for unknown input
        core.process_query("what time is it?")
        assert core._pending_action is None

    # ── list_files tool ───────────────────────────────────────────────

    def test_list_files_returns_workspace_files(self, tmp_workdir):
        """list_files tool returns files in workdir."""
        # Create a test file
        (tmp_workdir / "test_doc.md").write_text("# Test")
        from skills.filesystem.tools import list_files

        result = list_files.run({"_workdir": str(tmp_workdir)})
        assert result["status"] == "success"
        assert "test_doc.md" in result["content"]
        assert result["count"] >= 1

    def test_list_files_no_workdir(self):
        """list_files returns error when no workdir is set."""
        import os

        env_backup = os.environ.pop("BREGGER_WORKDIR", None)
        try:
            from skills.filesystem.tools import list_files

            result = list_files.run({})
            assert result["status"] == "error"
        finally:
            if env_backup:
                os.environ["BREGGER_WORKDIR"] = env_backup

    # ── ReAct loop: force-finish ──────────────────────────────────────

    def test_react_force_finish_on_max_steps(self, core):
        """When max_steps is reached, return a force-finish response."""
        from bregger_core import Step

        # Script 10 steps that all try a non-existent tool (will hit loop limit)
        steps = [Step(i, "still trying", "nonexistent_tool", {"x": i}) for i in range(1, 10)]
        core.router.set_step_sequence(steps)
        core.config["loop"] = {"max_steps": 3, "max_loop_seconds": 30}
        result = core.process_query("do something impossible")
        assert "wasn't able" in result.lower() or "found" in result.lower()

    def test_react_finish_step_returns_answer(self, core):
        """A finish step is returned immediately as the response."""
        from bregger_core import Step

        core.router.set_step_sequence(
            [
                Step(1, "I know the answer.", "finish", {"final_answer": "The answer is 42."}),
            ]
        )
        result = core.process_query("what is the answer?")
        assert "42" in result


# ---------------------------------------------------------------------------
# Group 5: Unified Memory Signals
# ---------------------------------------------------------------------------


class TestUnifiedMemorySignals:
    def test_chat_signals_logged(self, core):
        from bregger_core import Step

        # Mock a ReAct step with a signal
        core.router.set_step_sequence(
            [
                Step(
                    1,
                    "I will check the schedule.",
                    "finish",
                    {"final_answer": "Done"},
                    signal={"topic": "scheduling", "entity": "calendar", "entity_type": "other"},
                ),
            ]
        )
        core.process_query("check my calendar")

        with sqlite3.connect(core.db_path) as conn:
            row = conn.execute(
                "SELECT source, topic_hint, entity_text FROM signals WHERE ref_source='traces'"
            ).fetchone()

        assert row is not None
        assert row[0] == "chat"
        assert row[1] == "scheduling"
        assert row[2] == "calendar"

    def test_cross_channel_escalation(self, core):
        import sqlite3

        import bregger_heartbeat
        from bregger_heartbeat import RuleEngine

        # Seed the DB with a chat signal from today (needs >1 count to be an active thread)
        with sqlite3.connect(core.db_path) as conn:
            conn.execute(
                "INSERT INTO signals (source, topic_hint, timestamp, content_preview) VALUES ('chat', 'presentation_deck', datetime('now', '-1 hours'), 'test1')"
            )
            conn.execute(
                "INSERT INTO signals (source, topic_hint, timestamp, content_preview) VALUES ('chat', 'presentation_deck', datetime('now', '-2 hours'), 'test2')"
            )
            conn.commit()

        # Instantiate RuleEngine and ensure mock tables exist
        rules = RuleEngine(core.db_path)
        try:
            rules._ensure_table()
            rules._ensure_triage_tables()
        except Exception:
            pass

        # Confirm active threads picks it up
        active = bregger_heartbeat._get_active_threads(core.db_path)
        assert len(active) == 1
        assert active[0]["topic"] == "presentation deck"

        # Mock an email about presentation
        email = {"id": "123", "from": "boss@acme.com", "subject": "presentation deck", "body": "where is it"}

        # Monkeypatch check_email, classify_email, and is_quiet_hours just for this test
        original_check = bregger_heartbeat.check_email
        original_classify = bregger_heartbeat.classify_email
        original_quiet = bregger_heartbeat.is_quiet_hours
        try:
            bregger_heartbeat.check_email = lambda *args: [email]
            bregger_heartbeat.classify_email = lambda *args, **kwargs: "DIGEST"
            bregger_heartbeat.is_quiet_hours = lambda: False

            # We mock TelegramNotifier
            class MockNotifier:
                def send(self, msg):
                    pass

            # Call tick which processes all unread emails
            bregger_heartbeat.tick(core.db_path.parent.parent, core.db_path, MockNotifier(), rules, "gemma2:9b")

            # Now verify triage_log has URGENT
            with sqlite3.connect(core.db_path) as conn:
                row = conn.execute("SELECT verdict, subject FROM triage_log WHERE email_id='123'").fetchone()

            assert row is not None
            assert row[0] == "URGENT"
            assert "🔥" in row[1]
            assert "presentation" in row[1]

        finally:
            bregger_heartbeat.check_email = original_check
            bregger_heartbeat.classify_email = original_classify
            bregger_heartbeat.is_quiet_hours = original_quiet

    def test_pinned_topics_escalation(self, core):
        import sqlite3

        import bregger_heartbeat

        # Seed DB with a pinned topic
        with sqlite3.connect(core.db_path) as conn:
            conn.execute("INSERT INTO pinned_topics (topic) VALUES ('jetblue')")
            conn.commit()

        # Mock an email about jetblue
        email = {
            "id": "124",
            "from": "recruiter@jetblue.com",
            "subject": "flight attendant role",
            "body": "are you available",
        }

        # Monkeypatch check_email and classify_email
        original_check = bregger_heartbeat.check_email
        original_classify = bregger_heartbeat.classify_email
        original_quiet = bregger_heartbeat.is_quiet_hours
        try:
            bregger_heartbeat.check_email = lambda *args: [email]
            bregger_heartbeat.classify_email = lambda *args, **kwargs: "DIGEST"
            bregger_heartbeat.is_quiet_hours = lambda: False

            class MockNotifier:
                def send(self, msg):
                    pass

            from bregger_heartbeat import RuleEngine

            rules = RuleEngine(core.db_path)
            try:
                rules._ensure_table()
                rules._ensure_triage_tables()
            except Exception:
                pass

            rules.extract_topic_from_subject = lambda s: ("jetblue", None, None)

            bregger_heartbeat.tick(core.db_path.parent.parent, core.db_path, MockNotifier(), rules, "gemma2:9b")

            with sqlite3.connect(core.db_path) as conn:
                row = conn.execute("SELECT verdict, subject FROM triage_log WHERE email_id='124'").fetchone()

            assert row is not None
            assert row[0] == "URGENT"
            assert "📌" in row[1]
            assert "jetblue" in row[1]

        finally:
            bregger_heartbeat.check_email = original_check
            bregger_heartbeat.classify_email = original_classify
            bregger_heartbeat.is_quiet_hours = original_quiet

    def test_topic_normalization(self):
        from bregger_heartbeat import _normalize_topic

        # Stemming
        assert _normalize_topic("scheduling") == "schedule"
        assert _normalize_topic("presentation_deck") == "presentation deck"
        # Stopwords
        assert _normalize_topic("my schedule") == "schedule"
        assert _normalize_topic("the presentation") == "presentation"
        # Synonyms
        assert _normalize_topic("calendar") == "schedule"
        assert _normalize_topic("inbox") == "email"
        assert _normalize_topic("mail") == "email"

    def test_active_threads_merged(self, core):
        import sqlite3

        import bregger_heartbeat

        # Seed DB with fragmented topics that should merge to 'schedule'
        with sqlite3.connect(core.db_path) as conn:
            conn.execute(
                "INSERT INTO signals (source, topic_hint, timestamp, content_preview) VALUES ('chat', 'scheduling', datetime('now', '-1 hours'), 'test1')"
            )
            conn.execute(
                "INSERT INTO signals (source, topic_hint, timestamp, content_preview) VALUES ('chat', 'calendar', datetime('now', '-2 hours'), 'test2')"
            )
            conn.execute(
                "INSERT INTO signals (source, topic_hint, timestamp, content_preview) VALUES ('chat', 'my schedule', datetime('now', '-3 hours'), 'test3')"
            )
            conn.commit()

        # Should merge all 3 into 1 active thread
        active = bregger_heartbeat._get_active_threads(core.db_path)
        assert len(active) == 1
        assert active[0]["topic"] == "schedule"
        assert active[0]["count"] == 3


class TestSkillContract:
    def test_skill_triggers_auto_registered(self, tmp_path):
        import json
        import os

        from bregger_core import BreggerCore

        # 1. Setup mock skills dir
        workdir = tmp_path / "bregger"
        skills_dir = workdir / "skills"
        skills_dir.mkdir(parents=True)
        (workdir / "data").mkdir()

        # 2. Create mock skill
        skill_path = skills_dir / "test_skill"
        skill_path.mkdir()
        manifest = {
            "name": "test_skill",
            "description": "test",
            "control_plane_triggers": [{"regex": "^trigger me$", "intent": "test_intent", "tool": "test_tool"}],
            "tools": [{"name": "test_tool", "description": "test", "output_type": "raw"}],
        }
        with open(skill_path / "manifest.json", "w") as f:
            json.dump(manifest, f)

        # 3. Create mock config
        config = {"assistant": {"workdir": str(workdir)}, "llm": {"model": "mock"}}
        config_path = workdir / "config.json"
        with open(config_path, "w") as f:
            json.dump(config, f)

        # 4. Init core (should auto-register)
        os.environ["BREGGER_MOCK_ROUTER"] = "1"
        core = BreggerCore(str(config_path))

        # 5. Check KeywordRouter match
        match = core.control_plane.match("trigger me")
        assert match is not None
        assert match["intent"] == "test_intent"

        # 6. Check IntentMapper plan
        plan = core.intent_mapper.to_plan(match)
        assert plan is not None
        assert plan["skill"] == "test_skill"
        assert plan["tool"] == "test_tool"

    def test_intent_mapper_min_tier(self, tmp_path):

        from bregger_core import IntentMapper

        # Mock registry with a Tier 3 tool
        class MockRegistry:
            def get_tool_min_tier(self, s, t):
                return 3 if t == "complex_tool" else 1

        mapper = IntentMapper(MockRegistry())
        mapper.register("test_intent", "skill", "complex_tool")

        # Should fail closed (return None) to force Tier 3 routing
        intent_obj = {"intent": "test_intent", "entities": {}}
        plan = mapper.to_plan(intent_obj)
        assert plan is None

    def test_manifest_validator_catches_errors(self, capsys):
        from bregger_core import SkillRegistry

        # Registry with a bad manifest
        registry = SkillRegistry("/tmp/nonexistent")  # empty
        registry.skills = {
            "bad_skill": {
                "manifest": {
                    "name": "bad_skill",
                    "tools": [
                        {"name": "bad_tool", "output_type": "invalid"}  # bad output type, missing description
                    ],
                }
            }
        }

        registry.validate_manifests()
        captured = capsys.readouterr().out
        assert "Missing description" in captured
        assert "Invalid output_type" in captured
