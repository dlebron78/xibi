import os
import json
import urllib.request
import urllib.error

# Maximum characters per snippet when falling back to raw results
_SNIPPET_CHARS = 300
# Maximum total characters of raw snippets sent to LLM
_MAX_TOTAL_CHARS = 1000


def _slim_results(res_data: dict, query: str) -> dict:
    """
    Data-reduction pipeline — runs before results ever reach the LLM.

    Priority order:
      1. Tavily's synthesised 'answer'  →  discard raw snippets entirely
      2. Raw snippets                   →  truncate hard to _SNIPPET_CHARS,
                                           strip URL / images / follow-ups,
                                           cap total at _MAX_TOTAL_CHARS
    """
    answer = (res_data.get("answer") or "").strip()

    if answer:
        # Tavily already synthesised the answer — no snippets needed
        return {
            "answer": answer,
            "source": "tavily_answer",
            "query": query,
        }

    # Fallback: trim raw snippets
    snippets = []
    total = 0
    for res in res_data.get("results", []):
        title = (res.get("title") or "").strip()
        content = (res.get("content") or "").strip()[:_SNIPPET_CHARS]
        if not content:
            continue
        entry = f"{title}: {content}" if title else content
        if total + len(entry) > _MAX_TOTAL_CHARS:
            break
        snippets.append(entry)
        total += len(entry)

    return {
        "snippets": snippets,
        "source": "tavily_snippets",
        "query": query,
        "count": len(snippets),
    }


def run(params):
    query = params.get("query")
    if isinstance(query, dict):
        return {
            "status": "error",
            "message": "Got a schema instead of a query. Please retry with plain text.",
        }

    if not query or not str(query).strip():
        return {"status": "error", "message": "No search query provided."}

    query = str(query).strip()
    depth = params.get("search_depth", "basic")
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()

    if not api_key or api_key == "YOUR_KEY_HERE":
        return {
            "status": "error",
            "message": (
                "Search is not configured yet. Please provide your Tavily API key using the 'configure_search' tool."
            ),
        }

    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": depth,
        "include_answer": True,  # always request the synthesised answer
        "max_results": 5,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = json.loads(response.read().decode("utf-8"))

        slimmed = _slim_results(res_data, query)
        return {
            "status": "success",
            "message": f"Search completed for: {query}",
            "data": slimmed,
        }

    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {
                "status": "error",
                "message": (
                    "Tavily API key is invalid or unauthorized (401). "
                    "Please re-configure it using the 'configure_search' tool."
                ),
            }
        return {"status": "error", "message": f"Tavily search failed: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Tavily search failed: {e}"}
