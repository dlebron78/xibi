import json
from unittest.mock import MagicMock

from xibi.executor import Executor
from xibi.react import dispatch
from xibi.skills.registry import SkillRegistry


def test_execute_unknown_tool_returns_error(tmp_path):
    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("unknown", {})
    assert "Unknown tool" in result["message"]
    assert result["status"] == "error"


def test_execute_missing_tool_file_returns_error(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("tool1", {})
    assert "Tool file not found" in result["message"]
    assert result["status"] == "error"


def test_execute_calls_run_function(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): return {'status': 'ok', 'message': 'success'}")

    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("tool1", {})
    assert result == {"status": "ok", "message": "success"}


def test_execute_injects_workdir(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): return {'status': 'ok', 'workdir': params.get('_workdir')}")

    registry = SkillRegistry(tmp_path)
    workdir = tmp_path / "work"
    executor = Executor(registry, workdir=workdir)
    result = executor.execute("tool1", {})
    assert result["workdir"] == str(workdir)


def test_execute_does_not_mutate_input(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): params['added'] = True; return {'status': 'ok'}")

    registry = SkillRegistry(tmp_path)
    executor = Executor(registry, workdir="/tmp")
    tool_input = {"key": "val"}
    executor.execute("tool1", tool_input)
    assert "_workdir" not in tool_input
    assert "added" not in tool_input


def test_execute_handles_exception(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): raise ValueError('boom')")

    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("tool1", {})
    assert result["status"] == "error"
    assert "Execution error: boom" in result["message"]


def test_execute_missing_run_function(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("x = 1")

    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("tool1", {})
    assert result["status"] == "error"
    assert "missing 'run' function" in result["message"]


def test_dispatch_uses_executor_when_provided():
    from xibi.command_layer import CommandLayer

    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "from_mock": True}

    # Post-step-102: dispatch requires a command_layer to reach the executor.
    result = dispatch(
        "tool",
        {"input": 1},
        [],
        executor=mock_executor,
        command_layer=CommandLayer(interactive=True),
    )
    assert result == {"status": "ok", "from_mock": True}
    mock_executor.execute.assert_called_once_with("tool", {"input": 1})


def test_dispatch_uses_stub_when_no_executor():
    skill_registry = [{"name": "foo"}]
    result = dispatch("foo", {}, skill_registry)
    assert result == {"status": "ok", "message": "stub"}


def test_dispatch_unknown_tool_no_executor():
    result = dispatch("bar", {}, [])
    assert result["status"] == "error"
    assert "Unknown tool" in result["message"]


def test_execute_pseudo_tool_returns_error(tmp_path):
    """Pseudo-tools (finish, ask_user, error) should return an error message."""
    registry = SkillRegistry(tmp_path)
    executor = Executor(registry)
    result = executor.execute("finish", {"answer": "done"})
    assert result["status"] == "error"
    assert "Pseudo-tool" in result["message"]


def test_execute_circuit_open_blocks_tool(tmp_path):
    """When circuit is open, executor returns a circuit_open error without running tool."""
    import json as _json

    from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, FailureType

    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(_json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): return {'status': 'ok'}")

    db_path = tmp_path / "xibi.db"
    registry = SkillRegistry(tmp_path)
    executor = Executor(registry, config={"db_path": db_path})

    # Force circuit open
    breaker = CircuitBreaker("tool:tool1", db_path, CircuitBreakerConfig(failure_threshold=1))
    breaker.record_failure(FailureType.PERSISTENT)
    assert breaker.is_open()

    result = executor.execute("tool1", {})
    assert result["status"] == "error"
    assert result.get("circuit_open") is True


def test_local_handler_executor_uses_handler(tmp_path):
    """LocalHandlerExecutor should call handler.py functions when present."""
    import json as _json

    from xibi.executor import LocalHandlerExecutor

    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(_json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    (skill_dir / "handler.py").write_text("def tool1(params): return {'status': 'ok', 'via': 'handler'}")
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()

    registry = SkillRegistry(tmp_path)
    executor = LocalHandlerExecutor(registry)
    result = executor.execute("tool1", {})
    assert result["status"] == "ok"
    assert result["via"] == "handler"


def test_local_handler_executor_falls_back_to_tool(tmp_path):
    """LocalHandlerExecutor should fall back to tool file when no handler.py."""
    import json as _json

    from xibi.executor import LocalHandlerExecutor

    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(_json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "tool1.py").write_text("def run(params): return {'status': 'ok', 'via': 'tool'}")

    registry = SkillRegistry(tmp_path)
    executor = LocalHandlerExecutor(registry)
    result = executor.execute("tool1", {})
    assert result["status"] == "ok"
    assert result["via"] == "tool"


def test_local_handler_executor_unknown_handler_function(tmp_path):
    """LocalHandlerExecutor should return error when handler.py lacks the tool function."""
    import json as _json

    from xibi.executor import LocalHandlerExecutor

    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(_json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    (skill_dir / "handler.py").write_text("def other_tool(params): return {'status': 'ok'}")
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()

    registry = SkillRegistry(tmp_path)
    executor = LocalHandlerExecutor(registry)
    result = executor.execute("tool1", {})
    assert result["status"] == "error"
    assert "Unknown tool" in result["message"]


def test_local_handler_executor_injects_workdir(tmp_path):
    """LocalHandlerExecutor injects _workdir into params when workdir is set."""
    import json as _json

    from xibi.executor import LocalHandlerExecutor

    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(_json.dumps({"name": "skill1", "tools": [{"name": "tool1"}]}))
    (skill_dir / "handler.py").write_text("def tool1(params): return {'status': 'ok', 'wd': params.get('_workdir')}")
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()

    registry = SkillRegistry(tmp_path)
    executor = LocalHandlerExecutor(registry, workdir=workdir)
    result = executor.execute("tool1", {})
    assert result["status"] == "ok"
    assert result["wd"] == str(workdir)
