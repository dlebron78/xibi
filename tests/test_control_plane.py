import asyncio
from unittest.mock import MagicMock

from xibi.react import run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter


def test_greet_hello():
    router = ControlPlaneRouter()
    decision = router.match("hello")
    assert decision.intent == "greet"
    assert decision.confident is True


def test_greet_good_morning():
    router = ControlPlaneRouter()
    decision = router.match("good morning")
    assert decision.intent == "greet"
    assert decision.confident is True


def test_greet_no_match_long_sentence():
    router = ControlPlaneRouter()
    decision = router.match("hello I need help with email")
    assert decision.confident is False


def test_status_check():
    router = ControlPlaneRouter()
    decision = router.match("ping")
    assert decision.intent == "status_check"
    assert decision.confident is True


def test_reset():
    router = ControlPlaneRouter()
    decision = router.match("/reset")
    assert decision.intent == "reset"
    assert decision.confident is True


def test_capability_check():
    router = ControlPlaneRouter()
    decision = router.match("what tools do you have")
    assert decision.intent == "capability_check"
    assert decision.confident is True


def test_update_assistant_name():
    router = ControlPlaneRouter()
    decision = router.match("call yourself Aria")
    assert decision.intent == "update_assistant_name"
    assert decision.params == {"name": "Aria"}


def test_update_user_name():
    router = ControlPlaneRouter()
    decision = router.match("my name is Daniel")
    assert decision.intent == "update_user_name"
    assert decision.params == {"name": "Daniel"}


def test_name_too_long_fail_closed():
    router = ControlPlaneRouter()
    decision = router.match("call yourself a very long implausible name here")
    assert decision.confident is False


def test_no_match_falls_through():
    router = ControlPlaneRouter()
    decision = router.match("find the latest invoice from Acme")
    assert decision.confident is False


def test_register_custom_pattern():
    router = ControlPlaneRouter()
    router.register(r"custom pattern", "custom_intent")
    decision = router.match("custom pattern")
    assert decision.intent == "custom_intent"
    assert decision.confident is True


def test_react_run_with_control_plane_intercepts():
    router = ControlPlaneRouter()
    config = Config(providers={})
    # ReAct should return without calling LLM (mock LLM not even needed)
    result = asyncio.asyncio.run(
        asyncio.run(run(asyncio.run(run(query="hello", config=config, skill_registry=[], control_plane=router)))
    )
    assert result.answer == "Hello! How can I help?"
    assert result.steps == []
    assert result.exit_reason == "finish"


def test_react_run_with_control_plane_falls_through(monkeypatch):
    router = ControlPlaneRouter()
    config = Config(providers={})

    # Mock get_model to return a mock LLM
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "test", "tool": "finish", "tool_input": {"answer": "react response"}}'
    monkeypatch.setattr("xibi.react.get_model", lambda **kwargs: mock_llm)

    result = asyncio.asyncio.run(
        run(asyncio.run(run(query="find invoice", config=config, skill_registry=[], control_plane=router)))
    )
    assert result.answer == "react response"
    assert len(result.steps) == 0  # finish is a pseudo-tool, not appended to scratchpad
    assert result.exit_reason == "finish"
