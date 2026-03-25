import importlib.util
import sys
from pathlib import Path
from typing import Any

from xibi.skills.registry import SkillRegistry


class Executor:
    def __init__(self, registry: SkillRegistry, workdir: str | Path | None = None):
        self.registry = registry
        self.workdir = Path(workdir) if workdir else None

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        # 1. Resolve skill
        skill_name = None
        skill_name = tool_name if tool_name in self.registry.skills else self.registry.find_skill_for_tool(tool_name)

        if not skill_name:
            return {"status": "error", "message": f"Unknown tool: {tool_name}"}

        skill_info = self.registry.skills[skill_name]

        # 2. Locate tool file
        tool_file = skill_info.path / "tools" / f"{tool_name}.py"
        if not tool_file.exists():
            return {"status": "error", "message": f"Tool file not found: {tool_file}"}

        # 3. Prepare params
        params = tool_input.copy()
        if self.workdir:
            params["_workdir"] = str(self.workdir)

        # 4. Add tools dir to sys.path temporarily
        tools_dir = str(skill_info.path / "tools")
        sys.path.insert(0, tools_dir)

        try:
            # 5. Dynamic import and invoke
            spec = importlib.util.spec_from_file_location(tool_name, tool_file)
            if spec is None or spec.loader is None:
                return {"status": "error", "message": f"Could not load spec for tool: {tool_name}"}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, "run"):
                return {"status": "error", "message": f"Tool '{tool_name}' missing 'run' function"}

            result = module.run(params)
            if isinstance(result, dict):
                return result
            return {"status": "error", "message": f"Tool '{tool_name}' returned non-dict result"}

        except Exception as e:
            # 6. Exception handling
            return {"status": "error", "message": f"Execution error: {str(e)}"}
        finally:
            if tools_dir in sys.path:
                sys.path.remove(tools_dir)
