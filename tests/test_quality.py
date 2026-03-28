from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from xibi.quality import quality_score_span
from xibi.tracing import Tracer


@pytest.fixture
def mock_config():
    return {
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "llama3", "options": {}}
            }
        },
        "providers": {
            "ollama": {"base_url": "http://localhost:11434"}
        }
    }


def test_quality_score_high_relevance_and_groundedness(mock_config):
    mock_model = MagicMock()
    mock_model.generate_structured.return_value = {
        "relevance": 5,
        "groundedness": 4,
        "reasoning": "Answer directly addresses the query."
    }

    with patch("xibi.quality.get_model", return_value=mock_model):
        score = quality_score_span(
            "what emails do I have?",
            "You have 3 unread emails.",
            [],
            mock_config,
            {"environment": "dev"}
        )

    assert score is not None
    assert score.relevance == 5
    assert score.groundedness == 4
    assert score.composite == 4.6
    assert score.reasoning == "Answer directly addresses the query."


def test_quality_score_skipped_in_test_env(mock_config):
    with patch("xibi.quality.get_model") as mock_get_model:
        score = quality_score_span(
            "anything",
            "anything",
            [],
            mock_config,
            {"environment": "test"}
        )

    assert score is None
    mock_get_model.assert_not_called()


def test_quality_score_model_error_returns_none(mock_config):
    with patch("xibi.quality.get_model", side_effect=RuntimeError("unavailable")):
        score = quality_score_span(
            "query",
            "answer",
            [],
            mock_config,
            {"environment": "dev"}
        )

    assert score is None


def test_quality_score_out_of_range_returns_none(mock_config):
    mock_model = MagicMock()
    mock_model.generate_structured.return_value = {
        "relevance": 7,
        "groundedness": 2,
        "reasoning": "..."
    }

    with patch("xibi.quality.get_model", return_value=mock_model):
        score = quality_score_span(
            "query",
            "answer",
            [],
            mock_config,
            {"environment": "dev"}
        )

    assert score is None


def test_quality_score_composite_calculation(mock_config):
    mock_model = MagicMock()
    mock_model.generate_structured.return_value = {
        "relevance": 4,
        "groundedness": 2,
        "reasoning": "..."
    }

    with patch("xibi.quality.get_model", return_value=mock_model):
        score = quality_score_span(
            "query",
            "answer",
            [],
            mock_config,
            {"environment": "dev"}
        )

    assert score is not None
    # 4 * 0.6 + 2 * 0.4 = 2.4 + 0.8 = 3.2
    assert score.composite == 3.2


def test_tracer_record_quality(tmp_path):
    db_path = tmp_path / "xibi.db"
    tracer = Tracer(db_path)

    from xibi.quality import QualityScore
    score = QualityScore(relevance=4, groundedness=3, composite=3.6, reasoning="ok")

    tracer.record_quality("trace-abc", score, "test query")

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM spans WHERE operation = 'quality.judge'").fetchone()

    assert row is not None
    assert row["trace_id"] == "trace-abc"
    assert row["component"] == "quality"

    attrs = json.loads(row["attributes"])
    assert attrs["relevance"] == 4
    assert attrs["groundedness"] == 3
    assert attrs["composite"] == 3.6
    assert attrs["reasoning"] == "ok"
    assert attrs["query_preview"] == "test query"


def test_apply_quality_to_trust_failure():
    from xibi.quality import QualityScore, apply_quality_to_trust
    from xibi.trust.gradient import FailureType

    mock_trust = MagicMock()
    score = QualityScore(relevance=2, groundedness=2, composite=2.0, reasoning="Poor answer")

    apply_quality_to_trust(score, mock_trust, "text", "fast")

    mock_trust.record_failure.assert_called_once_with("text", "fast", FailureType.QUALITY_DEGRADATION)


def test_apply_quality_to_trust_success():
    from xibi.quality import QualityScore, apply_quality_to_trust

    mock_trust = MagicMock()
    score = QualityScore(relevance=4, groundedness=4, composite=4.0, reasoning="Good answer")

    apply_quality_to_trust(score, mock_trust, "text", "fast")

    mock_trust.record_success.assert_called_once_with("text", "fast")


def test_apply_quality_to_trust_neutral_zone():
    from xibi.quality import QualityScore, apply_quality_to_trust

    mock_trust = MagicMock()
    # 3.0 is between 2.5 and 3.5
    score = QualityScore(relevance=3, groundedness=3, composite=3.0, reasoning="Average answer")

    apply_quality_to_trust(score, mock_trust, "text", "fast")

    mock_trust.record_failure.assert_not_called()
    mock_trust.record_success.assert_not_called()


def test_apply_quality_to_trust_never_raises():
    from xibi.quality import QualityScore, apply_quality_to_trust

    mock_trust = MagicMock()
    mock_trust.record_success.side_effect = RuntimeError("DB error")
    score = QualityScore(relevance=5, groundedness=5, composite=5.0, reasoning="Excellent")

    # Should not raise
    apply_quality_to_trust(score, mock_trust, "text", "fast")
