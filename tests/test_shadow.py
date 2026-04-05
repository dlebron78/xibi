import asyncio
import json
from unittest.mock import MagicMock

import pytest

from xibi.react import run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher

# --- BM25 scoring tests ---


def test_exact_phrase_match_is_direct():
    matcher = ShadowMatcher()
    matcher.build_corpus([("skill1", "tool1", "get weather")])
    match = matcher.match("get weather")
    assert match is not None
    assert match.tool == "tool1"
    assert match.tier == "direct"
    assert match.score >= 0.85


def test_partial_match_is_hint():
    matcher = ShadowMatcher()
    matcher.build_corpus([("skill1", "tool1", "get weather today")])
    # 2/3 = 0.666 -> tier "hint"
    match = matcher.match("get weather")
    assert match is not None
    assert match.tool == "tool1"
    assert match.tier == "hint"


def test_no_match_returns_none():
    matcher = ShadowMatcher()
    matcher.build_corpus([("skill1", "tool1", "get weather")])
    match = matcher.match("send an email")
    assert match is None


def test_empty_corpus_returns_none():
    matcher = ShadowMatcher()
    matcher.build_corpus([])
    match = matcher.match("anything")
    assert match is None


# --- Corpus building tests ---


def test_build_corpus_sets_avg_doc_length():
    matcher = ShadowMatcher()
    matcher.build_corpus([("s1", "t1", "one two"), ("s1", "t2", "one two three four")])
    assert matcher.avg_doc_length == 3.0


def test_duplicate_documents_handled():
    matcher = ShadowMatcher()
    matcher.build_corpus([("s1", "t1", "get weather"), ("s1", "t1", "get weather")])
    assert len(matcher.documents) == 2
    match = matcher.match("get weather")
    assert match is not None


def test_single_token_query():
    matcher = ShadowMatcher()
    matcher.build_corpus([("s1", "t1", "weather")])
    match = matcher.match("weather")
    assert match is not None
    assert match.tier == "direct"


# --- Manifest loading tests ---


def test_load_manifests_builds_corpus(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "weather"
    skill_dir.mkdir(parents=True)
    manifest = {
        "name": "weather",
        "tools": [{"name": "get_weather", "examples": ["what is the weather like -> tool_call"]}],
    }
    with open(skill_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    matcher = ShadowMatcher()
    matcher.load_manifests(skills_dir)
    match = matcher.match("what is the weather like")
    assert match is not None
    assert match.tool == "get_weather"


def test_load_manifests_missing_dir():
    matcher = ShadowMatcher()
    matcher.load_manifests("/nonexistent/path/at/all")
    assert len(matcher.documents) == 0


def test_load_manifests_skips_malformed(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Valid manifest
    s1 = skills_dir / "s1"
    s1.mkdir()
    with open(s1 / "manifest.json", "w") as f:
        json.dump({"tools": [{"name": "t1", "examples": ["phrase1"]}]}, f)

    # Malformed manifest
    s2 = skills_dir / "s2"
    s2.mkdir()
    with open(s2 / "manifest.json", "w") as f:
        f.write("{ malformed json")

    matcher = ShadowMatcher()
    matcher.load_manifests(skills_dir)
    assert len(matcher.documents) == 1
    assert matcher.documents[0][1] == "t1"


# --- Threshold tests ---


def test_score_below_hint_threshold_returns_none():
    matcher = ShadowMatcher()
    # "very long phrase with many words" vs "phrase"
    matcher.build_corpus([("s1", "t1", "one two three four five six seven eight nine ten")])
    # Scoring "one" against that might be very low normalized score
    match = matcher.match("one")
    # If it's below 0.65, it should be None
    if match:
        assert match.score < 0.65
        raise AssertionError(f"Should have been None, but got {match.tier} with score {match.score}")
    else:
        assert True


def test_score_between_thresholds_is_hint():
    matcher = ShadowMatcher()
    # 3/4 = 0.75 -> hint
    matcher.build_corpus([("s1", "t1", "get the weather today")])
    match = matcher.match("get the weather")
    assert match is not None
    assert 0.65 <= match.score < 0.85
    assert match.tier == "hint"


def test_score_above_direct_threshold_is_direct():
    matcher = ShadowMatcher()
    matcher.build_corpus([("s1", "t1", "get weather")])
    match = matcher.match("get weather")
    assert match is not None
    assert match.score >= 0.85
    assert match.tier == "direct"


# --- ReAct integration tests ---


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    return config


def test_react_shadow_direct_calls_tool(mock_config):
    skill_registry = [{"name": "get_weather"}]
    executor = MagicMock()
    executor.execute.return_value = {"status": "ok", "content": "Sunny"}

    shadow = ShadowMatcher()
    shadow.build_corpus([("weather", "get_weather", "what is the weather")])

    result = asyncio.run(run(
        query="what is the weather", config=mock_config, skill_registry=skill_registry, executor=executor, shadow=shadow
    ))

    assert result.answer == "Sunny"
    assert result.exit_reason == "finish"
    assert len(result.steps) == 0
    executor.execute.assert_called_once_with("get_weather", {})


def test_react_shadow_hint_prepends_context(mock_config, monkeypatch):
    skill_registry = [{"name": "get_weather"}]

    shadow = ShadowMatcher()
    shadow.build_corpus([("weather", "get_weather", "get weather today")])

    # We want to check if context in the prompt includes the hint
    # We can mock get_model and its generate method
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "using tool", "tool": "finish", "tool_input": {"answer": "done"}}'

    def mock_get_model(*args, **kwargs):
        return mock_llm

    import xibi.react

    monkeypatch.setattr(xibi.react, "get_model", mock_get_model)

    asyncio.run(run(
        query="get weather",
        config=mock_config,
        skill_registry=skill_registry,
        shadow=shadow,
        context="original context",
    ))

    # Check if prompt passed to generate contains the hint
    args, kwargs = mock_llm.generate.call_args
    prompt = args[0]
    assert "[Shadow hint: consider using get_weather]" in prompt
    assert "original context" in prompt


def test_react_shadow_none_falls_through(mock_config, monkeypatch):
    import xibi.react

    skill_registry = [{"name": "get_weather"}]
    shadow = ShadowMatcher()
    shadow.build_corpus([("weather", "get_weather", "get weather")])

    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "using tool", "tool": "finish", "tool_input": {"answer": "done"}}'
    monkeypatch.setattr(xibi.react, "get_model", lambda *args, **kwargs: mock_llm)

    asyncio.run(run(query="unrelated query", config=mock_config, skill_registry=skill_registry, shadow=shadow))

    args, kwargs = mock_llm.generate.call_args
    prompt = args[0]
    assert "[Shadow hint:" not in prompt


def test_react_shadow_after_control_plane(mock_config):
    # Control plane should match "hi"
    cp = ControlPlaneRouter()

    shadow = MagicMock(spec=ShadowMatcher)
    # Even if shadow would match, it shouldn't be called if CP matches

    result = asyncio.run(run(query="hi", config=mock_config, skill_registry=[], control_plane=cp, shadow=shadow))

    assert "Hello" in result.answer
    shadow.match.assert_not_called()
