"""Tests for the jobspy MCP server arg contract.

Regression cover for a silent failure mode: callers that sent ``search_term``
as the arg key (the jobspy-library native name) got back ``"software engineer"``
results because the server hard-coded that fallback when ``query`` was missing.
The fix is twofold:

1. Accept ``search_term`` as an alias for ``query``.
2. When neither is provided, return an explicit error rather than running a
   default search — silent defaults were masking real config bugs.

These tests do not require the jobspy / pandas runtime deps. The fail-fast
cases return before the jobspy import, and the alias-routing cases stub
the ``jobspy`` module entirely in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
import unittest
from typing import Any


def _install_jobspy_stub(capture: dict[str, Any]) -> types.ModuleType:
    """Install a fake ``jobspy`` module that records the kwargs scrape_jobs is
    called with and returns a minimal duck-typed 'empty DataFrame' (iterrows ->
    empty iterator). Returns the installed stub module for unwind/cleanup."""

    class _FakeDF:
        def iterrows(self):
            return iter(())

    def fake_scrape_jobs(**kwargs):
        capture.clear()
        capture.update(kwargs)
        return _FakeDF()

    stub = types.ModuleType("jobspy")
    stub.scrape_jobs = fake_scrape_jobs
    sys.modules["jobspy"] = stub
    return stub


class TestSearchJobsArgContract(unittest.TestCase):
    """Argument handling for jobspy MCP's ``search_jobs`` tool."""

    def setUp(self):
        self._prev_jobspy = sys.modules.get("jobspy")
        self.capture: dict[str, Any] = {}
        _install_jobspy_stub(self.capture)

    def tearDown(self):
        if self._prev_jobspy is not None:
            sys.modules["jobspy"] = self._prev_jobspy
        else:
            sys.modules.pop("jobspy", None)

    def test_query_key_is_honored(self):
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        _search_jobs({"query": "Director of Product", "location": "Remote"})

        self.assertEqual(self.capture.get("search_term"), "Director of Product")
        self.assertEqual(self.capture.get("location"), "Remote")

    def test_search_term_alias_is_honored(self):
        """Legacy alias: callers using 'search_term' should still work."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        _search_jobs({"search_term": "Head of Product", "location": "NYC"})

        self.assertEqual(self.capture.get("search_term"), "Head of Product")

    def test_query_wins_over_search_term_when_both_present(self):
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        _search_jobs({"query": "VP Product", "search_term": "ignored"})

        self.assertEqual(self.capture.get("search_term"), "VP Product")

    def test_missing_query_fails_loudly(self):
        """No default search — missing query is an error, not a silent fallback."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        result = _search_jobs({"location": "Remote"})

        # scrape_jobs should not have been invoked.
        self.assertEqual(self.capture, {})
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["count"], 0)
        self.assertIn("query", result["error"].lower())

    def test_empty_query_fails_loudly(self):
        """Empty-string query is treated the same as missing — no silent default."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        result = _search_jobs({"query": "", "location": "Remote"})

        self.assertEqual(self.capture, {})
        self.assertIn("query", result["error"].lower())


class TestToolSchema(unittest.TestCase):
    """The advertised tool schema should document both the canonical key and alias."""

    def test_schema_advertises_query_and_search_term(self):
        from xibi.mcp.jobspy_mcp_server import TOOL_SCHEMA

        props = TOOL_SCHEMA["inputSchema"]["properties"]
        self.assertIn("query", props)
        self.assertIn("search_term", props)

    def test_schema_does_not_require_query(self):
        """With the either/or alias, 'query' cannot be strictly required — the
        server enforces the contract in code and returns a clear error."""
        from xibi.mcp.jobspy_mcp_server import TOOL_SCHEMA

        required = TOOL_SCHEMA["inputSchema"].get("required", [])
        self.assertNotIn("query", required)


if __name__ == "__main__":
    unittest.main()
