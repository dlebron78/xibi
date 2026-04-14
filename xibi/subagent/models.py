from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SubagentRun:
    id: str
    agent_id: str
    status: str  # SPAWNED | RUNNING | DONE | FAILED | TIMEOUT | CANCELLED
    trigger: str  # review_cycle | scheduled | telegram | manual
    trigger_context: dict[str, Any] = field(default_factory=dict)
    scoped_input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    error_detail: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_reason: str | None = None
    budget_max_calls: int = 0
    budget_max_cost_usd: float = 0.0
    budget_max_duration_s: int = 0
    actual_calls: int = 0
    actual_cost_usd: float = 0.0
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ChecklistStep:
    id: str
    run_id: str
    step_order: int
    skill_name: str
    status: str  # PENDING | RUNNING | DONE | FAILED | SKIPPED
    model: str | None = None
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error_detail: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


@dataclass
class PendingL2Action:
    id: str
    run_id: str
    step_id: str | None
    tool: str
    args: dict[str, Any]
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CostEvent:
    id: str
    run_id: str
    step_id: str | None
    model: str
    provider: str = "anthropic"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
