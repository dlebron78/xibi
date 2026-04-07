import hashlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xibi.heartbeat.extractors import (
    _extract_domain,
    _url_to_ref_id,
    extract_web_search_signals,
)
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.heartbeat.source_poller import SourcePoller


def test_url_to_ref_id_is_stable():
    url = "https://example.com"
    id1 = _url_to_ref_id(url)
    id2 = _url_to_ref_id(url)
    assert id1 == id2
    assert len(id1) == 16
    assert all(c in "0123456789abcdef" for c in id1)


def test_url_to_ref_id_different_urls_different_ids():
    assert _url_to_ref_id("https://a.com") != _url_to_ref_id("https://b.com")


def test_extract_domain_strips_www():
    assert _extract_domain("https://www.techcrunch.com/article") == "techcrunch.com"


def test_extract_domain_no_www():
    assert _extract_domain("https://news.ycombinator.com/item") == "news.ycombinator.com"


def test_extract_domain_no_scheme():
    assert _extract_domain("example.com/path") == "example.com"


def test_extract_domain_empty():
    assert _extract_domain("") == ""


def test_web_search_extractor_structured_results():
    data = {
        "structured": {"results": [{"title": "Test Title", "url": "https://example.com/a", "snippet": "Test Snippet"}]}
    }
    signals = extract_web_search_signals("web_search", data, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig["ref_id"] == _url_to_ref_id("https://example.com/a")
    assert sig["entity_text"] == "example.com"
    assert sig["type"] == "web_result"
    assert sig["topic_hint"] == "Test Title"
    assert "Test Snippet" in sig["content_preview"]


def test_web_search_extractor_multiple_results():
    data = {
        "structured": {
            "results": [
                {"title": "T1", "url": "https://a.com", "snippet": "S1"},
                {"title": "T2", "url": "https://b.com", "snippet": "S2"},
                {"title": "T3", "url": "https://c.com", "snippet": "S3"},
            ]
        }
    }
    signals = extract_web_search_signals("web_search", data, {})
    assert len(signals) == 3


def test_web_search_extractor_empty_results():
    data = {"structured": {"results": []}}
    signals = extract_web_search_signals("web_search", data, {})
    assert signals == []


def test_web_search_extractor_missing_url_skipped():
    data = {
        "structured": {
            "results": [
                {"title": "T1", "snippet": "S1"},  # Missing URL
                {"url": "https://a.com", "snippet": "S2"},  # Missing Title (fallback to Untitled)
            ]
        }
    }
    signals = extract_web_search_signals("web_search", data, {})
    assert len(signals) == 1
    assert signals[0]["topic_hint"] == "Untitled"


def test_web_search_extractor_fallback_to_generic_on_plain_text():
    data = {"result": "some plain text"}
    # extract_web_search_signals calls extract_generic_signals which returns a list with 1 signal
    signals = extract_web_search_signals("web_search", data, {})
    assert len(signals) == 1
    assert signals[0]["needs_llm_extraction"] is True
    assert signals[0]["type"] == "mcp_result"


@pytest.mark.asyncio
async def test_poll_watch_topics_calls_mcp_when_due():
    mock_mcp_registry = MagicMock()
    mock_client = AsyncMock()
    mock_mcp_registry.get_client.return_value = mock_client
    mock_client.call_tool.return_value = {"structured": {"results": []}}

    config = {
        "mcp_servers": [{"name": "brave-search", "type": "web_search", "tool": "search"}],
        "watch_topics": [{"query": "test query", "interval_minutes": 60}],
    }
    poller = SourcePoller(config, MagicMock(), mock_mcp_registry)

    now = datetime.utcnow()
    results = await poller._poll_watch_topics(now)

    assert len(results) == 1
    assert results[0]["extractor"] == "web_search"
    mock_client.call_tool.assert_called_once_with("search", {"query": "test query", "count": 5})


@pytest.mark.asyncio
async def test_poll_watch_topics_skips_when_not_due():
    mock_mcp_registry = MagicMock()
    mock_client = AsyncMock()
    mock_mcp_registry.get_client.return_value = mock_client

    query = "test query"
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]

    config = {
        "mcp_servers": [{"name": "brave-search", "type": "web_search"}],
        "watch_topics": [{"query": query, "interval_minutes": 60}],
    }
    poller = SourcePoller(config, MagicMock(), mock_mcp_registry)

    now = datetime.utcnow()
    poller.last_poll[f"watch:{query_hash}"] = now - timedelta(minutes=30)

    results = await poller._poll_watch_topics(now)
    assert results == []
    mock_client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_poll_watch_topics_no_server_no_crash():
    mock_mcp_registry = MagicMock()
    config = {"mcp_servers": [], "watch_topics": [{"query": "test query"}]}
    poller = SourcePoller(config, MagicMock(), mock_mcp_registry)
    results = await poller._poll_watch_topics(datetime.utcnow())
    assert results == []


@pytest.mark.asyncio
async def test_max_results_clamped_to_10():
    mock_mcp_registry = MagicMock()
    mock_client = AsyncMock()
    mock_mcp_registry.get_client.return_value = mock_client
    mock_client.call_tool.return_value = {"structured": {"results": []}}

    config = {
        "mcp_servers": [{"name": "brave-search", "type": "web_search"}],
        "watch_topics": [{"query": "test query", "max_results": 50}],
    }
    poller = SourcePoller(config, MagicMock(), mock_mcp_registry)
    await poller._poll_watch_topics(datetime.utcnow())

    args = mock_client.call_tool.call_args[0][1]
    assert args["count"] == 10


@pytest.mark.asyncio
async def test_source_metadata_propagated_to_extractor_context():
    # We need to mock SignalExtractorRegistry.extract and see if it's called with source_metadata
    with patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract:
        mock_extract.return_value = []

        # Setup HeartbeatPoller
        MagicMock()
        db_path = MagicMock()
        MagicMock()
        rules = MagicMock()

        poller = HeartbeatPoller.__new__(HeartbeatPoller)
        poller.db_path = db_path
        poller.profile = {"some": "profile"}
        poller.source_poller = MagicMock()

        # Mock poll_due_sources to return a web_search result with metadata
        poll_result = {
            "source": "web_search:test",
            "extractor": "web_search",
            "data": {"some": "data"},
            "metadata": {"query": "test query"},
        }
        poller.source_poller.poll_due_sources = AsyncMock(return_value=[poll_result])

        # Mock other dependencies for async_tick
        poller._is_quiet_hours = MagicMock(return_value=False)
        poller._sweep_thread_lifecycle = MagicMock()
        poller.signal_intelligence_enabled = False
        poller.observation_cycle = None
        poller._jules_watcher = None
        poller.radiant = None
        poller.rules = rules
        rules.load_rules.return_value = []
        rules.get_seen_ids_with_conn.return_value = set()
        rules.load_triage_rules_with_conn.return_value = {}

        with patch("xibi.db.open_db"), patch("xibi.heartbeat.poller.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "2023-01-01 00:00:00"
            await poller.async_tick()

        mock_extract.assert_called_once()
        # extract is a classmethod, so args are (extractor_name, source_name, data, context)
        # when called as SignalExtractorRegistry.extract(...)
        context = mock_extract.call_args[1]["context"]
        assert context["source_metadata"]["query"] == "test query"
