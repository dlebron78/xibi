from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Step:
    step_num: int
    thought: str = ""
    tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    parse_warning: str | None = None

    def full_text(self) -> str:
        """Full detail — injected for the 2 most recent steps."""
        out = str(self.tool_output)
        if len(out) > 800:
            out = out[:800] + "... [truncated]"
        return (
            f"Step {self.step_num}:\n"
            f"  Thought: {self.thought}\n"
            f"  Action: {self.tool}\n"
            f"  Input: {json.dumps(self.tool_input, separators=(',', ':'))}\n"
            f"  Output: {out}"
        )

    def one_line_summary(self) -> str:
        """Compressed one-liner for older steps."""
        input_summary = json.dumps(self.tool_input, separators=(",", ":"))[:60]
        if self.tool_output.get("status") == "error":
            output_hint = f"ERROR: {self.tool_output.get('message', '?')[:60]}"
        elif self.tool_output.get("content"):
            output_hint = str(self.tool_output["content"])[:80]
        else:
            output_hint = str(self.tool_output)[:80]
        return f"Step {self.step_num}: {self.tool}({input_summary}) → {output_hint}"


@dataclass
class ReActResult:
    answer: str
    steps: list[Step]
    exit_reason: Literal["finish", "ask_user", "max_steps", "timeout", "error"]
    duration_ms: int
