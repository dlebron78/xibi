#!/usr/bin/env python3
"""Minimal MCP server wrapping python-jobspy for Xibi integration."""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger("jobspy_mcp")

TOOL_NAME = "search_jobs"
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Search for job listings across Indeed, LinkedIn, Glassdoor, and ZipRecruiter.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Job search query (e.g. 'product manager'). Alias: 'search_term'.",
            },
            "search_term": {
                "type": "string",
                "description": "Alias for 'query'. Accepted for legacy callers; prefer 'query'.",
            },
            "location": {"type": "string", "description": "Location (e.g. 'Miami, FL')", "default": ""},
            "results_wanted": {"type": "integer", "description": "Number of results", "default": 5},
            "hours_old": {"type": "integer", "description": "Max age of listings in hours", "default": 72},
            "site_name": {
                "type": "string",
                "description": "Comma-separated sites: indeed,linkedin,glassdoor,zip_recruiter",
                "default": "indeed,linkedin",
            },
        },
        # Not listing 'query' in required — either 'query' or 'search_term' is
        # acceptable. _search_jobs enforces the either/or and returns an error
        # structure when neither is provided.
    },
}


def _search_jobs(arguments: dict) -> dict:
    """Run python-jobspy and return structured results."""
    # Accept ``query`` (canonical) or ``search_term`` (alias used by some callers,
    # incl. early config.json examples). If neither is present, fail loudly — a
    # silent default like "software engineer" masked real config bugs for weeks.
    # Validate *before* importing jobspy so a missing-arg error is surfaced
    # regardless of whether the jobspy dep is installed.
    query = arguments.get("query") or arguments.get("search_term")
    if not query:
        return {
            "jobs": [],
            "count": 0,
            "error": "missing required 'query' (alias 'search_term') argument",
        }

    from jobspy import scrape_jobs

    location = arguments.get("location", "")
    results_wanted = arguments.get("results_wanted", 5)
    hours_old = arguments.get("hours_old", 72)
    sites = [s.strip() for s in arguments.get("site_name", "indeed,linkedin").split(",")]

    try:
        jobs_df = scrape_jobs(
            site_name=sites,
            search_term=query,
            location=location or None,
            results_wanted=results_wanted,
            hours_old=hours_old,
            verbose=0,
        )

        jobs_list = []
        for _, row in jobs_df.iterrows():
            job = {
                "id": str(row.get("id", "")),
                "title": str(row.get("title", "")),
                "company": str(row.get("company", "")),
                "location": str(row.get("location", "")),
                "url": str(row.get("job_url", "")),
                "date_posted": str(row.get("date_posted", "")),
                "salary_min": row.get("min_amount")
                if row.get("min_amount") and str(row.get("min_amount")) != "nan"
                else None,
                "salary_max": row.get("max_amount")
                if row.get("max_amount") and str(row.get("max_amount")) != "nan"
                else None,
                "site": str(row.get("site", "")),
                "is_remote": bool(row.get("is_remote", False)),
                "job_type": str(row.get("job_type", "")),
            }
            jobs_list.append(job)

        return {"jobs": jobs_list, "count": len(jobs_list)}

    except Exception as e:
        logger.error("scrape_jobs failed: %s", e, exc_info=True)
        return {"jobs": [], "count": 0, "error": str(e)}


def _handle_request(request: dict) -> dict | None:
    """Handle a single JSON-RPC request. Returns response dict or None for notifications."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jobspy-mcp", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # Notification, no response

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [TOOL_SCHEMA]},
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                },
            }

        result = _search_jobs(arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "structuredContent": result,
            },
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    """Run stdio JSON-RPC loop."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    logger.info("JobSpy MCP server starting (stdio mode)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            error_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            sys.stdout.write(json.dumps(error_resp) + "\n")
            sys.stdout.flush()
            continue

        response = _handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
