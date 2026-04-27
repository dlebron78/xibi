"""tool.dispatch summarize_email span carries received_via_account.

Per step-109 TRR condition #8, the executor must lift `received_via_account`
from the tool's `data` dict into span attributes so the PDV decomposition
query (Observability section of the spec) returns rows. The lift is a
narrow, whitelist-of-tools mirror — only `summarize_email` is enrolled.
"""

from __future__ import annotations


def _emit_span_for(tool_name: str, result: dict) -> dict | None:
    """Run Executor._emit_tool_span and capture the span attributes dict.

    Sidesteps DB / SkillRegistry by stubbing what _emit_tool_span needs.
    """
    from xibi.executor import Executor
    from xibi.router import _active_trace, _active_tracer

    captured: dict = {}

    class _Tracer:
        def new_span_id(self):
            return "span-1"

        def emit(self, span):
            captured["attributes"] = span.attributes
            captured["operation"] = span.operation

    trace_token = _active_trace.set({"trace_id": "trace-1", "parent_span_id": None})
    tracer_token = _active_tracer.set(_Tracer())
    try:
        executor = Executor.__new__(Executor)
        executor.mcp_executor = None
        executor._emit_tool_span(
            tool_name=tool_name,
            tool_input={"email_id": "1"},
            result=result,
            duration_ms=10,
            is_mcp=False,
            server_name="",
            start_ms=0,
        )
    finally:
        _active_trace.reset(trace_token)
        _active_tracer.reset(tracer_token)
    return captured.get("attributes")


def test_summarize_email_span_includes_received_via_account():
    attrs = _emit_span_for(
        "summarize_email",
        {"status": "success", "data": {"received_via_account": "afya"}},
    )
    assert attrs is not None
    assert attrs["received_via_account"] == "afya"
    assert attrs["tool"] == "summarize_email"


def test_summarize_email_span_omits_attribute_when_no_match():
    attrs = _emit_span_for(
        "summarize_email",
        {"status": "success", "data": {"received_via_account": None}},
    )
    assert attrs is not None
    # None matches → attribute not surfaced (avoid noisy NULL rows)
    assert "received_via_account" not in attrs


def test_other_tools_do_not_get_provenance_attribute():
    """Whitelist guard: only summarize_email is enrolled."""
    attrs = _emit_span_for(
        "list_unread",
        {"status": "success", "data": {"received_via_account": "afya"}},
    )
    assert attrs is not None
    assert "received_via_account" not in attrs
