"""Caretaker configuration.

Config lives in-code — not in ``~/.xibi/config.yaml`` — because (a) the
caretaker config should not itself be subject to the drift it watches
for, and (b) changes should go through the usual spec + review path.
"""

from __future__ import annotations

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


DEFAULTS = CaretakerConfig()
