import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xibi.heartbeat.extractors import (
    _issue_to_ref_id,
    _sha_to_ref_id,
    extract_github_activity_signals,
)
from xibi.heartbeat.source_poller import SourcePoller


# Helper function tests
def test_sha_to_ref_id_is_stable():
    sha = "abc123def456"
    res1 = _sha_to_ref_id(sha)
    res2 = _sha_to_ref_id(sha)
    assert res1 == res2
    assert len(res1) == 16
    assert all(c in "0123456789abcdef" for c in res1)


def test_sha_to_ref_id_different_shas_different_ids():
    assert _sha_to_ref_id("abc") != _sha_to_ref_id("def")


def test_issue_to_ref_id_is_stable():
    repo = "owner/repo"
    num = 42
    res1 = _issue_to_ref_id(repo, num)
    res2 = _issue_to_ref_id(repo, num)
    assert res1 == res2
    assert len(res1) == 16
    assert all(c in "0123456789abcdef" for c in res1)


def test_issue_to_ref_id_different_repos_different_ids():
    assert _issue_to_ref_id("a/b", 1) != _issue_to_ref_id("c/d", 1)


def test_issue_to_ref_id_different_numbers_different_ids():
    assert _issue_to_ref_id("a/b", 1) != _issue_to_ref_id("a/b", 2)


# Extractor tests
def test_github_activity_extractor_commits():
    data = {
        "structured": {
            "commits": [
                {
                    "sha": "abc123def456",
                    "message": "Fix bug\n\nDetails",
                    "author": {"name": "Alice"},
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ]
        }
    }
    context = {"source_metadata": {"repo": "owner/repo"}}
    signals = extract_github_activity_signals("github", data, context)

    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "github_commit"
    assert sig["entity_text"] == "Alice"
    assert sig["content_preview"] == "abc123de: Fix bug"
    assert sig["ref_id"] == _sha_to_ref_id("abc123def456")
    assert sig["metadata"]["repo"] == "owner/repo"


def test_github_activity_extractor_issues():
    data = {
        "structured": {
            "issues": [
                {
                    "number": 42,
                    "title": "Login broken",
                    "state": "open",
                    "user": {"login": "bob"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://github.com/owner/repo/issues/42",
                    "body": "...",
                }
            ]
        }
    }
    context = {"source_metadata": {"repo": "owner/repo"}}
    signals = extract_github_activity_signals("github", data, context)

    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "github_issue"
    assert sig["entity_text"] == "#42"
    assert sig["content_preview"] == "[open] #42: Login broken"
    assert sig["ref_id"] == _issue_to_ref_id("owner/repo", 42)


def test_github_activity_extractor_prs():
    data = {
        "structured": {
            "pull_requests": [
                {
                    "number": 57,
                    "title": "Add feature",
                    "state": "open",
                    "user": {"login": "alice"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://github.com/owner/repo/pull/57",
                    "body": "...",
                }
            ]
        }
    }
    context = {"source_metadata": {"repo": "owner/repo"}}
    signals = extract_github_activity_signals("github", data, context)

    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "github_pr"
    assert sig["entity_text"] == "PR #57"


def test_github_activity_extractor_fallback_on_unknown_structured():
    data = {"structured": {"unknown_key": []}}
    signals = extract_github_activity_signals("github", data, {})
    # Should fall back to generic which returns one signal with needs_llm_extraction=True
    assert len(signals) == 1
    assert signals[0].get("needs_llm_extraction") is True


def test_github_activity_extractor_skips_commit_missing_sha():
    data = {"structured": {"commits": [{"message": "No sha"}]}}
    signals = extract_github_activity_signals("github", data, {})
    assert len(signals) == 0


def test_github_activity_extractor_skips_issue_missing_number():
    data = {"structured": {"issues": [{"title": "No number"}]}}
    signals = extract_github_activity_signals("github", data, {})
    assert len(signals) == 0


def test_github_activity_extractor_commit_first_line_only():
    data = {
        "structured": {
            "commits": [
                {"sha": "abc123def456", "message": "Fix bug\n\nDetailed explanation here", "author": {"name": "Alice"}}
            ]
        }
    }
    signals = extract_github_activity_signals("github", data, {})
    assert signals[0]["topic_hint"] == "Fix bug"
    assert signals[0]["content_preview"] == "abc123de: Fix bug"


# Poller tests
@pytest.mark.asyncio
async def test_poll_watch_repos_calls_mcp_for_commits():
    config = {
        "watch_repos": [
            {
                "repo": "owner/repo",
                "watch_commits": True,
                "watch_issues": False,
                "watch_prs": False,
                "interval_minutes": 60,
                "max_items": 10,
            }
        ],
        "mcp_servers": [{"name": "github", "type": "github"}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"structured": {"commits": []}})
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)

    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
        results = await poller._poll_watch_repos(datetime.utcnow())

    assert len(results) == 1
    assert results[0]["extractor"] == "github_activity"
    assert results[0]["metadata"]["event_type"] == "commits"
    client.call_tool.assert_called_once_with("list_commits", {"repo": "owner/repo", "max_results": 10})


@pytest.mark.asyncio
async def test_poll_watch_repos_skips_when_not_due():
    config = {
        "watch_repos": [{"repo": "owner/repo", "interval_minutes": 60}],
        "mcp_servers": [{"name": "github", "type": "github"}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"structured": {}})
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    now = datetime.utcnow()
    import hashlib

    repo_hash = hashlib.sha256(b"owner/repo").hexdigest()[:8]
    # Set last poll for all default enabled event types (commits and prs)
    poller.last_poll[f"watchrepo:{repo_hash}:commits"] = now - timedelta(minutes=30)
    poller.last_poll[f"watchrepo:{repo_hash}:prs"] = now - timedelta(minutes=30)

    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
        results = await poller._poll_watch_repos(now)

    assert len(results) == 0
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_poll_watch_repos_no_server_no_crash():
    config = {"watch_repos": [{"repo": "a/b"}], "mcp_servers": []}
    poller = SourcePoller(config, MagicMock(), MagicMock())
    results = await poller._poll_watch_repos(datetime.utcnow())
    assert results == []


@pytest.mark.asyncio
async def test_poll_watch_repos_skips_when_no_token():
    config = {"watch_repos": [{"repo": "owner/repo"}], "mcp_servers": [{"name": "github", "type": "github"}]}
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)

    with patch.dict(os.environ, {}, clear=True):
        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]
        results = await poller._poll_watch_repos(datetime.utcnow())

    assert results == []
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_max_items_clamped_to_20():
    config = {
        "watch_repos": [{"repo": "owner/repo", "max_items": 50}],
        "mcp_servers": [{"name": "github", "type": "github"}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={})
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
        await poller._poll_watch_repos(datetime.utcnow())

    # max_results passed to call_tool should be 20
    args = client.call_tool.call_args[0][1]
    assert args["max_results"] == 20


@pytest.mark.asyncio
async def test_poll_watch_repos_independent_per_event_type():
    config = {
        "watch_repos": [{"repo": "owner/repo", "watch_commits": True, "watch_issues": True, "watch_prs": False}],
        "mcp_servers": [{"name": "github", "type": "github"}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"structured": {}})
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
        results = await poller._poll_watch_repos(datetime.utcnow())

    assert len(results) == 2
    event_types = [r["metadata"]["event_type"] for r in results]
    assert "commits" in event_types
    assert "issues" in event_types
    assert "prs" not in event_types
    assert client.call_tool.call_count == 2
