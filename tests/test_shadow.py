import json
import asyncio
from unittest.mock import MagicMock
import pytest
from xibi.react import run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher


@pytest.fixture
def mock_config():
    return MagicMock(spec=Config)


def test_react_shadow_direct_calls_tool(mock_config):
    skill_registry = [{"name": "get_weather"}]
    executor = MagicMock()

    async def mock_execute(*args, **kwargs):
        return {"status": "ok", "content": "Sunny"}

    executor.execute = mock_execute

    shadow = ShadowMatcher()
    shadow.build_corpus([("weather", "get_weather", "what is the weather")])

    result = asyncio.asyncio.asyncio.asyncio.run(asyncio.run(run(run(run(
        asyncio.run(run()
            query="what is the weather",
            config=mock_config,
            skill_registry=skill_registry,
            executor=executor,
            shadow=shadow,
        )
    ))

    assert result.answer == "Sunny"
    assert result.exit_reason == "finish"


def test_react_shadow_hint_prepends_context(mock_config, monkeypatch):
    skill_registry = [{"name": "get_weather"}]
    shadow = ShadowMatcher()
    shadow.build_corpus([("weather", "get_weather", "get weather today")])

    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "hint", "tool": "finish", "tool_input": {"answer": "done"}}'
    monkeypatch.setattr("xibi.react.get_model", lambda *args, **kwargs: mock_llm))))

    asyncio.asyncio.asyncio.asyncio.run(asyncio.run(run(run(run(
        asyncio.run(run()
            query="get weather",
            config=mock_config,
            skill_registry=skill_registry,
            shadow=shadow,
            context="original context",
        )))
    )))

    args, kwargs = mock_llm.generate.call_args
    assert "[Shadow hint: consider using get_weather]" in args[0]


def test_react_shadow_after_control_plane(mock_config):
    cp = ControlPlaneRouter()
    shadow = MagicMock(spec=ShadowMatcher)
    result = asyncio.asyncio.run(asyncio.run(run(
        asyncio.run(run(query="hi", config=mock_config, skill_registry=[], control_plane=cp, shadow=shadow))
    ))
    assert "Hello" in result.answer
