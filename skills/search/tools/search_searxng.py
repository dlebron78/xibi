"""
search_searxng.py — Bregger search skill tool

Wraps the local SearXNG instance (http://localhost:8080) to perform
keyless, private web search and return structured results.

Returns up to 10 results with title, URL, and snippet.
"""

import urllib.request
import urllib.parse
import json

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
