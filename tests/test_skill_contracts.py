"""
tests/test_skill_contracts.py — Layer 1: Skill Contract Tests

Verifies that every skill is structurally sound before deployment.
No Ollama, no NucBox, no network, no mocks. Pure import checks.

Run with:
    pytest tests/test_skill_contracts.py -v

What this catches:
  - Missing `import sys` (or any import) that only surfaces at runtime
  - Tool listed in manifest but .py file deleted or renamed
  - Manifest JSON that's malformed after an edit
  - Tool refactored without keeping the run(params) -> dict contract
  - Helper files accidentally promoted to tool entries in manifest

This suite should pass before every deploy to the NucBox.
"""

import sys
import json
import importlib.util
import inspect
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
SKILLS_DIR   = PROJECT_ROOT / "skills"

# Helper files that live inside tools/ but are NOT tools themselves.
# They won't appear in manifests, so we don't assert run() on them.
HELPER_FILES = {"_google_auth.py", "__init__.py"}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _all_manifests():
    """Yield (skill_name, manifest_path) for every skill."""
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        manifest = skill_dir / "manifest.json"
        if manifest.exists():
            yield skill_dir.name, manifest


def _manifest_tools(manifest_path: Path):
    """Return list of tool dicts declared in a manifest's tools[] array."""
    data = json.loads(manifest_path.read_text())
    return data.get("tools", [])


def _skill_tool_py(skill_name: str, tool_name: str) -> Path:
    return SKILLS_DIR / skill_name / "tools" / f"{tool_name}.py"


# Pre-collect manifests to avoid redundant disk I/O
ALL_MANIFESTS = list(_all_manifests())


def _import_tool(skill_name: str, py_path: Path):
    """Import a tool .py file by absolute path. Returns the module."""
    # Prefix module name with skill to avoid collisions (e.g., search.search vs memory.search)
    module_name = f"{skill_name}.{py_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, py_path)
    mod  = importlib.util.module_from_spec(spec)
    # Add its parent dir to sys.path so relative sibling imports work
    tools_dir = str(py_path.parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Parametrize: one test ID per (skill, tool) pair
# ---------------------------------------------------------------------------

def _collect_tool_cases():
    """Build the pytest parameter list: [(skill_name, tool_name, py_path), ...]."""
    cases = []
    for skill_name, manifest_path in ALL_MANIFESTS:
        for tool in _manifest_tools(manifest_path):
            tool_name = tool.get("name")
            if not tool_name:
                continue
            py_path = _skill_tool_py(skill_name, tool_name)
            cases.append(pytest.param(
                skill_name, tool_name, py_path,
                id=f"{skill_name}:{tool_name}"
            ))
    return cases


TOOL_CASES = _collect_tool_cases()


# ---------------------------------------------------------------------------
# Test 1 — Manifest JSON validity
# ---------------------------------------------------------------------------

class TestManifests:
    """Every manifest.json must be valid and structurally correct."""

    @pytest.mark.parametrize("skill_name,manifest_path", [
        pytest.param(s, m, id=s) for s, m in ALL_MANIFESTS
    ])
    def test_manifest_is_valid_json(self, skill_name, manifest_path):
        """Manifest parses without error."""
        try:
            data = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            pytest.fail(f"{skill_name}/manifest.json is not valid JSON: {e}")
        assert isinstance(data, dict), f"{skill_name}/manifest.json root must be a dict"

    @pytest.mark.parametrize("skill_name,manifest_path", [
        pytest.param(s, m, id=s) for s, m in _all_manifests()
    ])
    def test_manifest_has_tools_list(self, skill_name, manifest_path):
        """Manifest declares a non-empty 'tools' array."""
        data = json.loads(manifest_path.read_text())
        tools = data.get("tools")
        assert isinstance(tools, list), \
            f"{skill_name}/manifest.json missing 'tools' list"
        assert len(tools) > 0, \
            f"{skill_name}/manifest.json has empty 'tools' list"

    @pytest.mark.parametrize("skill_name,manifest_path", [
        pytest.param(s, m, id=s) for s, m in _all_manifests()
    ])
    def test_every_tool_entry_has_name(self, skill_name, manifest_path):
        """Every tool entry in manifest has a non-empty 'name' field."""
        data = json.loads(manifest_path.read_text())
        for i, tool in enumerate(data.get("tools", [])):
            assert tool.get("name"), \
                f"{skill_name}/manifest.json tools[{i}] missing 'name'"

    @pytest.mark.parametrize("skill_name,manifest_path", [
        pytest.param(s, m, id=s) for s, m in _all_manifests()
    ])
    def test_every_tool_entry_has_description(self, skill_name, manifest_path):
        """Every tool entry has a non-empty 'description' field."""
        data = json.loads(manifest_path.read_text())
        for tool in data.get("tools", []):
            name = tool.get("name", "<unnamed>")
            assert tool.get("description"), \
                f"{skill_name}:{name} missing 'description' in manifest"


# ---------------------------------------------------------------------------
# Test 2 — Tool file existence
# ---------------------------------------------------------------------------

class TestToolFiles:
    """Every tool declared in a manifest must have a corresponding .py file."""

    @pytest.mark.parametrize("skill_name,tool_name,py_path", TOOL_CASES)
    def test_tool_py_exists(self, skill_name, tool_name, py_path):
        assert py_path.exists(), \
            f"Tool declared in manifest but .py missing: {py_path.relative_to(PROJECT_ROOT)}"


# ---------------------------------------------------------------------------
# Test 3 — Tool import (catches bad imports, syntax errors)
# ---------------------------------------------------------------------------

class TestToolImports:
    """Every tool .py must import cleanly — no syntax errors, no missing deps."""

    @pytest.mark.parametrize("skill_name,tool_name,py_path", TOOL_CASES)
    def test_tool_imports_cleanly(self, skill_name, tool_name, py_path):
        try:
            _import_tool(skill_name, py_path)
        except ImportError as e:
            pytest.fail(
                f"{skill_name}:{tool_name} — ImportError: {e}\n"
                f"  File: {py_path.relative_to(PROJECT_ROOT)}"
            )
        except SyntaxError as e:
            pytest.fail(
                f"{skill_name}:{tool_name} — SyntaxError at line {e.lineno}: {e.msg}\n"
                f"  File: {py_path.relative_to(PROJECT_ROOT)}"
            )


# ---------------------------------------------------------------------------
# Test 4 — run() contract
# ---------------------------------------------------------------------------

class TestRunContract:
    """Every tool must expose run(params: dict) -> dict."""

    @pytest.mark.parametrize("skill_name,tool_name,py_path", TOOL_CASES)
    def test_tool_has_run_function(self, skill_name, tool_name, py_path):
        mod = _import_tool(skill_name, py_path)
        assert hasattr(mod, "run"), \
            f"{skill_name}:{tool_name} missing run() function"
        assert callable(mod.run), \
            f"{skill_name}:{tool_name} run is not callable"

    @pytest.mark.parametrize("skill_name,tool_name,py_path", TOOL_CASES)
    def test_run_accepts_dict_param(self, skill_name, tool_name, py_path):
        """run() must accept at least one positional argument (the params dict)."""
        mod  = _import_tool(skill_name, py_path)
        sig  = inspect.signature(mod.run)
        params = list(sig.parameters.values())
        assert len(params) >= 1, \
            f"{skill_name}:{tool_name} run() takes no arguments — expected run(params: dict)"


# ---------------------------------------------------------------------------
# Test 5 — No orphan tool files (helper: informational only, not failed)
# ---------------------------------------------------------------------------

class TestOrphans:
    """
    Warn about .py files in tools/ that are NOT in the manifest and NOT helpers.
    These are dead code — not a hard failure, but worth knowing about.
    """

    @pytest.mark.parametrize("skill_name,manifest_path", [
        pytest.param(s, m, id=s) for s, m in ALL_MANIFESTS
    ])
    def test_no_orphan_tool_files(self, skill_name, manifest_path):
        data  = json.loads(manifest_path.read_text())
        declared = {t["name"] for t in data.get("tools", []) if t.get("name")}
        tools_dir = SKILLS_DIR / skill_name / "tools"

        if not tools_dir.exists():
            return

        orphans = []
        for py_file in tools_dir.glob("*.py"):
            if py_file.name in HELPER_FILES:
                continue
            if py_file.stem not in declared:
                orphans.append(py_file.name)

        if orphans:
            # Soft warning — pytest.warns would require a warning fixture.
            # Fail with a clear message so it's visible without being silent.
            pytest.fail(
                f"{skill_name}: tool .py files not declared in manifest: {orphans}\n"
                "  These are dead code. Either add them to the manifest or delete them."
            )
