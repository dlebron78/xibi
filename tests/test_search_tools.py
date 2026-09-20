"""
Tests for the search skill tools (step-140).

Covers:
  - search_searxng reports a total engine outage as status="error" (fixture A)
  - search_searxng still reports a genuinely empty result set as success (fixture B)
  - search_searxng passes healthy results through unchanged (fixture C)
  - search_searxng's pre-existing network-error path is unchanged
  - the outage message names no alternative tool (provider preference stays in
    the manifest, per CLAUDE.md rule 5)
  - the outage dict carries "status" at the top level, where xibi/react.py reads it
  - the search.engines_unavailable warning is emitted, on the module logger
  - search_tavily answer/snippet modes and _slim_results' truncation limits

Fixture note: the urlopen stub must implement the context-manager protocol and
return bytes from read(), because search_searxng.py consumes the response as
`with urllib.request.urlopen(...) as r: json.loads(r.read().decode(...))`.
Returning a bare dict raises inside the `with` and lands on the generic
`except Exception` network branch — which would make fixture A's status=="error"
assertion pass for entirely the wrong reason. test_fixture_a guards against that
explicitly.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import logging
import sys
import urllib.request
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent


def _load_tool(name: str):
    """Import a search skill tool module by file path."""
    tool_path = _ROOT / "skills" / "search" / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_tool", tool_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResponse:
    """Context-manager response whose read() returns encoded JSON bytes."""

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _patch_urlopen(monkeypatch, payload: dict):
    """Patch the module attribute search_*.py resolves at call time."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(payload))


# Fixture A — total outage: no results, four dead engines.
_DEAD_ENGINES = [
    ["brave", "too many requests"],
    ["duckduckgo", "CAPTCHA"],
    ["google", "access denied"],
    ["startpage", "Suspended: CAPTCHA"],
]
_FIXTURE_A = {"results": [], "unresponsive_engines": _DEAD_ENGINES}

# Fixture B — genuine empty result set, engines healthy.
_FIXTURE_B = {"results": [], "unresponsive_engines": []}

# Fixture C — healthy search with results.
_FIXTURE_C = {
    "results": [
        {"title": "Result one", "url": "https://example.com/1", "content": "First."},
        {"title": "Result two", "url": "https://example.com/2", "content": "Second."},
        {"title": "Result three", "url": "https://example.com/3", "content": "Third."},
    ],
    "unresponsive_engines": [],
}


# ── search_searxng ────────────────────────────────────────────────────────────


def test_fixture_a_total_outage_returns_error(monkeypatch):
    """A: zero results + dead engines → error naming every dead engine."""
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_A)

    result = mod.run({"query": "capital of Portugal"})

    # Guard: a malformed fixture would fail through the network branch and
    # satisfy status=="error" without ever reaching the new code.
    assert "SearXNG request failed" not in result["message"]
    assert "unresponsive_engines" in result["data"]

    assert result["status"] == "error"
    for engine, _reason in _DEAD_ENGINES:
        assert engine in result["message"]
    assert "4 of its engines failed" in result["message"]
    assert result["data"]["unresponsive_engines"] == _DEAD_ENGINES
    assert result["data"]["source"] == "searxng"
    assert result["data"]["query"] == "capital of Portugal"


def test_fixture_b_genuine_empty_still_succeeds(monkeypatch):
    """B: zero results, healthy engines → unchanged success. The whole point."""
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_B)

    result = mod.run({"query": "asdkjhasdkjhasd"})

    assert result["status"] == "success"
    assert result["message"] == "No results found."
    assert result["data"]["results"] == []
    assert result["data"]["count"] == 0
    assert result["data"]["source"] == "searxng"


def test_fixture_c_healthy_results_pass_through(monkeypatch):
    """C: results present → unchanged shape and content."""
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_C)

    result = mod.run({"query": "nba champions"})

    assert result["status"] == "success"
    assert result["message"] == "Found 3 results for: nba champions"
    assert result["data"]["count"] == 3
    assert result["data"]["source"] == "searxng"
    assert result["data"]["results"][0] == {
        "title": "Result one",
        "url": "https://example.com/1",
        "snippet": "First.",
    }


def test_network_exception_path_unchanged(monkeypatch):
    """A transport failure still returns the pre-existing error message."""
    mod = _load_tool("search_searxng")

    def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    result = mod.run({"query": "anything"})

    assert result["status"] == "error"
    assert "SearXNG request failed" in result["message"]
    assert "connection refused" in result["message"]


def test_outage_message_names_no_alternative_tool(monkeypatch):
    """
    Asserts the Constraint mechanically: provider preference lives in the
    manifest, never in Python. Enumerates every tool name in every skill
    manifest except search_searxng itself — the failing provider is named by
    design, and data["source"] is "searxng".
    """
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_A)

    result = mod.run({"query": "capital of Portugal"})
    message = result["message"].lower()

    other_tools = []
    for manifest_path in sorted(glob.glob(str(_ROOT / "skills" / "*" / "manifest.json"))):
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        for tool in manifest.get("tools", []):
            if tool["name"] != "search_searxng":
                other_tools.append(tool["name"])

    assert other_tools, "expected to discover tool names across skill manifests"
    for name in other_tools:
        assert name.lower() not in message, f"error message names another tool: {name}"


def test_outage_status_key_is_top_level(monkeypatch):
    """
    xibi/react.py reads tool_output.get("status") at the top level to count
    consecutive errors. A status nested under data would be invisible there.
    """
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_A)

    result = mod.run({"query": "capital of Portugal"})

    assert result.get("status") == "error"
    assert "status" not in result["data"]


def test_outage_emits_engines_unavailable_warning(monkeypatch, caplog):
    """
    DoD: the search.engines_unavailable warning is emitted. Asserting the
    record's logger name also pins TRR condition 6 — swapping logger.warning
    for the module-level logging.warning would record under "root" and call
    basicConfig(), installing a root StreamHandler into xibi-telegram.
    """
    mod = _load_tool("search_searxng")
    _patch_urlopen(monkeypatch, _FIXTURE_A)

    with caplog.at_level(logging.WARNING):
        mod.run({"query": "capital of Portugal"})

    emitted = [r for r in caplog.records if "search.engines_unavailable" in r.getMessage()]
    assert len(emitted) == 1, "expected exactly one search.engines_unavailable warning"
    assert emitted[0].levelno == logging.WARNING
    assert emitted[0].name == mod.logger.name != "root"
    for engine, _reason in _DEAD_ENGINES:
        assert engine in emitted[0].getMessage()


# ── search_tavily ─────────────────────────────────────────────────────────────


def test_tavily_answer_mode(monkeypatch):
    """Answer mode returns data["answer"] with source="tavily_answer"."""
    mod = _load_tool("search_tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key-not-a-real-secret")
    _patch_urlopen(
        monkeypatch,
        {"answer": "The New York Knicks won.", "results": [{"title": "T", "content": "C"}]},
    )

    result = mod.run({"query": "who won the 2026 nba finals"})

    assert result["status"] == "success"
    assert result["data"]["source"] == "tavily_answer"
    assert result["data"]["answer"] == "The New York Knicks won."
    assert "results" not in result["data"]


def test_tavily_snippet_mode(monkeypatch):
    """No synthesised answer → data["snippets"] with source="tavily_snippets"."""
    mod = _load_tool("search_tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key-not-a-real-secret")
    _patch_urlopen(
        monkeypatch,
        {"answer": "", "results": [{"title": "Title", "content": "Body text."}]},
    )

    result = mod.run({"query": "something obscure"})

    assert result["status"] == "success"
    assert result["data"]["source"] == "tavily_snippets"
    assert result["data"]["snippets"] == ["Title: Body text."]
    assert "results" not in result["data"]


def test_slim_results_truncates_long_snippet():
    """
    A single oversized snippet is cut at _SNIPPET_CHARS. Called directly:
    run() is gated on TAVILY_API_KEY, and answer mode returns before any
    truncation happens, so this is the only path with the logic.
    """
    mod = _load_tool("search_tavily")

    slimmed = mod._slim_results({"answer": "", "results": [{"title": "T", "content": "x" * 500}]}, "q")

    assert slimmed["source"] == "tavily_snippets"
    assert slimmed["snippets"] == ["T: " + "x" * mod._SNIPPET_CHARS]
    assert "x" * (mod._SNIPPET_CHARS + 1) not in slimmed["snippets"][0]


def test_slim_results_caps_total_chars():
    """Entries stop accumulating once the next one would exceed _MAX_TOTAL_CHARS."""
    mod = _load_tool("search_tavily")

    results = [{"title": f"T{i}", "content": "x" * mod._SNIPPET_CHARS} for i in range(5)]
    slimmed = mod._slim_results({"answer": "", "results": results}, "q")

    # Each entry is "Tn: " + 300 chars = 304; the fourth would cross 1000.
    assert slimmed["count"] == 3
    assert len(slimmed["snippets"]) == 3
    assert sum(len(s) for s in slimmed["snippets"]) <= mod._MAX_TOTAL_CHARS
