import contextlib
import time
from unittest.mock import MagicMock, patch

import pytest

from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, FailureType
from xibi.errors import ErrorCategory, XibiError
from xibi.executor import Executor
from xibi.react import run
from xibi.router import Config, get_timeout
from xibi.skills.registry import SkillRegistry
from xibi.types import ReActResult


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_xibi.db"


def test_tool_timeout_returns_xibi_error(db_path, tmp_path):
    # Mock skill registry
    registry = MagicMock(spec=SkillRegistry)
    registry.skills = {
        "slow_skill": MagicMock(
            path=tmp_path, name="slow_skill", manifest={"tools": [{"name": "slow_tool", "timeout_secs": 1}]}
        )
    }
    registry.find_skill_for_tool.return_value = "slow_skill"

    # Create a tool file that sleeps
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    tool_file = tool_dir / "slow_tool.py"
    tool_file.write_text("import time\ndef run(params): time.sleep(2); return {'status': 'ok'}")

    executor = Executor(registry, config={"db_path": db_path, "timeouts": {"tool_default_secs": 1}})

    result = executor.execute("slow_tool", {})
    assert result["status"] == "error"
    assert "_xibi_error" in result
    assert result["_xibi_error"].category == ErrorCategory.TIMEOUT


def test_circuit_opens_after_persistent_failures(db_path):
    breaker = CircuitBreaker("test_breaker", db_path, CircuitBreakerConfig(failure_threshold=3))

    breaker.record_failure(FailureType.PERSISTENT)
    breaker.record_failure(FailureType.PERSISTENT)
    assert not breaker.is_open()

    breaker.record_failure(FailureType.PERSISTENT)
    assert breaker.is_open()
    assert breaker.get_status()["state"] == "open"


def test_transient_failures_do_not_open_circuit(db_path):
    breaker = CircuitBreaker("test_breaker", db_path, CircuitBreakerConfig(failure_threshold=3))

    for _ in range(10):
        breaker.record_failure(FailureType.TRANSIENT)

    assert not breaker.is_open()
    assert breaker.get_status()["state"] == "closed"


def test_circuit_half_open_after_recovery_timeout(db_path):
    breaker = CircuitBreaker(
        "test_breaker", db_path, CircuitBreakerConfig(failure_threshold=1, recovery_timeout_secs=0.1)
    )
    breaker.record_failure(FailureType.PERSISTENT)
    assert breaker.is_open()

    time.sleep(0.2)
    assert not breaker.is_open()  # is_open() should transition to HALF_OPEN and return False
    assert breaker.get_status()["state"] == "half_open"


def test_circuit_closes_after_success_threshold(db_path):
    # Set high recovery_timeout_secs so it doesn't auto-open but we can control it
    breaker = CircuitBreaker(
        "test_breaker",
        db_path,
        CircuitBreakerConfig(failure_threshold=1, recovery_timeout_secs=60, success_threshold=2),
    )
    breaker.record_failure(FailureType.PERSISTENT)
    assert breaker.is_open()

    # Mock time to exceed recovery_timeout_secs
    with patch("time.time", return_value=time.time() + 61):
        assert not breaker.is_open()  # Transitions to HALF_OPEN

    breaker.record_success()
    assert breaker.get_status()["state"] == "half_open"

    breaker.record_success()
    assert breaker.get_status()["state"] == "closed"


def test_circuit_state_persists_across_instances(db_path):
    breaker1 = CircuitBreaker("shared_breaker", db_path, CircuitBreakerConfig(failure_threshold=1))
    breaker1.record_failure(FailureType.PERSISTENT)
    assert breaker1.is_open()

    breaker2 = CircuitBreaker("shared_breaker", db_path)
    assert breaker2.is_open()


def test_react_collects_error_summary():
    # Mock LLM and Executor
    llm = MagicMock()
    # First call: Tool call
    # Second call: finish
    llm.generate.side_effect = [
        '{"thought": "test", "tool": "fail_tool", "tool_input": {}}',
        '{"thought": "done", "tool": "finish", "tool_input": {"answer": "done"}}',
    ]

    executor = MagicMock()
    error = XibiError(category=ErrorCategory.TIMEOUT, message="fail", component="executor")
    executor.execute.return_value = {"status": "error", "_xibi_error": error}

    config = {"models": {"text": {"fast": {"provider": "mock", "model": "m"}}}, "providers": {"mock": {}}}

    with patch("xibi.react.get_model", return_value=llm):
        result = run("query", config, [], executor=executor)

    assert len(result.error_summary) == 1
    assert result.error_summary[0].category == ErrorCategory.TIMEOUT


def test_user_facing_failure_message_all_exit_reasons():
    res_timeout = ReActResult(answer="", steps=[], exit_reason="timeout", duration_ms=0)
    assert "took too long" in res_timeout.user_facing_failure_message()

    res_max_steps = ReActResult(answer="", steps=[], exit_reason="max_steps", duration_ms=0)
    assert "reasoning limit" in res_max_steps.user_facing_failure_message()

    err = XibiError(category=ErrorCategory.CIRCUIT_OPEN, message="blocked", component="test")
    res_error = ReActResult(answer="", steps=[], exit_reason="error", duration_ms=0, error_summary=[err])
    assert "temporarily pausing" in res_error.user_facing_failure_message()


def test_cli_prints_error_on_empty_answer(tmp_path):
    import sqlite3
    import sys

    from xibi.cli import main

    db_path = tmp_path / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_turns (
                turn_id     TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                source      TEXT NOT NULL DEFAULT 'user'
            );
            CREATE TABLE IF NOT EXISTS session_entities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                turn_id      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                value        TEXT NOT NULL,
                source_tool  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    # Mock inputs and dependencies to trigger an error path in CLI
    with (
        patch("builtins.input", side_effect=["test query", "quit"]),
        patch("xibi.cli.chat.load_config_with_env_fallback") as mock_load_config,
        patch("xibi.cli.chat.SkillRegistry"),
        patch("xibi.cli.chat.LocalHandlerExecutor"),
        patch("xibi.cli.chat.ControlPlaneRouter") as mock_cp_cls,
        patch("xibi.cli.chat.ShadowMatcher") as mock_sm_cls,
        patch("xibi.cli.chat.run") as mock_run,
        patch.object(sys, "argv", ["xibi"]),
        patch("builtins.print") as mock_print,
        patch("xibi.session.get_model"),
    ):
        mock_load_config.return_value = {"models": {}, "providers": {}, "db_path": db_path}

        mock_cp = mock_cp_cls.return_value
        mock_cp.match.return_value.matched = False

        mock_sm = mock_sm_cls.return_value
        mock_sm.match.return_value = None

        err = XibiError(category=ErrorCategory.TIMEOUT, message="Too slow", component="executor")
        mock_run.return_value = ReActResult(
            answer="", steps=[], exit_reason="timeout", duration_ms=100, error_summary=[err]
        )

        with contextlib.suppress(SystemExit, EOFError, StopIteration):
            main()

        # Just print calls for debugging if it fails
        printed_texts = [(" ".join(str(arg) for arg in call.args)) for call in mock_print.call_args_list]

        found_message = any("Too slow" in text or "That took too long" in text for text in printed_texts)
        assert found_message


def test_telegram_sends_failure_message(tmp_path):
    import sqlite3

    from xibi.channels.telegram import TelegramAdapter

    db_path = tmp_path / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_turns (
                turn_id     TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                source      TEXT NOT NULL DEFAULT 'user'
            );
            CREATE TABLE IF NOT EXISTS session_entities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                turn_id      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                value        TEXT NOT NULL,
                source_tool  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    class MockCore:
        def __init__(self):
            self.registry = MagicMock()
            self.registry.get_skill_manifests.return_value = []
            self.executor = MagicMock()
            self.config = {
                "models": {
                    "text": {
                        "fast": {"provider": "ollama", "model": "m1"},
                        "think": {"provider": "ollama", "model": "m2"},
                    }
                },
                "providers": {"ollama": {"base_url": "http://localhost:11434"}},
            }

        def process_query(self, text):
            return ""

        def _get_awaiting_task(self):
            return None

    core = MockCore()

    with (
        patch.dict("os.environ", {"XIBI_TELEGRAM_TOKEN": "test", "XIBI_SYNC_SESSION": "1"}),
        patch("xibi.channels.telegram.TelegramAdapter._api_call") as mock_api,
        patch("xibi.react.run") as mock_run,
        patch("xibi.session.get_model"),
        patch("xibi.react.get_model"),
    ):
        mock_run.return_value = ReActResult(answer="", steps=[], exit_reason="timeout", duration_ms=0)
        # mock_api return for typing action
        mock_api.return_value = {"ok": True}
        adapter = TelegramAdapter(
            config=core.config,
            skill_registry=core.registry,
            executor=core.executor,
            db_path=db_path,
        )

        # We need to mock send_message since it calls _api_call
        with (
            patch.object(adapter, "send_message") as mock_send,
            patch.object(ReActResult, "user_facing_failure_message", return_value="too long"),
        ):
            # Also mock the result answer to be empty so it falls through to failure message
            mock_run.return_value.answer = ""
            adapter._handle_text(123, "hello")

            # Check if sendMessage was called
            found = False
            for call in mock_send.call_args_list:
                text = call.args[1]
                if "too long" in text:
                    found = True
                    break
            assert found


def test_get_timeout_falls_back_to_defaults():
    config: Config = {"models": {}, "providers": {}}
    assert get_timeout(config, "tool_default_secs") == 15

    config_with_timeouts: Config = {"models": {}, "providers": {}, "timeouts": {"tool_default_secs": 100}}
    assert get_timeout(config_with_timeouts, "tool_default_secs") == 100


def test_circuit_upsert_idempotent(db_path):
    CircuitBreaker("same", db_path)
    CircuitBreaker("same", db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM circuit_breakers WHERE name='same'").fetchone()[0]
    assert count == 1
