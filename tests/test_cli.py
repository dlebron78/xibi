import json
from unittest.mock import patch

import pytest

from xibi.cli import main
from xibi.executor import LocalHandlerExecutor
from xibi.skills.registry import SkillRegistry
from xibi.types import ReActResult, Step


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


def test_cli_control_plane_routes(mock_registry, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["hi", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.run") as mock_run,
        patch("xibi.session.SessionContext.is_continuation", return_value=False),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[control] greet: Hello! How can I help?" in out
        assert "(via:control" in out
        mock_run.assert_not_called()


def test_cli_shadow_direct_routes(mock_registry, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["check my email", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.run") as mock_run,
        patch("xibi.session.SessionContext.is_continuation", return_value=False),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[shadow:direct] list_emails" in out
        assert "(via:shadow-direct" in out
        mock_run.assert_not_called()


def test_cli_shadow_hint_routes(mock_registry, capsys):
    # Hint threshold is 0.65. "check email" should be a hint for "check my email"
    from xibi.types import ReActResult

    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["check email", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch(
            "xibi.cli.run",
            return_value=ReActResult(answer="hinted answer", steps=[], exit_reason="finish", duration_ms=100),
        ) as mock_run,
        patch("xibi.session.SessionContext.is_continuation", return_value=False),
        patch("xibi.session.SessionContext.add_turn"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[shadow:hint] list_emails" in out
        assert "(via:shadow-hint" in out
        assert "hinted answer" in out
        mock_run.assert_called_once()


def test_cli_react_fallthrough(mock_registry, capsys):
    from xibi.types import ReActResult

    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["something unknown", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch(
            "xibi.cli.run",
            return_value=ReActResult(answer="react answer", steps=[], exit_reason="finish", duration_ms=100),
        ) as mock_run,
        patch("xibi.session.SessionContext.is_continuation", return_value=False),
        patch("xibi.session.SessionContext.add_turn"),
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


def test_cli_quit_exits_cleanly(mock_registry, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "Goodbye!" in out


# ── Step 20 New Tests ─────────────────────────────────────────────────────────


def test_main_exits_on_quit(mock_registry, capsys):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["/exit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "Goodbye!" in out


def test_slash_traces_no_crash_empty(mock_registry, capsys, tmp_path):
    with (
        patch("sys.argv", ["xibi"]),
        patch("builtins.input", side_effect=["/traces", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.Tracer") as mock_tracer_cls,
    ):
        mock_tracer = mock_tracer_cls.return_value
        mock_tracer.recent_traces.return_value = []
        main()
        out, _ = capsys.readouterr()
        assert "No traces yet." in out


def test_step_callback_debug_output(mock_registry, capsys):

    # We need to capture the step_callback from the run() call or similar.
    # Since step_callback is defined inside main(), we'll patch run to call it.

    def mock_run(query, config, manifests, **kwargs):
        callback = kwargs.get("step_callback")
        if callback:
            step = Step(
                step_num=1,
                thought="Testing debug output",
                tool="email_tool",
                tool_input={"id": 123},
                tool_output={"status": "ok", "content": "hello"},
            )
            callback(step)
        return ReActResult(answer="done", steps=[], exit_reason="finish", duration_ms=10)

    with (
        patch("sys.argv", ["xibi", "--debug"]),
        patch("builtins.input", side_effect=["test", "quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("xibi.cli.run", side_effect=mock_run),
        patch("xibi.session.SessionContext.is_continuation", return_value=False),
        patch("xibi.session.SessionContext.add_turn"),
    ):
        main()
        out, _ = capsys.readouterr()
        assert "[1] email_tool" in out
        assert "thought: Testing debug output" in out
        assert "hello" in out


def test_no_spinner_flag(mock_registry):
    with (
        patch("sys.argv", ["xibi", "--no-spinner"]),
        patch("builtins.input", side_effect=["quit"]),
        patch("xibi.cli.SkillRegistry", return_value=mock_registry),
        patch("threading.Thread") as mock_thread,
    ):
        main()
        # Thread should NOT be started for the spinner if --no-spinner is set
        # (Actually the thread might be used for other things, but let's check carefully)
        for call in mock_thread.call_args_list:
            # Check if it's the spinner thread
            if call.kwargs.get("target").__name__ == "_spin":
                pytest.fail("Spinner thread started despite --no-spinner")
