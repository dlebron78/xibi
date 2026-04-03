from typing import Any


def web_search(params: dict[str, Any]) -> dict[str, Any]:
    query = params.get("query", "")
    return {
        "status": "ok",
        "env": "dev",
        "results": [
            {
                "title": f"Result 1 for {query}",
                "snippet": f"Information about {query}...",
                "url": "https://example.com/1",
            },
            {"title": f"Result 2 for {query}", "snippet": f"More on {query} here.", "url": "https://example.com/2"},
            {
                "title": f"Result 3 for {query}",
                "snippet": f"Comprehensive guide to {query}.",
                "url": "https://example.com/3",
            },
        ],
    }
