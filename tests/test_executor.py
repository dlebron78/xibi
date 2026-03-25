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
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "from_mock": True}

    result = dispatch("tool", {"input": 1}, [], executor=mock_executor)
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
