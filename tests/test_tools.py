import importlib.util
import json
import os
import sys


def load_mock_params():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    mock_file = os.path.join(current_dir, "fixtures", "mock_params.json")
    with open(mock_file) as f:
        return json.load(f)


def execute_tool_with_mock(skill_name, tool_name, params):
    """Dynamically load and execute a tool with the given parameters."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    tool_file = os.path.join(project_root, "skills", skill_name, "tools", f"{tool_name}.py")

    assert os.path.exists(tool_file), f"Tool file missing: {tool_file}"

    # 1. Inject sys.path to resolve local sibling imports
    tools_dir = os.path.dirname(tool_file)
    path_injected = False
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
        path_injected = True

    try:
        # 2. Dynamic import
        spec = importlib.util.spec_from_file_location(tool_name, tool_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 3. Execution
        assert hasattr(module, "run"), f"Tool module '{tool_file}' is missing a 'run' function."

        try:
            result = module.run(params)
        except Exception as e:
            # If the tool throws a raw exception instead of returning {"status": "error"}, the test fails!
            # The tool MUST handle its own exceptions.
            raise AssertionError(f"Tool {skill_name}:{tool_name} threw an unhandled Python exception during run(): {e}")

        # 4. Assert response contract
        assert isinstance(result, dict), f"Tool {skill_name}:{tool_name} returned {type(result)} instead of dict."
        # If it returned an error dictionary, that's fine! Network/API failures are expected during a unit test.

    finally:
        if path_injected:
            sys.path.remove(tools_dir)


def test_mock_executions():
    """Iterate through mock_params.json and run each defined tool."""
    mocks = load_mock_params()

    for identifier, params in mocks.items():
        # Identifier is "skill_name:tool_name"
        skill_name, tool_name = identifier.split(":")
        execute_tool_with_mock(skill_name, tool_name, params)
