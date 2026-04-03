from __future__ import annotations

import os
from pathlib import Path
import yaml
import pytest
from unittest.mock import patch, MagicMock
from xibi.cli.skill_test import cmd_skill_test

@pytest.fixture(autouse=True)
def clean_xibi_home(tmp_path, monkeypatch):
    xibi_home = tmp_path / ".xibi"
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return xibi_home

def strip_ansi(text):
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def test_skill_test_valid_manifest(clean_xibi_home, capsys):
    skill_name = "test_skill"
    skill_dir = clean_xibi_home / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": skill_name,
        "description": "test",
        "tools": [
            {
                "name": "test_tool",
                "description": "test",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "p1": {"type": "string"}
                    },
                    "required": ["p1"]
                }
            }
        ]
    }
    with open(skill_dir / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    args = MagicMock()
    args.name = skill_name

    # Mock executor to avoid needing real handler
    with patch("xibi.executor.LocalHandlerExecutor.execute", return_value={"status": "ok"}):
        cmd_skill_test(args)

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✓] Manifest valid YAML" in captured
    assert "[✓] Schema fields present" in captured
    assert "[✓] Tool \"test_tool\" has input_schema" in captured
    assert "[✓] Tool \"test_tool\" input schema is valid JSON Schema" in captured
    assert "[✓] Tool \"test_tool\" schema has required fields" in captured
    assert "[✓] Tool \"test_tool\" invocable" in captured

def test_skill_test_invalid_json_schema(clean_xibi_home, capsys):
    skill_name = "bad_skill"
    skill_dir = clean_xibi_home / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": skill_name,
        "description": "test",
        "tools": [
            {
                "name": "bad_tool",
                "input_schema": {
                    "type": "not-a-type" # Invalid type in JSON Schema
                }
            }
        ]
    }
    with open(skill_dir / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    args = MagicMock()
    args.name = skill_name

    with pytest.raises(SystemExit) as exc:
        cmd_skill_test(args)
    assert exc.value.code == 1

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✗] Tool \"bad_tool\" input schema is invalid" in captured

def test_skill_test_missing_required_field(clean_xibi_home, capsys):
    skill_name = "no_required_skill"
    skill_dir = clean_xibi_home / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": skill_name,
        "description": "test",
        "tools": [
            {
                "name": "tool_without_required",
                "input_schema": {
                    "type": "object",
                    "properties": {"p1": {"type": "string"}}
                    # missing "required"
                }
            }
        ]
    }
    with open(skill_dir / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    args = MagicMock()
    args.name = skill_name

    with pytest.raises(SystemExit) as exc:
        cmd_skill_test(args)
    assert exc.value.code == 1

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✗] Tool \"tool_without_required\" schema missing 'required' field" in captured
