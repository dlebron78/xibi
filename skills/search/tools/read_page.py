"""
read_page.py — Bregger search skill tool

Fetches and extracts clean text from a URL using trafilatura.
The caller (LLM) decides when to invoke this, typically after search_searxng
returns a URL that needs deeper reading.
"""

import urllib.request

_MAX_CHARS = 4000  # Token budget: keep context lean for the 9B model
_TIMEOUT = 10  # seconds


def run(params: dict) -> dict:
    url = params.get("url", "").strip()
    if not url:
        return {"status": "error", "message": "No URL provided."}
    if not url.startswith(("http://", "https://")):
        # Detect mailto: specifically — model may pass email IDs as mailto: URLs
        if url.startswith("mailto:"):
            # Extract numeric ID from mailto:[152292] or mailto:152292
            email_id = url.replace("mailto:", "").strip().strip("[]")
            return {
                "status": "error",
                "message": "read_page only fetches web pages (http/https). To read email content, use summarize_email.",
                "correct_tool": "summarize_email",
                "email_id": email_id if email_id.isdigit() else None,
            }
        return {"status": "error", "message": f"Invalid URL: {url}. Only http:// and https:// URLs are supported."}

    text = _extract(url)
    if text:
        return {
            "status": "success",
            "message": f"Page content extracted: {url}",
            "data": {
                "url": url,
                "content": text[:_MAX_CHARS],
                "truncated": len(text) > _MAX_CHARS,
            },
        }

    return {"status": "error", "message": f"Could not extract content from: {url}"}


def _extract(url: str) -> str:
    """Fetch page and extract main content using trafilatura."""
    try:
        import trafilatura

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Bregger/1.0)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            html = response.read()
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        return text.strip() if text else ""
    except Exception:
        return ""
