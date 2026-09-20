"""
search_searxng.py — Bregger search skill tool

Wraps the local SearXNG instance (http://localhost:8080) to perform
keyless, private web search and return structured results.

Returns up to 10 results with title, URL, and snippet.
"""

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Local SearXNG endpoint — must be running on the NucBox
_SEARXNG_URL = "http://localhost:8080/search"
_MAX_RESULTS = 5
_TIMEOUT = 10  # seconds


def run(params: dict) -> dict:
    query = params.get("query", "").strip()
    if not query:
        return {"status": "error", "message": "No search query provided."}

    args = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "safesearch": "0",
        }
    )
    url = f"{_SEARXNG_URL}?{args}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Bregger/1.0 (local-assistant)"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "message": f"SearXNG request failed: {e}"}

    raw_results = data.get("results", [])
    dead = data.get("unresponsive_engines", [])  # shape: [[engine, reason], ...]

    # A total engine outage is a tool failure, not an empty result set. Reporting
    # it as success leaves the model unable to tell "the web has no answer" from
    # "this tool is broken", so it retries the same dead tool. Which tool to reach
    # for instead is the manifest's job — naming one here would put provider
    # preference in Python (CLAUDE.md rule 5).
    if not raw_results and dead:
        logger.warning("search.engines_unavailable engines=%s", [e[0] for e in dead])
        return {
            "status": "error",
            "message": (
                f"SearXNG returned no results because {len(dead)} of its engines failed: "
                f"{', '.join(e[0] for e in dead)}. This is a tool outage, not an empty "
                f"result set."
            ),
            "data": {"query": query, "unresponsive_engines": dead, "source": "searxng"},
        }

    if not raw_results:
        return {
            "status": "success",
            "message": "No results found.",
            "data": {"results": [], "query": query, "count": 0, "source": "searxng"},
        }

    results = []
    for r in raw_results[:_MAX_RESULTS]:
        # Cap title at 80 and snippet at 300
        title = r.get("title", "")
        if len(title) > 80:
            title = title[:77] + "..."

        snippet = r.get("content", "")
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."

        results.append(
            {
                "title": title,
                "url": r.get("url", ""),
                "snippet": snippet,
            }
        )

    return {
        "status": "success",
        "message": f"Found {len(results)} results for: {query}",
        "data": {
            "results": results,
            "query": query,
            "count": len(results),
            "source": "searxng",
        },
    }
