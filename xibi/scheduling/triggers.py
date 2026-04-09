from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Callable

TriggerCalculator = Callable[[dict, datetime], datetime]

_REGISTRY: dict[str, TriggerCalculator] = {}

def register_trigger(name: str):
    def deco(fn: TriggerCalculator) -> TriggerCalculator:
        _REGISTRY[name] = fn
        return fn
    return deco

def compute_next_run(trigger_type: str, config: dict, after: datetime) -> datetime:
    fn = _REGISTRY.get(trigger_type)
    if fn is None:
        raise ValueError(f"Unknown trigger type: {trigger_type}")
    return fn(config, after)

@register_trigger("interval")
def _interval(config: dict, after: datetime) -> datetime:
    every_seconds = config.get("every_seconds", 86400)
    # Jitter is ignored for now per spec (Step 59 basic implementation)
    return after + timedelta(seconds=every_seconds)

@register_trigger("oneshot")
def _oneshot(config: dict, after: datetime) -> datetime:
    """Returns 'at' on first call; returns datetime.max afterward."""
    at_str = config.get("at")
    if not at_str:
        return datetime.max

    try:
        at_dt = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
        # Ensure at_dt is timezone-aware if after is
        if after.tzinfo and not at_dt.tzinfo:
            at_dt = at_dt.replace(tzinfo=timezone.utc)
        elif not after.tzinfo and at_dt.tzinfo:
            at_dt = at_dt.replace(tzinfo=None)
    except ValueError:
        return datetime.max

    if after < at_dt:
        return at_dt

    return datetime.max

@register_trigger("cron")
def _cron(config: dict, after: datetime) -> datetime:
    raise NotImplementedError(
        "cron triggers ship in a follow-up spec. Use 'interval' for now."
    )
