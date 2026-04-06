from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from xibi.executor import LocalHandlerExecutor
from xibi.skills.registry import SkillRegistry

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def check_mark(success: bool) -> str:
    return f"{GREEN}[✓]{RESET}" if success else f"{RED}[✗]{RESET}"


def cmd_skill_test(args: Any) -> None:
    skill_name = args.name
    print(f"Testing skill: {skill_name}\n")

    skill_dir = Path.home() / ".xibi" / "skills" / skill_name
    manifest_path = skill_dir / "manifest.yaml"

    # Fallback for dev environment or if not in home
    if not manifest_path.exists():
        skill_dir = Path("xibi/skills/sample") / skill_name
        manifest_path = skill_dir / "manifest.yaml"
        # Also check .json if .yaml is missing
        if not manifest_path.exists():
            manifest_path = skill_dir / "manifest.json"

    if not manifest_path.exists():
        print(f"{RED}[✗] Manifest not found at {manifest_path}{RESET}")
        sys.exit(1)

    try:
        # Check 1: manifest is valid YAML/JSON
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f) if manifest_path.suffix == ".yaml" else json.load(f)
        print(f"{check_mark(True)} Manifest valid {manifest_path.suffix[1:].upper()}")

        # Check 2: required fields present
        required_root = ["name", "description", "tools"]
        missing_root = [f for f in required_root if f not in manifest]
        if not missing_root:
            print(f"{check_mark(True)} Schema fields present (name, description, tools)")
        else:
            print(f"{check_mark(False)} Missing root fields: {', '.join(missing_root)}")
            sys.exit(1)

        # Check tools
        tools = manifest.get("tools", [])
        if not isinstance(tools, list):
            print(f"{check_mark(False)} 'tools' must be a list")
            sys.exit(1)

        for tool in tools:
            tool_name = tool.get("name", "unknown")
            # Check 3: each tool has inputSchema
            if "inputSchema" in tool:
                print(f'{check_mark(True)} Tool "{tool_name}" has inputSchema')
            elif "input_schema" in tool:
                print(f'{check_mark(True)} Tool "{tool_name}" has input_schema (legacy)')
            else:
                print(f'{check_mark(False)} Tool "{tool_name}" missing inputSchema')
                sys.exit(1)

            # Check 4: inputSchema is valid JSON Schema
            schema = tool.get("inputSchema") or tool.get("input_schema")
            try:
                # Basic check: is it a dict?
                if not isinstance(schema, dict):
                    raise jsonschema.SchemaError("inputSchema must be a dictionary")

                # Use jsonschema to validate the schema itself
                jsonschema.Draft7Validator.check_schema(schema)
                print(f'{check_mark(True)} Tool "{tool_name}" input schema is valid JSON Schema')
            except Exception as e:
                print(f'{check_mark(False)} Tool "{tool_name}" input schema is invalid: {e}')
                sys.exit(1)

            # Check 5: schema has required field (at least one required field)
            if "required" in schema and isinstance(schema["required"], list) and len(schema["required"]) > 0:
                print(f'{check_mark(True)} Tool "{tool_name}" schema has required fields')
            else:
                print(f"{check_mark(False)} Tool \"{tool_name}\" schema missing 'required' field or it is empty")
                sys.exit(1)

        # Check 6: Tool invocable (dry run)
        try:
            registry = SkillRegistry(str(skill_dir.parent))
            executor = LocalHandlerExecutor(registry)

            for tool in tools:
                tool_name = tool.get("name")
                # Create synthetic input from schema
                mock_input: dict[str, Any] = {}
                schema = tool.get("inputSchema") or tool.get("input_schema") or {}
                required = schema.get("required", [])
                properties = schema.get("properties", {})

                for req in required:
                    prop = properties.get(req, {})
                    ptype = prop.get("type", "string")
                    if ptype == "string":
                        mock_input[req] = "test"
                    elif ptype == "integer":
                        mock_input[req] = 1
                    elif ptype == "number":
                        mock_input[req] = 1.0
                    elif ptype == "boolean":
                        mock_input[req] = True
                    else:
                        mock_input[req] = None

                # Invoke dry run
                print(f'Testing tool "{tool_name}" with mock input: {mock_input}')
                try:
                    executor.execute(f"{manifest['name']}.{tool_name}", mock_input)
                    print(f'{check_mark(True)} Tool "{tool_name}" invocable')
                except Exception as e:
                    print(f'{check_mark(False)} Tool "{tool_name}" invocation failed: {e}')
                    sys.exit(1)

        except Exception as e:
            print(f"{RED}[✗] Failed to load skill for invocation test: {e}{RESET}")
            sys.exit(1)

        print(f"\n{GREEN}✓ {skill_name} is compliant and functional.{RESET}")

    except Exception as e:
        print(f"{RED}[✗] Error testing skill: {e}{RESET}")
        sys.exit(1)
