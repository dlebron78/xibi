import json
from unittest.mock import patch

import pytest

import sqlite3
from xibi.cli import main
from xibi.executor import LocalHandlerExecutor
from xibi.skills.registry import SkillRegistry


@pytest.fixture
def test_db(tmp_path):
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
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
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
    return db_path


@pytest.fixture
def mock_registry(tmp_path):
    skills_dir = tmp_path / "skills"
    email_dir = skills_dir / "email"
    email_dir.mkdir(parents=True)
    (email_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "email",
                "tools": [
                    {"name": "list_emails", "examples": ["check my email"], "output_type": "raw"},
                    {"name": "triage_email", "examples": ["triage my inbox"], "output_type": "raw"},
                ],
            }
        )
    )
    (email_dir / "handler.py").write_text("""
def list_emails(params):
    return {"status": "ok", "env": "dev", "emails": [{"subject": "test"}]}
def triage_email(params):
    return {"status": "ok", "env": "dev", "urgent": []}
""")

    schedule_dir = skills_dir / "schedule"
    schedule_dir.mkdir(parents=True)
    (schedule_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "schedule",
                "tools": [
                    {"name": "list_events", "examples": ["show my schedule"], "output_type": "raw"},
                    {"name": "add_event", "examples": ["add an event"], "output_type": "action"},
                ],
            }
        )
    )
    (schedule_dir / "handler.py").write_text("""
def list_events(params):
    return {"status": "ok", "env": "dev", "events": []}
def add_event(params):
    return {"status": "ok", "env": "dev", "message": "added"}
""")

    search_dir = skills_dir / "search"
    search_dir.mkdir(parents=True)
    (search_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "search",
                "tools": [{"name": "web_search", "examples": ["search for"], "output_type": "raw"}],
            }
        )
    )
    (search_dir / "handler.py").write_text("""
def web_search(params):
    return {"status": "ok", "env": "dev", "results": [{"title": params.get("query")}]}
""")
    return SkillRegistry(skills_dir)


def test_cli_control_plane_routes(mock_registry, test_db, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["hi", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch("xibi.cli.run") as mock_run,
        patch("xibi.session.get_model"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[control] greet: Hello! How can I help?" in out
        assert "(via:control" in out
        mock_run.assert_not_called()


def test_cli_shadow_direct_routes(mock_registry, test_db, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["check my email", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch("xibi.cli.run") as mock_run,
        patch("xibi.session.get_model"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[shadow:direct] list_emails" in out
        assert "(via:shadow-direct" in out
        mock_run.assert_not_called()


def test_cli_shadow_hint_routes(mock_registry, test_db, capsys):
    # Hint threshold is 0.65. "check email" should be a hint for "check my email"
    from xibi.types import ReActResult

    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["check email", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch(
            "xibi.cli.run",
            return_value=ReActResult(answer="hinted answer", steps=[], exit_reason="finish", duration_ms=100),
        ) as mock_run,
        patch("xibi.session.get_model"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[shadow:hint] list_emails" in out
        assert "(via:shadow-hint" in out
        assert "hinted answer" in out
        mock_run.assert_called_once()


def test_cli_react_fallthrough(mock_registry, test_db, capsys):
    from xibi.types import ReActResult

    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["something unknown", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch(
            "xibi.cli.run",
            return_value=ReActResult(answer="react answer", steps=[], exit_reason="finish", duration_ms=100),
        ) as mock_run,
        patch("xibi.session.get_model"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "(via:react" in out
        assert "react answer" in out
        mock_run.assert_called_once()


def test_list_emails_handler(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("list_emails", {"max_results": 1})
    assert res["status"] == "ok"
    assert res["env"] == "dev"
    assert "emails" in res


def test_triage_email_handler(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("triage_email", {})
    assert res["status"] == "ok"
    assert res["env"] == "dev"
    assert "urgent" in res


def test_list_events_handler(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("list_events", {})
    assert res["status"] == "ok"
    assert res["env"] == "dev"
    assert "events" in res


def test_add_event_handler(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("add_event", {"title": "meeting", "time": "noon"})
    assert res["status"] == "ok"
    assert res["env"] == "dev"
    assert "added" in res["message"]


def test_web_search_handler(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("web_search", {"query": "test query"})
    assert res["status"] == "ok"
    assert res["env"] == "dev"
    assert res["results"][0]["title"] == "test query"


def test_local_handler_executor_dispatch(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("list_emails", {})
    assert res["status"] == "ok"


def test_local_handler_executor_unknown_tool(mock_registry):
    executor = LocalHandlerExecutor(mock_registry)
    res = executor.execute("non_existent_tool", {})
    assert res["status"] == "error"
    assert "Unknown tool" in res["message"]


def test_cli_quit_exits_cleanly(mock_registry, test_db, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "Goodbye!" in out
    # If it didn't crash, it exited cleanly.


def test_slash_traces_no_crash_empty(mock_registry, test_db, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["/traces", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch("xibi.tracing.Tracer.recent_traces", return_value=[]),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "No traces yet." in out


def test_step_callback_debug_output(mock_registry, test_db, capsys):
    from xibi.types import ReActResult, Step

    # We need to capture what's passed to run to get the step_callback
    def mock_run_impl(
        query,
        config,
        manifests,
        executor,
        control_plane,
        shadow,
        step_callback,
        session_context,
        tracer,
    ):
        step = Step(
            step_num=1,
            thought="I should list emails",
            tool="list_emails",
            tool_input={"max_results": 5},
            tool_output={"status": "ok", "emails": []},
        )
        step_callback(step)
        return ReActResult(answer="done", steps=[step], exit_reason="finish", duration_ms=100)

    with (
        patch("sys.argv", ["xibi", "--debug"]),
        patch("builtins.input", side_effect=["some query", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch("xibi.cli.run", side_effect=mock_run_impl),
        patch("xibi.session.get_model"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[1] list_emails" in out
        assert "thought: I should list emails" in out
        assert "← {\"status\": \"ok\", \"emails\": []}" in out


def test_no_spinner_flag(mock_registry, test_db):
    from xibi.types import ReActResult

    with (
        patch("sys.argv", ["xibi", "--no-spinner"]),
        patch("builtins.input", side_effect=["some query", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.load_config_with_env_fallback", return_value={"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}, "providers": {"ollama": {"base_url": "http://localhost:11434"}}, "db_path": test_db}),
        patch(
            "xibi.cli.run",
            return_value=ReActResult(answer="hi", steps=[], exit_reason="finish", duration_ms=100),
        ),
        patch("threading.Thread") as mock_thread,
        patch("xibi.session.get_model"),
        patch("sys.stderr.isatty", return_value=True),
    ):
        main()
        mock_thread.assert_not_called()
