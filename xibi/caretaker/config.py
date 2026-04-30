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
            watched_operations=(
                "heartbeat.tick.observation",
                "heartbeat.tick.reflection",
                "telegram.poll",
                "telegram.send",
            ),
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
