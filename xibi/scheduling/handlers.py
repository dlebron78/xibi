from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.executor import Executor


@dataclass
class ExecutionContext:
    action_id: str
    name: str
    trust_tier: str
    executor: Executor
    db_path: Path
    trace_id: str


@dataclass
class HandlerResult:
    status: str  # 'success' | 'error'
    output_preview: str  # truncated to 500 chars by kernel
    error: str | None = None


ActionHandler = Callable[[dict, ExecutionContext], HandlerResult]

_REGISTRY: dict[str, ActionHandler] = {}


def register_handler(name: str) -> Callable[[ActionHandler], ActionHandler]:
    def deco(fn: ActionHandler) -> ActionHandler:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_handler(name: str) -> ActionHandler | None:
    return _REGISTRY.get(name)


@register_handler("tool_call")
def _tool_call(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    tool = action_config.get("tool")
    if not tool:
        return HandlerResult("error", "", "Missing 'tool' in action_config")
    args = action_config.get("args", {})

    try:
        # Set trace context so tool calls are linked
        from xibi.router import set_trace_context

        set_trace_context(trace_id=ctx.trace_id, span_id=None, operation=f"scheduled_tool:{tool}")

        result = ctx.executor.execute(tool, args)

        from xibi.router import clear_trace_context

        clear_trace_context()

        if result.get("status") == "error":
            return HandlerResult("error", str(result)[:500], result.get("error") or result.get("message"))

        # Pull success result
        res_val = result.get("result", result)
        return HandlerResult("success", str(res_val)[:500])
    except Exception as e:
        return HandlerResult("error", "", f"{type(e).__name__}: {str(e)}")


_INTERNAL_HOOKS: dict[str, Callable[[dict, ExecutionContext], HandlerResult]] = {}


def register_internal_hook(name: str, fn: Callable[[dict, ExecutionContext], HandlerResult]) -> None:
    _INTERNAL_HOOKS[name] = fn


def _handle_send_reminder(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Internal hook: send a reminder message via Telegram."""
    text = action_config.get("text", "Reminder")
    from xibi.telegram.api import send_nudge

    try:
        # Step 66: Added reminder category
        send_nudge(f"⏰ Reminder: {text}", category="reminder")
        return HandlerResult("success", f"Reminder sent: {text}")
    except Exception as e:
        return HandlerResult("error", "", f"Failed to send reminder: {e}")


register_internal_hook("send_reminder", _handle_send_reminder)


@register_handler("internal_hook")
def _internal_hook(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    hook_name = action_config.get("hook")
    if not hook_name:
        return HandlerResult("error", "", "Missing 'hook' in action_config")

    hook_fn = _INTERNAL_HOOKS.get(hook_name)
    if not hook_fn:
        return HandlerResult("error", "", f"Unknown internal_hook: {hook_name}")

    try:
        return hook_fn(action_config.get("args", {}), ctx)
    except Exception as e:
        return HandlerResult("error", "", f"Hook error: {str(e)}")
