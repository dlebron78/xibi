from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from xibi.errors import XibiError


@dataclass
class Step:
    step_num: int
    thought: str = ""
    tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    parse_warning: str | None = None
    error: XibiError | None = None

    def full_text(self) -> str:
        """Full detail — injected for the most recent steps."""
        out = str(self.tool_output)
        if len(out) > 4000:
            out = out[:4000] + "... [truncated]"
        return (
            f"Step {self.step_num}:\n"
            f"  Thought: {self.thought}\n"
            f"  Action: {self.tool}\n"
            f"  Input: {json.dumps(self.tool_input, separators=(',', ':'))}\n"
            f"  Output: {out}"
        )

    def one_line_summary(self) -> str:
        """Compressed summary for older steps — preserves key data."""
        input_summary = json.dumps(self.tool_input, separators=(",", ":"))[:100]
        if self.tool_output.get("status") == "error":
            output_hint = f"ERROR: {self.tool_output.get('message', '?')[:120]}"
        elif self.tool_output.get("content"):
            output_hint = str(self.tool_output["content"])[:400]
        else:
            output_hint = str(self.tool_output)[:400]
        return f"Step {self.step_num}: {self.tool}({input_summary}) → {output_hint}"


@dataclass
class ReActResult:
    answer: str
    steps: list[Step]
    exit_reason: Literal["finish", "ask_user", "max_steps", "timeout", "error"]
    duration_ms: int
    error_summary: list[XibiError] = field(default_factory=list)
    trace_id: str | None = None

    def user_facing_failure_message(self) -> str:
        """
        Returns a single user-safe string summarising the failure.
        Called by CLI and Telegram when answer is empty.
        """
        if not self.error_summary:
            match self.exit_reason:
                case "timeout":
                    return "That took too long. Try a simpler request."
                case "max_steps":
                    return (
                        "I hit my reasoning limit without a clear answer. Try breaking the request into smaller parts."
                    )
                case _:
                    return "Something went wrong. Please try again."
        # Surface the most recent / highest-priority error
        err = self.error_summary[-1]
        return err.user_message()
