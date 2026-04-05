import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from xibi.command_layer import CommandResult
from xibi.react import run
from xibi.tools import validate_schema


def test_local_tool_valid_input_passes_validation():
    manifest_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tool_input = {"path": "/tmp/test.txt"}
    errors = validate_schema("read_file", tool_input, manifest_schema)
    assert errors == []


def test_local_tool_invalid_input_caught():
    manifest_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tool_input = {}
    errors = validate_schema("read_file", tool_input, manifest_schema)
    assert len(errors) > 0
    assert "Missing required field: path" in errors


def test_missing_input_schema_field_returns_empty():
    errors = validate_schema("some_tool", {"foo": "bar"}, None)
    assert errors == []
    errors = validate_schema("some_tool", {"foo": "bar"}, {})
    assert errors == []


def test_all_local_skill_manifests_have_input_schema():
    skills_dir = Path("skills")
    for manifest_path in skills_dir.glob("*/manifest.json"):
        with open(manifest_path) as f:
            manifest = json.load(f)
        for tool in manifest.get("tools", []):
            assert "inputSchema" in tool, f"Tool '{tool.get('name')}' in {manifest_path} missing 'inputSchema'"
            assert isinstance(tool["inputSchema"], dict), (
                f"'inputSchema' for tool '{tool.get('name')}' in {manifest_path} must be a dict"
            )
            assert tool["inputSchema"].get("type") == "object", (
                f"'inputSchema' for tool '{tool.get('name')}' in {manifest_path} must have type 'object'"
            )


def test_react_loop_validates_tool_input():
    config = {"models": {"text": {"fast": {"provider": "mock", "model": "mock-model"}}}}
    skill_registry = [
        {
            "name": "test_skill",
            "tools": [
                {
                    "name": "mock_tool",
                    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                }
            ],
        }
    ]

    mock_llm = MagicMock()
    # LLM decides to call mock_tool without required path
    mock_llm.generate.return_value = json.dumps(
        {"thought": "I will call the tool.", "tool": "mock_tool", "tool_input": {}}
    )

    import xibi.react

    original_get_model = xibi.react.get_model
    xibi.react.get_model = MagicMock(return_value=mock_llm)

    executor = MagicMock()
    command_layer = MagicMock()
    # Mock command_layer.check to return allowed=False with validation errors
    command_layer.check.return_value = CommandResult(
        allowed=False,
        tier="green",
        validation_errors=["Missing required field: path"],
        dedup_suppressed=False,
        audit_required=False,
        block_reason="Validation failed: Missing required field: path",
        retry_hint="Schema validation failed for mock_tool: Missing required field: path. Please fix the parameters and try again.",
    )

    try:
        result = asyncio.run(
            asyncio.run(asyncio.run(run(
                query="test query",
                config=config,
                skill_registry=skill_registry,
                executor=executor,
                command_layer=command_layer,
                max_steps=1,
            )
        )  # In the first step, it should have received the error from dispatch and put it in tool_output
        assert len(result.steps) > 0
        assert result.steps[0].tool == "mock_tool"
        assert result.steps[0].tool_output["status"] == "error"
        assert "Missing required field: path" in result.steps[0].tool_output["message"]
        assert result.steps[0].tool_output.get("retry") is True

        # Executor should NOT have been called
        executor.execute.assert_not_called()

    finally:
        xibi.react.get_model = original_get_model
