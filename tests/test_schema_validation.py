from __future__ import annotations

import json
from pathlib import Path

from xibi.react import run
from xibi.router import Config
from xibi.tools import validate_schema


def test_local_tool_valid_input_passes_validation():
    manifest_schema = {
        "path": {"type": "string"},
        "recursive": {"type": "boolean", "default": False},
    }
    tool_input = {"path": "/tmp/test", "recursive": True}
    errors = validate_schema("test_tool", tool_input, manifest_schema)
    assert errors == []


def test_local_tool_invalid_input_caught():
    manifest_schema = {
        "path": {"type": "string"},
        "count": {"type": "integer"},
    }
    # Missing path, and count is wrong type
    tool_input = {"count": "five"}
    errors = validate_schema("test_tool", tool_input, manifest_schema)
    assert "Missing required field: path" in errors
    assert "Field 'count' expected integer, got str" in errors


def test_missing_inputSchema_field_returns_empty():
    # If manifest_schema is None or empty, it should be valid
    errors = validate_schema("test_tool", {"any": "thing"}, None)
    assert errors == []
    errors = validate_schema("test_tool", {"any": "thing"}, {})
    assert errors == []


def test_all_local_skill_manifests_have_inputSchema():
    skills_dir = Path("skills")
    for manifest_path in skills_dir.glob("*/manifest.json"):
        with open(manifest_path) as f:
            manifest = json.load(f)
        for tool in manifest.get("tools", []):
            assert "inputSchema" in tool, f"Tool {tool.get('name')} in {manifest_path} missing inputSchema"
            assert isinstance(tool["inputSchema"], (dict, list)), f"inputSchema in {tool.get('name')} in {manifest_path} should be dict or list"


class MockExecutor:
    def execute(self, tool_name, tool_input):
        return {"status": "ok", "output": "success"}


def test_react_loop_validates_tool_input():
    # We need a minimal config and skill registry
    config: Config = {
        "models": {"text": {"fast": {"provider": "stub"}}},
        "providers": {"stub": {}},
    }
    skill_registry = [
        {
            "name": "test_tool",
            "inputSchema": {
                "path": {"type": "string"}
            }
        }
    ]

    # We need to mock get_model to return a model that calls our tool
    from unittest.mock import MagicMock, patch
    mock_model = MagicMock()
    # First call returns tool call, second call returns finish
    mock_model.generate.side_effect = [
        json.dumps({"thought": "calling tool", "tool": "test_tool", "tool_input": {}}), # Missing 'path'
        json.dumps({"thought": "finishing", "tool": "finish", "tool_input": {"answer": "done"}})
    ]

    from xibi.command_layer import CommandLayer

    with patch("xibi.react.get_model", return_value=mock_model):
        cl = CommandLayer(interactive=True)
        result = run(
            query="test",
            config=config,
            skill_registry=skill_registry,
            command_layer=cl,
            executor=MockExecutor()
        )

        # The loop should have seen a validation error and retried or recorded it.
        # In react.py, if validation fails, it returns {"status": "error", "message": result.retry_hint, "retry": True}
        # which is then stored in step.tool_output.
        assert len(result.steps) >= 2
        # First step should be the failed tool call
        assert result.steps[0].tool == "test_tool"
        assert "Schema validation failed" in result.steps[0].tool_output["message"]
        assert result.steps[0].tool_output["retry"] is True
