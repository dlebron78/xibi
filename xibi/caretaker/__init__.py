"""Caretaker: failure-visibility watchdog.

Independent pulse-driven watcher (runs in its own systemd unit) that
detects silent failures across three classes — service silence, config
drift, schema drift — and emits deduplicated telegram alerts.
"""

from xibi.caretaker.finding import Finding, Severity
from xibi.caretaker.pulse import Caretaker, PulseResult

__all__ = ["Caretaker", "Finding", "PulseResult", "Severity"]
