"""Step-60 §1: Tracing gap fix — provider telemetry must fire on failure."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from xibi.db import migrate, open_db
from xibi.errors import ErrorCategory, XibiError
from xibi.router import (
    OllamaClient,
    _active_db_path,
    _active_trace,
    _active_tracer,
    init_telemetry,
    set_trace_context,
    clear_trace_context,
)
from xibi.tracing import Tracer


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    return db_path


@pytest.fixture
def trace_ctx(db: Path):
    tracer = Tracer(db)
    init_telemetry(db, tracer=tracer)
    set_trace_context(trace_id="t-test", span_id="s-parent", operation="react_step")
    yield tracer
    clear_trace_context()
    _active_db_path.set(None)
    _active_tracer.set(None)


def test_failure_emits_span_with_error_status(db: Path, trace_ctx: Tracer) -> None:
    client = OllamaClient(provider="ollama", model="qwen3.5:4b", options={}, base_url="http://localhost:11434")
    client._role = "fast"

    with patch("xibi.router.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("connection refused")
        with pytest.raises(XibiError) as excinfo:
            client.generate("hello", system=None)
        assert excinfo.value.category == ErrorCategory.PROVIDER_DOWN

    with open_db(db) as conn:
        rows = list(conn.execute(
            "SELECT status, attributes FROM spans WHERE trace_id = 't-test' AND operation = 'llm.generate'"
        ))
    assert len(rows) == 1
    status, attrs = rows[0]
    assert status == "error"
    assert "error.category" in attrs
    assert "provider_down" in attrs


def test_failure_records_inference_event_with_degraded_flag(db: Path, trace_ctx: Tracer) -> None:
    client = OllamaClient(provider="ollama", model="qwen3.5:4b", options={}, base_url="http://localhost:11434")
    client._role = "fast"

    with patch("xibi.router.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.Timeout("nope")
        with pytest.raises(XibiError):
            client.generate("hello", system=None)

    with open_db(db) as conn:
        rows = list(conn.execute(
            "SELECT degraded FROM inference_events WHERE trace_id = 't-test'"
        ))
    assert len(rows) == 1
    assert rows[0][0] == 1


def test_success_path_unchanged(db: Path, trace_ctx: Tracer) -> None:
    client = OllamaClient(provider="ollama", model="qwen3.5:4b", options={}, base_url="http://localhost:11434")
    client._role = "fast"

    fake_response = MagicMock()
    fake_response.json.return_value = {"response": "hi", "prompt_eval_count": 5, "eval_count": 3}
    fake_response.raise_for_status = MagicMock()

    with patch("xibi.router.requests.post", return_value=fake_response):
        out = client.generate("hello", system=None)
    assert out == "hi"

    with open_db(db) as conn:
        spans = list(conn.execute(
            "SELECT status, attributes FROM spans WHERE trace_id = 't-test' AND operation = 'llm.generate'"
        ))
        events = list(conn.execute(
            "SELECT degraded FROM inference_events WHERE trace_id = 't-test'"
        ))
    assert len(spans) == 1
    assert spans[0][0] == "ok"
    assert "error.category" not in spans[0][1]
    assert events[0][0] == 0
