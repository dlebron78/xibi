"""Provider-health check.

Reads ``inference_events`` over a configurable window, computes the
degraded-rate per ``(role, provider, model)`` group, and emits a
``Finding`` when degradation crosses ``cfg.degraded_threshold``.
Hysteresis (``reset_threshold`` < ``degraded_threshold``) prevents
flapping.

Mirrors the shape of ``service_silence.check`` — mechanical SQL +
arithmetic + state-aware emit, no LLM judgment. Recovery telegram is
out of scope for v1; the existing pulse-side ``resolve()`` deletes the
``caretaker_drift_state`` row silently when the rate falls below
``reset_threshold``.

Hysteresis state source: ``xibi.caretaker.dedup.seen_before(...)``.
This depends on ``pulse.py`` running the dedup state-machine loop
(``seen_before`` / ``record_finding`` / ``touch``) BEFORE the resolve
loop (``active_keys - observed_keys``) — the gray-zone Finding adds
its dedup_key to ``observed_keys`` and that protects the row from
deletion. Reordering the two loops in pulse.py would silently break
gray-zone keep-alert behavior (Scenario 6a in the spec).
"""

from __future__ import annotations

import logging
from pathlib import Path

from xibi.caretaker import dedup as _dedup
from xibi.caretaker.config import ProviderHealthConfig
from xibi.caretaker.finding import Finding, Severity
from xibi.db import open_db

logger = logging.getLogger(__name__)


def _last_success_text(last_success_at: str | None) -> str:
    return last_success_at if last_success_at else "never (in window)"


def check(db_path: Path, cfg: ProviderHealthConfig) -> list[Finding]:
    """One Finding per ``(role, model)`` whose degraded-rate trips
    the state-aware threshold.

    Decision tree per ``(role, provider, model)`` group:
      - ``total_calls < cfg.min_calls`` → INFO log ``skipped``, no Finding.
      - ``was_alerted = dedup.seen_before(db_path, dedup_key)``:
          * ``was_alerted`` and ``rate >= cfg.reset_threshold`` → emit Finding
            (keeps alert active across the gray zone).
          * ``was_alerted`` and ``rate < cfg.reset_threshold`` → emit NO Finding;
            pulse-side resolve will delete the row.
          * ``not was_alerted`` and ``rate >= cfg.degraded_threshold`` →
            new alert, emit Finding.
          * ``not was_alerted`` and ``rate < cfg.degraded_threshold`` →
            no Finding (gray zone or healthy).

    Honors ``cfg.enabled`` (which is set from
    ``XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED`` at config construction).
    Validates ``cfg.reset_threshold < cfg.degraded_threshold``.
    """
    if not cfg.enabled:
        logger.info("provider_health: disabled via env")
        return []

    if not (cfg.reset_threshold < cfg.degraded_threshold):
        logger.error(
            "provider_health: invalid config — reset_threshold (%s) must be < degraded_threshold (%s); returning no findings",
            cfg.reset_threshold,
            cfg.degraded_threshold,
        )
        return []

    window_offset = f"-{cfg.window_hours} hours"
    logger.info(
        "provider_health: examining (role, model) pairs in last %sh",
        cfg.window_hours,
    )

    try:
        with open_db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT role,
                       provider,
                       model,
                       COUNT(*)                                                  AS total_calls,
                       SUM(CASE WHEN degraded = 1 THEN 1 ELSE 0 END)             AS degraded_count,
                       MAX(CASE WHEN degraded = 0 THEN recorded_at ELSE NULL END) AS last_success_at
                  FROM inference_events
                 WHERE recorded_at > datetime('now', ?)
              GROUP BY role, provider, model
                """,
                (window_offset,),
            ).fetchall()
    except Exception:
        logger.exception("provider_health: failed to read inference_events; no findings emitted")
        return []

    findings: list[Finding] = []
    for role, provider, model, total, degraded, last_success_at in rows:
        if total < cfg.min_calls:
            logger.info(
                "provider_health: skipped role=%s total_calls=%d below min_calls=%d",
                role,
                total,
                cfg.min_calls,
            )
            continue

        try:
            rate = float(degraded) / float(total)
        except (TypeError, ZeroDivisionError):
            logger.exception(
                "provider_health: rate computation failed for role=%s provider=%s model=%s",
                role,
                provider,
                model,
            )
            continue

        pct = rate * 100.0
        logger.info(
            "provider_health: role=%s model=%s degraded_rate=%.1f%% calls=%d",
            role,
            model,
            pct,
            total,
        )

        dedup_key = f"provider_health:{role}:{model}"
        try:
            was_alerted = _dedup.seen_before(db_path, dedup_key)
        except Exception:
            logger.exception(
                "provider_health: dedup.seen_before failed for %s; assuming not alerted",
                dedup_key,
            )
            was_alerted = False

        if was_alerted:
            if rate < cfg.reset_threshold:
                continue
        else:
            if rate < cfg.degraded_threshold:
                continue

        message = (
            f"{role} role degradation\n"
            f"Provider: {provider} / {model}\n"
            f"Last {cfg.window_hours}h: {int(degraded)}/{int(total)} calls degraded ({pct:.0f}%)\n"
            f"Last successful: {_last_success_text(last_success_at)}\n"
            f"Likely: credit exhaustion or API issue. "
            f"Check console.anthropic.com / ~/.xibi/secrets.env"
        )
        logger.warning(
            "provider_health: ALERT role=%s model=%s rate=%.1f%%",
            role,
            model,
            pct,
        )
        findings.append(
            Finding(
                check_name="provider_health",
                severity=Severity.CRITICAL,
                dedup_key=dedup_key,
                message=message,
                metadata={
                    "role": role,
                    "provider": provider,
                    "model": model,
                    "degraded_count": int(degraded),
                    "total_calls": int(total),
                    "degraded_rate": rate,
                    "last_success_at": last_success_at,
                },
            )
        )

    return findings
