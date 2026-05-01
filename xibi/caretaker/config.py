"""Caretaker configuration.

Config lives in-code — not in ``~/.xibi/config.yaml`` — because (a) the
caretaker config should not itself be subject to the drift it watches
for, and (b) changes should go through the usual spec + review path.

``ProviderHealthConfig`` introduces the first env-var override pattern
in caretaker (borrowed from ``xibi.heartbeat``'s ``XIBI_*`` style).
Overrides are resolved at construction time only — the dataclass
remains ``frozen=True``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceSilenceConfig:
    watched_operations: tuple[str, ...]
    silence_threshold_min: int = 30


@dataclass(frozen=True)
class ConfigDriftConfig:
    watched_paths: tuple[str, ...]


@dataclass(frozen=True)
class SchemaDriftConfig:
    enabled: bool = True


@dataclass(frozen=True)
class ProviderHealthConfig:
    degraded_threshold: float = 0.5
    reset_threshold: float = 0.2
    min_calls: int = 3
    window_hours: int = 24
    enabled: bool = True


def _provider_health_from_env() -> ProviderHealthConfig:
    """Build ProviderHealthConfig honoring XIBI_CARETAKER_PROVIDER_HEALTH_*.

    Resolved at construction time; the resulting dataclass is frozen.
    """
    enabled = os.environ.get("XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED", "1") != "0"
    threshold = float(os.environ.get("XIBI_CARETAKER_PROVIDER_HEALTH_THRESHOLD", "0.5"))
    reset = float(os.environ.get("XIBI_CARETAKER_PROVIDER_HEALTH_RESET", "0.2"))
    min_calls = int(os.environ.get("XIBI_CARETAKER_PROVIDER_HEALTH_MIN_CALLS", "3"))
    window_hours = int(os.environ.get("XIBI_CARETAKER_PROVIDER_HEALTH_WINDOW_HOURS", "24"))
    return ProviderHealthConfig(
        degraded_threshold=threshold,
        reset_threshold=reset,
        min_calls=min_calls,
        window_hours=window_hours,
        enabled=enabled,
    )


@dataclass(frozen=True)
class CaretakerConfig:
    pulse_interval_min: int = 15
    service_silence: ServiceSilenceConfig = field(
        default_factory=lambda: ServiceSilenceConfig(
            # DISABLED 2026-05-01 pending heartbeat-tick liveness substrate.
            #
            # PR #129 (commit 3498268) replaced phantom-name watched_operations
            # with three operations that ARE emitted in production:
            #   - extraction.smart_parse              (bursty per-email; 2h+ gaps normal on quiet evenings)
            #   - review_cycle.priority_context_apply (scheduled 3x daily UTC; 6-12h gaps EXPECTED)
            #   - scheduled_action.run                (on-demand; gaps of any length normal)
            # All three are intermittent. The 30-min silence_threshold_min
            # assumes constant high-cadence emission, which none of them
            # have, so service_silence false-fired on each within hours of
            # the PR #129 deploy (3 telegrams to operator on 2026-05-01).
            #
            # The check itself is correct; it needs a HIGH-CADENCE liveness
            # signal to watch. Empty tuple disables the check (it iterates
            # over watched_operations; zero entries → zero findings).
            #
            # Proper fix is parked at:
            #   tasks/backlog/notes/heartbeat-tick-span-addition.md
            # Add a heartbeat.tick span emit inside async_tick (~5-min
            # cadence), then restore watched_operations to ("heartbeat.tick",).
            watched_operations=(),
            silence_threshold_min=30,
        )
    )
    config_drift: ConfigDriftConfig = field(
        default_factory=lambda: ConfigDriftConfig(
            watched_paths=(
                "~/.xibi/config.json",
                "~/.xibi/config.yaml",
                "~/.xibi/secrets.env",
            ),
        )
    )
    schema_drift: SchemaDriftConfig = field(default_factory=SchemaDriftConfig)
    provider_health: ProviderHealthConfig = field(default_factory=_provider_health_from_env)


DEFAULTS = CaretakerConfig()
