from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class SignalExtractorRegistry:
    """Registry of source-specific signal extraction strategies."""

    extractors: dict[str, Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]] = {}

    @classmethod
    def register(
        cls, name: str
    ) -> Callable[
        [Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]],
        Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]],
    ]:
        def decorator(
            fn: Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]],
        ) -> Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]:
            cls.extractors[name] = fn
            return fn

        return decorator

    @classmethod
    def extract(cls, extractor_name: str, source_name: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract signals from raw data using the specified strategy.
        context should contain any needed dependencies like db_path or config.
        """
        fn = cls.extractors.get(extractor_name, cls.extractors.get("generic"))
        if not fn:
            logger.warning(f"No extractor found for '{extractor_name}' and no generic fallback.")
            return []
        try:
            return fn(source_name, data, context)
        except Exception as e:
            logger.error(f"Extractor '{extractor_name}' failed: {e}", exc_info=True)
            return []


def _url_to_ref_id(url: str) -> str:
    """Return a stable 16-char hex ID from the URL (SHA-256 prefix)."""
    import hashlib

    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _path_to_ref_id(path: str) -> str:
    """Return a stable 16-char hex ID from a file path (SHA-256 prefix)."""
    import hashlib

    return hashlib.sha256(path.encode()).hexdigest()[:16]


def _extract_filename(path: str) -> str:
    """Extract the filename (last component) from a file path."""
    from pathlib import PurePosixPath

    return PurePosixPath(path).name or path


def _extract_extension(path: str) -> str:
    """Extract the file extension (without dot), lowercase."""
    from pathlib import PurePosixPath

    suffix = PurePosixPath(path).suffix
    return suffix.lstrip(".").lower() if suffix else ""


def _extract_domain(url: str) -> str:
    """Extract the domain from a URL, stripping www."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc or url
    if not parsed.netloc and "/" in domain:
        domain = domain.split("/")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _sha_to_ref_id(sha: str) -> str:
    """Return a stable 16-char hex ID from a Git commit SHA."""
    import hashlib

    return hashlib.sha256(sha.encode()).hexdigest()[:16]


def _issue_to_ref_id(repo: str, number: int) -> str:
    """Return a stable 16-char hex ID from a repo + issue/PR number pair."""
    import hashlib

    return hashlib.sha256(f"{repo}#{number}".encode()).hexdigest()[:16]


@SignalExtractorRegistry.register("github_activity")
def extract_github_activity_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract signals from GitHub MCP tool results.

    Supported data shapes (from @modelcontextprotocol/server-github):

      Commits result (from list_commits):
        {"structured": {"commits": [{"sha": str, "message": str,
          "author": {"name": str, "email": str}, "timestamp": str}, ...]}}

      Issues result (from list_issues):
        {"structured": {"issues": [{"number": int, "title": str, "state": str,
          "body": str, "user": {"login": str}, "created_at": str, "html_url": str}, ...]}}

      Pull requests result (from list_pull_requests):
        {"structured": {"pull_requests": [{"number": int, "title": str, "state": str,
          "body": str, "user": {"login": str}, "created_at": str, "html_url": str}, ...]}}

    Falls back to generic extractor if none of the structured keys are recognized.
    """
    if not isinstance(data, dict) or "structured" not in data:
        return extract_generic_signals(source, data, context)

    structured = data["structured"]
    repo = context.get("source_metadata", {}).get("repo", "")
    signals = []

    if "commits" in structured:
        for commit in structured["commits"]:
            sha = commit.get("sha")
            message = commit.get("message")
            if not sha or message is None:
                continue

            author = commit.get("author", {})
            author_name = author.get("name") or author.get("login") or "unknown"
            author_email = author.get("email", "")
            timestamp = commit.get("timestamp", "")
            message_first_line = message.splitlines()[0] if message else "(no message)"

            signals.append(
                {
                    "source": source,
                    "type": "github_commit",
                    "entity_text": author_name,
                    "entity_type": "developer",
                    "topic_hint": message_first_line,
                    "content_preview": f"{sha[:8]}: {message_first_line}",
                    "ref_id": _sha_to_ref_id(sha),
                    "ref_source": "github",
                    "metadata": {
                        "sha": sha,
                        "sha_short": sha[:8],
                        "author": author_name,
                        "author_email": author_email,
                        "timestamp": timestamp,
                        "repo": repo,
                    },
                }
            )

    elif "issues" in structured:
        for issue in structured["issues"]:
            number = issue.get("number")
            title = issue.get("title")
            if number is None or title is None:
                continue

            state = issue.get("state", "unknown")
            user_login = issue.get("user", {}).get("login", "unknown")
            created_at = issue.get("created_at", "")
            html_url = issue.get("html_url", "")

            signals.append(
                {
                    "source": source,
                    "type": "github_issue",
                    "entity_text": f"#{number}",
                    "entity_type": "issue",
                    "topic_hint": title,
                    "content_preview": f"[{state}] #{number}: {title}",
                    "ref_id": _issue_to_ref_id(repo, number),
                    "ref_source": "github",
                    "metadata": {
                        "number": number,
                        "title": title,
                        "state": state,
                        "author": user_login,
                        "created_at": created_at,
                        "url": html_url,
                        "repo": repo,
                    },
                }
            )

    elif "pull_requests" in structured:
        for pr in structured["pull_requests"]:
            number = pr.get("number")
            title = pr.get("title")
            if number is None or title is None:
                continue

            state = pr.get("state", "unknown")
            user_login = pr.get("user", {}).get("login", "unknown")
            created_at = pr.get("created_at", "")
            html_url = pr.get("html_url", "")

            signals.append(
                {
                    "source": source,
                    "type": "github_pr",
                    "entity_text": f"PR #{number}",
                    "entity_type": "pull_request",
                    "topic_hint": title,
                    "content_preview": f"[{state}] PR #{number}: {title}",
                    "ref_id": _issue_to_ref_id(repo, number),
                    "ref_source": "github",
                    "metadata": {
                        "number": number,
                        "title": title,
                        "state": state,
                        "author": user_login,
                        "created_at": created_at,
                        "url": html_url,
                        "repo": repo,
                    },
                }
            )
    else:
        return extract_generic_signals(source, data, context)

    return signals


@SignalExtractorRegistry.register("file_content")
def extract_file_content_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract signals from filesystem MCP tool results.

    Supported data shapes (from @modelcontextprotocol/server-filesystem):
      Single file result (from read_file):
        {"content": [{"type": "text", "text": "<file content>"}]}
        context["source_metadata"]["path"] contains the file path

      Multiple files result (from read_multiple_files):
        {"content": [{"type": "text", "text": "<path1>\n---\n<content1>"},
                     {"type": "text", "text": "<path2>\n---\n<content2>"}]}
    """
    if not isinstance(data, dict) or "content" not in data or not data["content"]:
        return extract_generic_signals(source, data, context)

    signals = []
    source_metadata = context.get("source_metadata", {})
    watch_dir = source_metadata.get("watch_dir", "")

    for item in data["content"]:
        if item.get("type") != "text" or "text" not in item:
            continue

        text = item["text"]
        if not text:
            continue

        # Multi-file parsing heuristic:
        # Detect if text block contains \n---\n and first line looks like a path.
        item_files = []
        if "\n---\n" in text:
            parts = text.split("\n---\n")
            first_part = parts[0].strip()
            if first_part.startswith(("/", "~", "./")) or "." in first_part:
                # Multi-file detected within a single text block
                current_path = first_part
                for i in range(1, len(parts)):
                    part = parts[i]
                    if i < len(parts) - 1:
                        if "\n" in part:
                            content, next_path = part.rsplit("\n", 1)
                            item_files.append((current_path, content))
                            current_path = next_path.strip()
                        else:
                            item_files.append((current_path, part))
                            current_path = "unknown"
                    else:
                        item_files.append((current_path, part))
            else:
                path = source_metadata.get("path", "unknown")
                item_files.append((path, text))
        else:
            path = source_metadata.get("path", "unknown")
            item_files.append((path, text))

        for path, content in item_files:
            display_content = content[:500]
            if len(content) > 500:
                display_content = display_content[:497] + "..."

            signals.append(
                {
                    "source": source,
                    "type": "file_content",
                    "entity_text": _extract_filename(path),
                    "entity_type": "file",
                    "topic_hint": path,
                    "content_preview": display_content,
                    "ref_id": _path_to_ref_id(path),
                    "ref_source": "filesystem",
                    "metadata": {
                        "path": path,
                        "size_chars": len(content),
                        "extension": _extract_extension(path),
                        "watch_dir": watch_dir,
                    },
                }
            )

    return signals


@SignalExtractorRegistry.register("web_search")
def extract_web_search_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract signals from web search MCP tool results.

    Expected data shape (from Brave or Tavily MCP):
      dict with "structured" key containing:
        {"results": [{"title": str, "url": str, "snippet": str, ...}, ...]}
      OR
      dict with "result" key (plain text fallback)
    """
    structured = None
    if isinstance(data, dict):
        structured = data.get("structured")

    if not structured or "results" not in structured:
        # Fallback behavior: if data has no structured results key, fall back to generic
        return extract_generic_signals(source, data, context)

    results = structured.get("results", [])
    if not results:
        return []

    signals = []
    query = context.get("source_metadata", {}).get("query", "")

    for res in results:
        title = res.get("title")
        url = res.get("url")
        snippet = res.get("snippet", "")

        if not url:
            continue

        title = title or "Untitled"
        url = url or ""

        # Snippet Truncation: content_preview ≤ 500 chars total (title + separator + snippet)
        # separator is " — " (3 chars). If longer than 500 - len(title) - 4, truncate.
        max_snippet_len = 500 - len(title) - 4
        display_snippet = snippet
        if len(display_snippet) > max_snippet_len:
            display_snippet = display_snippet[: max_snippet_len - 3] + "..."

        signals.append(
            {
                "source": source,
                "type": "web_result",
                "entity_text": _extract_domain(url),
                "entity_type": "website",
                "topic_hint": title,
                "content_preview": f"{title} — {display_snippet}",
                "ref_id": _url_to_ref_id(url) if url else "",
                "ref_source": "web_search",
                "metadata": {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "query": query,
                },
            }
        )
    return signals


@SignalExtractorRegistry.register("email")
def extract_email_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract signals from email data.
    data is expected to be a list of email dicts.
    """
    if not isinstance(data, list):
        logger.warning(f"Email extractor expected list, got {type(data)}")
        return []

    signals = []
    for email in data:
        email_id = str(email.get("id", ""))
        sender = email.get("from", email.get("sender", "unknown"))
        if isinstance(sender, dict):
            sender = sender.get("name") or sender.get("addr", "unknown")
        subject = email.get("subject", "No Subject")

        signals.append(
            {
                "source": source,
                "topic_hint": subject,
                "entity_text": str(sender),
                "entity_type": "person",
                "content_preview": f"{sender}: {subject}",
                "ref_id": email_id,
                "ref_source": "email",
                "metadata": {"email": email},
            }
        )
    return signals


def _normalize_company(name: str) -> str:
    """Normalize company name for thread matching."""
    suffixes = [", Inc.", " Inc.", " LLC", " Ltd.", " Corp.", ", Corp.", " Co.", " AG", " SE", " PLC"]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


@SignalExtractorRegistry.register("jobs")
def extract_job_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract one signal per job listing from JobSpy MCP tool results.
    """
    structured = None
    if isinstance(data, dict):
        structured = data.get("structured")

    if not structured or "jobs" not in structured:
        # Fallback: generic extraction with extractor hint for signal intelligence
        return [
            {
                "source": source,
                "type": "job_batch",
                "raw": data.get("result", str(data)) if isinstance(data, dict) else str(data),
                "needs_llm_extraction": True,
                "extractor_hint": "jobs",
                "content_preview": "Job search results (unstructured)",
            }
        ]

    signals = []
    for job in structured.get("jobs", []):
        job_id = str(job.get("id", ""))
        company = _normalize_company(job.get("company", "Unknown Company"))
        title = job.get("title", "Unknown Role")
        location = job.get("location", "")
        salary_range = ""
        if job.get("salary_min") and job.get("salary_max"):
            salary_range = f"${job['salary_min']:,}–${job['salary_max']:,}"

        signals.append(
            {
                "source": source,
                "type": "job_listing",
                "entity_text": company,
                "entity_type": "company",
                "topic_hint": f"{title} at {company}",
                "content_preview": f"{title} | {company} | {location}{' | ' + salary_range if salary_range else ''}",
                "ref_id": job_id,
                "ref_source": "jobspy",
                "metadata": {
                    "job": job,
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary_min": job.get("salary_min"),
                    "salary_max": job.get("salary_max"),
                    "url": job.get("url", ""),
                    "posted_at": job.get("posted_at", ""),
                },
            }
        )
    return signals


@SignalExtractorRegistry.register("calendar")
def extract_calendar_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract signals from calendar events."""
    if not isinstance(data, dict):
        logger.warning(f"Calendar extractor expected dict, got {type(data)}")
        return []

    signals = []
    for event in data.get("events", []):
        signals.append(
            {
                "source": source,
                "type": "event",
                "entity_text": event.get("organizer", "unknown"),
                "topic_hint": event.get("summary", ""),
                "content_preview": f"Event: {event.get('summary', '')} at {event.get('start', '')}",
                "timestamp": event.get("start", ""),
                "ref_id": event.get("id", ""),
                "ref_source": "calendar",
                "metadata": {"event": event},
            }
        )
    return signals


@SignalExtractorRegistry.register("generic")
def extract_generic_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Generic extractor for tool results.
    """
    # result might be a dict with "result" (text) and "structured" (MCP)
    text = ""
    structured = None
    if isinstance(data, dict):
        text = data.get("result", "")
        structured = data.get("structured")
    else:
        text = str(data)

    return [
        {
            "source": source,
            "type": "mcp_result",
            "content_preview": text[:500],
            "raw": text,
            "structured": structured,
            "needs_llm_extraction": True,
        }
    ]
