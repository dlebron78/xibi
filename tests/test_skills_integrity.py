import json
import os
import glob
import importlib
import inspect


def test_manifest_schema():
    """Verify all manifest.json files contain required keys."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    workdir = project_root
    manifest_paths = glob.glob(os.path.join(workdir, "skills", "*", "manifest.json"))
    assert len(manifest_paths) > 0, "No skill manifests found in the skills directory."

    for path in manifest_paths:
        with open(path, "r") as f:
            manifest = json.load(f)

        assert "name" in manifest, f"Manifest missing 'name': {path}"
        assert "description" in manifest, f"Manifest missing 'description': {path}"
        assert "tools" in manifest, f"Manifest missing 'tools': {path}"
        assert isinstance(manifest["tools"], list), f"'tools' must be a list: {path}"


def test_tool_compilation_and_contract():
    """Dynamically import every tool module and assert it exposes a run() function."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    workdir = project_root
    manifest_paths = glob.glob(os.path.join(workdir, "skills", "*", "manifest.json"))
    for path in manifest_paths:
        with open(path, "r") as f:
            manifest = json.load(f)

        skill_name = manifest["name"]

        for tool in manifest.get("tools", []):
            tool_name = tool.get("name")
            module_path = tool.get("python_module")

            assert tool_name, f"Tool missing 'name' in skill '{skill_name}'"

            tool_file = os.path.join(os.path.dirname(path), "tools", f"{tool_name}.py")
            assert os.path.exists(tool_file), f"Tool file missing: {tool_file}"

            # 1. Test compilation (import)
            tools_dir = os.path.dirname(tool_file)
            path_injected = False
            import sys

            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
                path_injected = True

            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location(tool_name, tool_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as e:
                raise ImportError(f"Failed to load tool module '{tool_file}' for skill '{skill_name}': {e}")
            finally:
                if path_injected:
                    sys.path.remove(tools_dir)

            # 2. Test contract (run function)
            assert hasattr(module, "run"), f"Tool module '{tool_file}' is missing a 'run' function."

            run_func = getattr(module, "run")
            assert callable(run_func), f"'run' in '{tool_file}' is not a function."

            # Verify signature has 'params' argument (with optional fallback check)
            sig = inspect.signature(run_func)
            parameters = list(sig.parameters.keys())
            assert "params" in parameters or "**" in str(sig), f"'run' function in '{tool_file}' must accept 'params'."
