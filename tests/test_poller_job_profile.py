from __future__ import annotations

import pytest

from xibi.heartbeat.source_poller import SourcePoller


@pytest.mark.asyncio
async def test_poller_uses_job_search_profile_args(mocker):
    config = {
        "heartbeat": {"sources": []},
        "job_search": {
            "enabled": True,
            "profiles": [
                {
                    "name": "pm_miami",
                    "query": "product manager",
                    "location": "Miami, FL",
                    "salary_min": 120000,
                    "interval_minutes": 60,
                }
            ],
        },
    }
    mcp_registry = mocker.Mock()
    client = mocker.AsyncMock()
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config=config, executor=mocker.Mock(), mcp_registry=mcp_registry)

    source = {
        "name": "jobs",
        "type": "mcp",
        "server": "jobspy",
        "tool": "search_jobs",
        "args": {"results_wanted": 5},
    }

    await poller._poll_source(source)

    # Check client.call_tool args
    client.call_tool.assert_called_once()
    args, kwargs = client.call_tool.call_args
    if args:
        assert args[0] == "search_jobs"
        params = args[1]
    else:
        assert kwargs["tool_name"] == "search_jobs"
        params = kwargs["arguments"]

    assert params["query"] == "product manager Miami, FL"
    assert params["results_wanted"] == 5


@pytest.mark.asyncio
async def test_poller_falls_back_to_source_args_without_profile(mocker):
    config = {
        "heartbeat": {"sources": []},
        "job_search": {"enabled": True, "profiles": []},
    }
    mcp_registry = mocker.Mock()
    client = mocker.AsyncMock()
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config=config, executor=mocker.Mock(), mcp_registry=mcp_registry)

    source = {
        "name": "jobs",
        "type": "mcp",
        "server": "jobspy",
        "tool": "search_jobs",
        "args": {"query": "standard query", "results_wanted": 5},
    }

    await poller._poll_source(source)

    client.call_tool.assert_called_once()
    args, kwargs = client.call_tool.call_args
    if args:
        params = args[1]
    else:
        params = kwargs["arguments"]
    assert params["query"] == "standard query"


@pytest.mark.asyncio
async def test_poller_non_jobspy_source_unaffected(mocker):
    config = {"job_search": {"profiles": [{"query": "product manager", "location": "Miami, FL"}]}}
    mcp_registry = mocker.Mock()
    client = mocker.AsyncMock()
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config=config, executor=mocker.Mock(), mcp_registry=mcp_registry)

    source = {
        "name": "slack",
        "type": "mcp",
        "server": "slack",
        "tool": "slack_search",
        "args": {"query": "important query"},
    }

    await poller._poll_source(source)

    client.call_tool.assert_called_once()
    args, kwargs = client.call_tool.call_args
    if args:
        params = args[1]
    else:
        params = kwargs["arguments"]
    assert params["query"] == "important query"
