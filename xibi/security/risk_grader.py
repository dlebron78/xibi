"""Shadow-mode risk scoring for trust-gated text (step-131, Layer 1).

Every piece of external text that flows through
:func:`xibi.security.trust_gate.trust_gate` is graded for injection risk
on the side. Grades never gate or alter signals -- this layer accumulates
structured log data so operators can review distributions before deciding
whether to feed grades into the classifier prompt (Phase B) or enable
gating (Phase A+3).

The grader's unique value is the set of signals the sanitizer cannot
provide:

- structural anomaly detection (base64 blocks, homoglyphs, invisible
  unicode, whitespace abuse),
- sender trust context (passed through from
  :func:`xibi.heartbeat.sender_trust.assess_sender_trust`),
- a composite that blends the structural sub-score with the sanitizer's
  binary "did this text trip my hardcoded patterns?" answer.

Phrase- and token-level injection matching is deliberately omitted to
avoid maintaining a second vocabulary alongside
:mod:`xibi.security.sanitize`. The grader receives the sanitizer's verdict
as a boolean (``sanitizer_flagged``) and uses it as one of three sub-scores.

The composite is mapped to a level via two thresholds:

- ``composite < low``  -> ``LOW``
- ``composite < high`` -> ``MEDIUM``
- otherwise            -> ``HIGH``

Defaults are tuned so ``HIGH`` requires sanitizer-flagged AND multiple
structural anomalies AND a poor sender tier, keeping the noise floor low
during the shadow-log review window.

This module is pure stdlib (re, unicodedata, dataclasses, logging). It
never raises: any internal error logs a WARNING and returns ``None`` so
the trust gate can continue processing the signal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# -- Public dataclass --------------------------------------------------------


@dataclass(frozen=True)
class RiskGrade:
    """Result of grading one piece of external text.

    All sub-score fields (``sanitizer_score``, ``structural_score``,
    ``sender_modifier``) are **raw** values, pre-weight. The operator
    knows the weights from config and can verify:
    ``composite = sanitizer_score * w_s + structural_score * w_st +
    sender_modifier * w_sender`` (clamped to ``[0.0, 1.0]``).
    """

    composite: float
    level: str
    sanitizer_flagged: bool
    sanitizer_score: float
    structural_score: float
    structural_flags: list[str] = field(default_factory=list)
    sender_modifier: float = 0.0
    sender_tier: str = ""


# -- Hardcoded defaults ------------------------------------------------------

# Structural detector names, in stable display order for the log line.
_ALL_STRUCTURAL_FLAGS: tuple[str, ...] = (
    "base64_blocks",
    "homoglyph_chars",
    "invisible_unicode",
    "excessive_whitespace",
)

_DEFAULT_WEIGHTS: dict[str, float] = {
    "sanitizer": 0.5,
    "structural": 0.35,
    "sender": 0.15,
}

# Per TRR condition C2: only the ``low`` and ``high`` thresholds are
# honored. The composite-table mapping uses two boundaries
# (``< low -> LOW``, ``< high -> MEDIUM``, ``else -> HIGH``); a third
# ``medium`` key would have no effect, so it is intentionally absent
# from the defaults. User configs that include ``medium`` are accepted
# but ignored (forward-compat with the spec's documented YAML example).
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "low": 0.2,
    "high": 0.8,
}

# Sender-trust tier -> raw modifier (pre-weight). An empty string means
# "no sender model at this call site" (MCP/subagent/calendar) and stays
# neutral. Unknown tiers fall through to neutral with a WARNING.
_SENDER_TIER_MODIFIERS: dict[str, float] = {
    "ESTABLISHED": -0.1,
    "RECOGNIZED": 0.0,
    "": 0.0,
    "UNKNOWN": 0.1,
    "NAME_MISMATCH": 0.2,
}


# -- Structural detectors ----------------------------------------------------

# Base64-encoded blocks of >=100 chars (with optional ``=``/``==``
# padding). Legitimate base64 in email bodies is rare outside
# attachments, which are stripped before trust_gate sees them.
_BASE64_BLOCK_RE = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")

# Hand-picked Cyrillic and Greek lookalikes for Latin letters. Kept
# small (18 chars) to avoid false positives; can be extended via a
# future spec if shadow logs show misses.
_HOMOGLYPH_CHARS: frozenset[str] = frozenset(
    [
        "а",  # Cyrillic 'a'
        "е",  # Cyrillic 'e'
        "о",  # Cyrillic 'o'
        "р",  # Cyrillic 'p'
        "с",  # Cyrillic 'c'
        "у",  # Cyrillic 'y'
        "х",  # Cyrillic 'x'
        "і",  # Cyrillic 'i'
        "Α",  # Greek 'A'
        "Β",  # Greek 'B'
        "Ε",  # Greek 'E'
        "Η",  # Greek 'H'
        "Κ",  # Greek 'K'
        "Μ",  # Greek 'M'
        "Ν",  # Greek 'N'
        "Ο",  # Greek 'O'
        "Ρ",  # Greek 'P'
        "Τ",  # Greek 'T'
    ]
)
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

# Zero-width and bidi-control codepoints. The detector runs on the
# original text (pipeline step 4), before delimiter escaping inserts
# any ``U+200B``, so there is no self-trigger risk.
_INVISIBLE_UNICODE_CHARS: frozenset[str] = frozenset(
    [
        "​",  # zero-width space
        "‌",  # zero-width non-joiner
        "‍",  # zero-width joiner
        "⁠",  # word joiner
        "‎",  # LTR mark
        "‏",  # RTL mark
        "‪",  # LRE
        "‫",  # RLE
        "‬",  # PDF
        "‭",  # LRO
        "‮",  # RLO
    ]
)

_WHITESPACE_RUN_RE = re.compile(r"\s+")
_EXCESSIVE_WHITESPACE_RATIO = 0.3


def _detect_base64_blocks(text: str) -> bool:
    """True when ``text`` contains a base64-shaped run of >=100 chars."""
    return _BASE64_BLOCK_RE.search(text) is not None


def _detect_homoglyph_chars(text: str) -> bool:
    """True when ``text`` mixes a Cyrillic/Greek lookalike with an ASCII letter."""
    has_lookalike = any(ch in _HOMOGLYPH_CHARS for ch in text)
    if not has_lookalike:
        return False
    return _ASCII_LETTER_RE.search(text) is not None


def _detect_invisible_unicode(text: str) -> bool:
    """True when ``text`` contains any zero-width or bidi-control codepoint."""
    return any(ch in _INVISIBLE_UNICODE_CHARS for ch in text)


def _detect_excessive_whitespace(text: str) -> bool:
    """True when >30% of ``text`` length disappears after collapsing whitespace runs."""
    original_len = len(text)
    if original_len == 0:
        return False
    collapsed_len = len(_WHITESPACE_RUN_RE.sub(" ", text))
    return (original_len - collapsed_len) / original_len > _EXCESSIVE_WHITESPACE_RATIO


_DETECTORS: dict[str, Any] = {
    "base64_blocks": _detect_base64_blocks,
    "homoglyph_chars": _detect_homoglyph_chars,
    "invisible_unicode": _detect_invisible_unicode,
    "excessive_whitespace": _detect_excessive_whitespace,
}


# -- Config merge ------------------------------------------------------------


def _merge_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Merge user config over defaults. Return ``None`` on malformed input.

    The grader is permissive about extra keys (forward-compat) but strict
    about types: a ``risk_scoring`` section that is not a dict, or a
    ``weights``/``thresholds``/``structural_flags`` value of the wrong
    type, signals operator error -- log WARNING and skip grading.
    """
    if config is None:
        return {
            "enabled": True,
            "structural_flags": list(_ALL_STRUCTURAL_FLAGS),
            "weights": dict(_DEFAULT_WEIGHTS),
            "thresholds": dict(_DEFAULT_THRESHOLDS),
        }
    if not isinstance(config, dict):
        logger.warning(
            "risk_grader: config not a dict (got %s); skipping grade",
            type(config).__name__,
        )
        return None

    enabled = bool(config.get("enabled", True))

    raw_flags = config.get("structural_flags", None)
    if raw_flags is None:
        structural_flags = list(_ALL_STRUCTURAL_FLAGS)
    elif isinstance(raw_flags, list) and all(isinstance(f, str) for f in raw_flags):
        # Preserve the order in _ALL_STRUCTURAL_FLAGS so logs stay stable.
        allowed = set(raw_flags)
        structural_flags = [f for f in _ALL_STRUCTURAL_FLAGS if f in allowed]
    else:
        logger.warning("risk_grader: structural_flags must be a list of strings; skipping grade")
        return None

    weights = dict(_DEFAULT_WEIGHTS)
    user_weights = config.get("weights")
    if user_weights is not None:
        if not isinstance(user_weights, dict):
            logger.warning("risk_grader: weights must be a dict; skipping grade")
            return None
        for key, val in user_weights.items():
            if key in weights and isinstance(val, (int, float)):
                weights[key] = float(val)

    thresholds = dict(_DEFAULT_THRESHOLDS)
    user_thresholds = config.get("thresholds")
    if user_thresholds is not None:
        if not isinstance(user_thresholds, dict):
            logger.warning("risk_grader: thresholds must be a dict; skipping grade")
            return None
        for key, val in user_thresholds.items():
            if key in thresholds and isinstance(val, (int, float)):
                thresholds[key] = float(val)

    return {
        "enabled": enabled,
        "structural_flags": structural_flags,
        "weights": weights,
        "thresholds": thresholds,
    }


# -- Scoring helpers ---------------------------------------------------------


def _structural_score_and_flags(text: str, enabled: list[str]) -> tuple[float, list[str]]:
    """Run the enabled structural detectors and return ``(score, [flag_names])``.

    Each fired detector contributes 0.25 to the sub-score (capped at 1.0).
    Disabled detectors are silently skipped so config can narrow the set
    without affecting the others. The flag list order matches
    :data:`_ALL_STRUCTURAL_FLAGS` for stable log output.
    """
    fired: list[str] = []
    for name in enabled:
        detector = _DETECTORS.get(name)
        if detector is None:
            continue
        if detector(text):
            fired.append(name)
    return min(0.25 * len(fired), 1.0), fired


def _sender_modifier(tier: str) -> float:
    """Map a sender-trust tier string to its raw modifier value.

    Unknown tiers fall through to neutral (``0.0``) with a WARNING so an
    upstream rename does not silently re-weight the composite.
    """
    if tier in _SENDER_TIER_MODIFIERS:
        return _SENDER_TIER_MODIFIERS[tier]
    logger.warning("risk_grader: unknown sender_trust_tier=%r; treating as neutral", tier)
    return 0.0


def _level_for(composite: float, thresholds: dict[str, float]) -> str:
    """Map a composite score to ``LOW`` / ``MEDIUM`` / ``HIGH`` via two boundaries.

    Per TRR condition C2 the level mapping uses only the ``low`` and
    ``high`` thresholds (``composite < low`` -> LOW, ``composite < high``
    -> MEDIUM, otherwise HIGH). A ``medium`` key in user config has no
    effect by design.
    """
    if composite < thresholds["low"]:
        return "LOW"
    if composite < thresholds["high"]:
        return "MEDIUM"
    return "HIGH"


# -- Public entry point ------------------------------------------------------


def grade_risk(
    text: str,
    *,
    source: str = "",
    mode: str = "content",
    sanitizer_flagged: bool = False,
    sender_trust_tier: str = "",
    config: dict[str, Any] | None = None,
) -> RiskGrade | None:
    """Score injection risk of untrusted ``text`` via structural analysis.

    Phrase-level injection detection is the sanitizer's job
    (:mod:`xibi.security.sanitize`). This function scores structural
    anomalies, sender trust context, and whether the sanitizer flagged
    the text. The composite blends all three.

    Parameters
    ----------
    text:
        The **original** (pre-sanitization) text. Structural detectors
        run on this value so they see the attacker's payload, not the
        sanitizer's cleaned version. Callers should not pass an empty
        string -- the trust gate already short-circuits on empty input
        before invoking the grader, so an empty ``text`` here means
        there is nothing to grade and the function returns ``None`` as
        defensive coverage.
    source / mode:
        Stable labels for the structured log line. The grader does not
        change behavior based on either; both are passed through to the
        ``risk_grade`` log record.
    sanitizer_flagged:
        Did :func:`xibi.security.sanitize.sanitize_untrusted_text` alter
        ``text``? This is the strongest single signal and weights 0.5
        in the default composite.
    sender_trust_tier:
        One of ``"ESTABLISHED"``, ``"RECOGNIZED"``, ``"UNKNOWN"``,
        ``"NAME_MISMATCH"``, or ``""`` (no sender model -- MCP, subagent,
        calendar). Unknown values fall back to neutral with a WARNING.
    config:
        Per-call override of the ``risk_scoring`` config sub-section
        (``enabled``, ``structural_flags``, ``weights``, ``thresholds``).
        ``None`` means use hardcoded defaults so the grader works even
        before any config edit. A malformed config logs WARNING and
        returns ``None`` (grading skipped, pipeline continues).

    Returns
    -------
    :class:`RiskGrade` | ``None``
        ``None`` for empty input, when grading is disabled in config, or
        when an internal error occurs. Otherwise a populated grade with
        the composite, level, sub-scores, and structural-flag list.
    """
    try:
        if not text:
            return None

        cfg = _merge_config(config)
        if cfg is None or not cfg["enabled"]:
            return None

        sanitizer_score = 1.0 if sanitizer_flagged else 0.0
        structural_score, structural_flags = _structural_score_and_flags(text, cfg["structural_flags"])
        sender_mod = _sender_modifier(sender_trust_tier)

        weights = cfg["weights"]
        composite = (
            sanitizer_score * weights["sanitizer"]
            + structural_score * weights["structural"]
            + sender_mod * weights["sender"]
        )
        composite = max(0.0, min(1.0, composite))
        level = _level_for(composite, cfg["thresholds"])

        grade = RiskGrade(
            composite=composite,
            level=level,
            sanitizer_flagged=sanitizer_flagged,
            sanitizer_score=sanitizer_score,
            structural_score=structural_score,
            structural_flags=structural_flags,
            sender_modifier=sender_mod,
            sender_tier=sender_trust_tier,
        )

        logger.info(
            "risk_grade source=%s mode=%s composite=%.2f level=%s sanitizer_flagged=%s "
            "structural=%.2f(flags=%s) sender_mod=%+.2f(tier=%s)",
            source or "(unset)",
            mode,
            composite,
            level,
            "true" if sanitizer_flagged else "false",
            structural_score,
            ",".join(structural_flags),
            sender_mod,
            sender_trust_tier or "(unset)",
        )

        return grade
    except Exception as exc:  # noqa: BLE001 -- contract: never raise
        logger.warning("risk_grader: scoring failed (%s); skipping grade", exc)
        return None
