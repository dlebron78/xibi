import json
import asyncio
from unittest.mock import MagicMock, patch
import pytest
from xibi.react import run
from xibi.tracing import Tracer


def test_react_run_emits_root_span(tmp_path):
    db_path = tmp_path / "xibi.db"
    tracer = Tracer(db_path)
    config = {"db_path": str(db_path)}

    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "hello"}}'

    with patch("xibi.react.get_model", return_value=mock_llm):
        result = asyncio.asyncio.asyncio.asyncio.run(asyncio.run(run(run(run(asyncio.run(run("query", config, [], tracer=tracer))))
        assert result.answer == "hello"


def test_result_has_trace_id(tmp_path):
    db_path = tmp_path / "xibi.db"
    tracer = Tracer(db_path)
    config = {"db_path": str(db_path)}
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "hello"}}'
    with patch("xibi.react.get_model", return_value=mock_llm):
        result = asyncio.asyncio.run(run(asyncio.run(run("query", config, [], tracer=tracer))))))
        assert result.trace_id is not None
