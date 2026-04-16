"""Tests for the jobspy MCP server arg contract.

Regression cover for a silent failure mode: callers that sent ``search_term``
as the arg key (the jobspy-library native name) got back ``"software engineer"``
results because the server hard-coded that fallback when ``query`` was missing.
The fix is twofold:

1. Accept ``search_term`` as an alias for ``query``.
2. When neither is provided, return an explicit error rather than running a
   default search — silent defaults were masking real config bugs.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd


class TestSearchJobsArgContract(unittest.TestCase):
    """Argument handling for jobspy MCP's ``search_jobs`` tool."""

    def _empty_df(self) -> pd.DataFrame:
        # scrape_jobs returns a DataFrame; an empty one is the simplest stub.
        return pd.DataFrame(columns=["id", "title", "company"])

    @patch("jobspy.scrape_jobs")
    def test_query_key_is_honored(self, mock_scrape):
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        mock_scrape.return_value = self._empty_df()
        _search_jobs({"query": "Director of Product", "location": "Remote"})

        _, kwargs = mock_scrape.call_args
        self.assertEqual(kwargs["search_term"], "Director of Product")
        self.assertEqual(kwargs["location"], "Remote")

    @patch("jobspy.scrape_jobs")
    def test_search_term_alias_is_honored(self, mock_scrape):
        """Legacy alias: callers using 'search_term' should still work."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        mock_scrape.return_value = self._empty_df()
        _search_jobs({"search_term": "Head of Product", "location": "NYC"})

        _, kwargs = mock_scrape.call_args
        self.assertEqual(kwargs["search_term"], "Head of Product")

    @patch("jobspy.scrape_jobs")
    def test_query_wins_over_search_term_when_both_present(self, mock_scrape):
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        mock_scrape.return_value = self._empty_df()
        _search_jobs({"query": "VP Product", "search_term": "ignored"})

        _, kwargs = mock_scrape.call_args
        self.assertEqual(kwargs["search_term"], "VP Product")

    @patch("jobspy.scrape_jobs")
    def test_missing_query_fails_loudly(self, mock_scrape):
        """No default search — missing query is an error, not a silent fallback."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        result = _search_jobs({"location": "Remote"})

        mock_scrape.assert_not_called()
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["count"], 0)
        self.assertIn("query", result["error"].lower())

    @patch("jobspy.scrape_jobs")
    def test_empty_query_fails_loudly(self, mock_scrape):
        """Empty-string query is treated the same as missing — no silent default."""
        from xibi.mcp.jobspy_mcp_server import _search_jobs

        result = _search_jobs({"query": "", "location": "Remote"})

        mock_scrape.assert_not_called()
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
