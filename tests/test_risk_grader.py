"""Unit tests for the shadow risk grader (step-131)."""

from __future__ import annotations

import logging
import math

import pytest

from xibi.security.risk_grader import RiskGrade, grade_risk


# -- Empty / contract corners ------------------------------------------------


def test_empty_text_returns_none():
    """Per TRR condition C3: empty input has nothing to grade."""
    assert grade_risk("") is None
    # Defensive coverage for None too -- the trust gate already short-circuits.
    assert grade_risk(None) is None  # type: ignore[arg-type]


def test_clean_text_scores_low():
    grade = grade_risk("Hello, this is a normal English paragraph.")
    assert isinstance(grade, RiskGrade)
    assert grade.level == "LOW"
    assert grade.structural_flags == []
    assert grade.sanitizer_score == 0.0
    assert grade.composite < 0.2


# -- Sanitizer sub-score -----------------------------------------------------


def test_sanitizer_flagged_raises_score():
    grade = grade_risk("payload", sanitizer_flagged=True)
    assert grade is not None
    # Sanitizer weight is 0.5, so a flagged-only signal lands at exactly 0.50.
    assert grade.sanitizer_score == 1.0
    assert grade.composite >= 0.5
    assert grade.level == "MEDIUM"


def test_sanitizer_not_flagged_zero():
    grade = grade_risk("payload", sanitizer_flagged=False)
    assert grade is not None
    assert grade.sanitizer_score == 0.0


# -- Structural flag detection ----------------------------------------------


def test_base64_structural_flag():
    payload = "intro " + ("A" * 120) + " outro"
    grade = grade_risk(payload)
    assert grade is not None
    assert "base64_blocks" in grade.structural_flags


def test_short_base64_not_flagged():
    # 80 chars of base64-shaped text should NOT trip the >=100 detector.
    payload = "snippet: " + ("A" * 80)
    grade = grade_risk(payload)
    assert grade is not None
    assert "base64_blocks" not in grade.structural_flags


def test_homoglyph_detection():
    # Cyrillic 'а' (U+0430) intermixed with Latin letters.
    payload = "Dаniel, please read this"
    grade = grade_risk(payload)
    assert grade is not None
    assert "homoglyph_chars" in grade.structural_flags


def test_pure_cyrillic_not_flagged():
    # All-Cyrillic Russian text -- no ASCII letters present, so the
    # mixed-script heuristic does not fire.
    payload = "Привет мир"  # "Privet mir"
    grade = grade_risk(payload)
    assert grade is not None
    assert "homoglyph_chars" not in grade.structural_flags


def test_invisible_unicode_detection():
    payload = "click here​ for more info"  # zero-width space
    grade = grade_risk(payload)
    assert grade is not None
    assert "invisible_unicode" in grade.structural_flags


def test_excessive_whitespace_detection():
    # >30% of the text is whitespace once runs are counted: pad with tabs.
    payload = "hi" + ("\t" * 50) + "there"
    grade = grade_risk(payload)
    assert grade is not None
    assert "excessive_whitespace" in grade.structural_flags


def test_normal_whitespace_not_flagged():
    payload = "this is a perfectly ordinary sentence with normal spaces."
    grade = grade_risk(payload)
    assert grade is not None
    assert "excessive_whitespace" not in grade.structural_flags


def test_multiple_structural_flags_compound():
    payload = "Dаniel​ says: " + ("A" * 120)
    grade = grade_risk(payload)
    assert grade is not None
    # Homoglyph + invisible_unicode + base64 = 3 flags -> structural_score 0.75.
    assert {"homoglyph_chars", "invisible_unicode", "base64_blocks"}.issubset(set(grade.structural_flags))
    assert math.isclose(grade.structural_score, 0.75, abs_tol=1e-9)


def test_all_four_flags():
    payload = (
        "Dаniel​"
        + ("\t" * 200)
        + "letters"
        + ("A" * 120)
    )
    grade = grade_risk(payload)
    assert grade is not None
    assert set(grade.structural_flags) == {
        "base64_blocks",
        "homoglyph_chars",
        "invisible_unicode",
        "excessive_whitespace",
    }
    assert math.isclose(grade.structural_score, 1.0, abs_tol=1e-9)


# -- Sender modifier --------------------------------------------------------


def test_sender_established_lowers_score():
    grade = grade_risk("payload", sender_trust_tier="ESTABLISHED")
    assert grade is not None
    assert math.isclose(grade.sender_modifier, -0.1, abs_tol=1e-9)


def test_sender_name_mismatch_raises_score():
    grade = grade_risk("payload", sender_trust_tier="NAME_MISMATCH")
    assert grade is not None
    assert math.isclose(grade.sender_modifier, 0.2, abs_tol=1e-9)


def test_sender_unknown_raises_score():
    grade = grade_risk("payload", sender_trust_tier="UNKNOWN")
    assert grade is not None
    assert math.isclose(grade.sender_modifier, 0.1, abs_tol=1e-9)


def test_no_sender_tier_neutral():
    grade = grade_risk("payload", sender_trust_tier="")
    assert grade is not None
    assert grade.sender_modifier == 0.0


def test_unknown_sender_tier_warns_and_neutral(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.security.risk_grader")
    grade = grade_risk("payload", sender_trust_tier="WEIRDTIER")
    assert grade is not None
    assert grade.sender_modifier == 0.0
    assert any("unknown sender_trust_tier" in r.getMessage() for r in caplog.records)


# -- Composite math ---------------------------------------------------------


def test_composite_clamped_to_unit():
    # Synthesize extreme inputs via custom weights to push composite past 1.0.
    cfg = {"weights": {"sanitizer": 1.0, "structural": 1.0, "sender": 1.0}}
    grade = grade_risk(
        "Dаniel​" + ("\t" * 50) + "letters" + ("A" * 120),
        sanitizer_flagged=True,
        sender_trust_tier="NAME_MISMATCH",
        config=cfg,
    )
    assert grade is not None
    assert 0.0 <= grade.composite <= 1.0

    # And the negative side: established sender, custom weights, but no flags.
    cfg2 = {"weights": {"sanitizer": 1.0, "structural": 1.0, "sender": 10.0}}
    g2 = grade_risk("clean", sender_trust_tier="ESTABLISHED", config=cfg2)
    assert g2 is not None
    assert 0.0 <= g2.composite <= 1.0


def test_composite_matches_table():
    """Verify each row of the example composite table from the spec."""
    # (sanitizer_flagged, structural-flag-count synthesizer, sender_tier,
    #  expected_composite, expected_level)
    # Build payloads that produce specific structural-flag counts.
    base = "this is a normal sentence."
    one_flag = "click​ here"  # invisible_unicode
    two_flags = "Dаniel​ says hi"  # homoglyph + invisible
    three_flags = "Dаniel​ says " + ("A" * 120)  # homoglyph + invisible + base64
    four_flags = (
        "Dаniel​" + ("\t" * 200) + "letters" + ("A" * 120)
    )

    rows = [
        # Row 1: clean + ESTABLISHED -> 0.0 (clamped from -0.015), LOW
        (base, False, "ESTABLISHED", 0.0, "LOW"),
        # Row 2: clean + UNKNOWN -> 0.015, LOW
        (base, False, "UNKNOWN", 0.015, "LOW"),
        # Row 3: sanitizer + UNKNOWN -> 0.515, MEDIUM
        (base, True, "UNKNOWN", 0.515, "MEDIUM"),
        # Row 4: sanitizer + invisible_unicode + UNKNOWN -> 0.6025, MEDIUM
        (one_flag, True, "UNKNOWN", 0.6025, "MEDIUM"),
        # Row 5: sanitizer + 2 flags + NAME_MISMATCH -> 0.705, MEDIUM
        (two_flags, True, "NAME_MISMATCH", 0.705, "MEDIUM"),
        # Row 6: sanitizer + 3 flags + NAME_MISMATCH -> 0.7925, MEDIUM
        (three_flags, True, "NAME_MISMATCH", 0.7925, "MEDIUM"),
        # Row 7: sanitizer + 4 flags + NAME_MISMATCH -> 0.880, HIGH
        (four_flags, True, "NAME_MISMATCH", 0.880, "HIGH"),
        # Row 8: no sanitizer + 4 flags + NAME_MISMATCH -> 0.380, MEDIUM
        (four_flags, False, "NAME_MISMATCH", 0.380, "MEDIUM"),
    ]
    for text, flagged, tier, expected_composite, expected_level in rows:
        grade = grade_risk(text, sanitizer_flagged=flagged, sender_trust_tier=tier)
        assert grade is not None
        assert math.isclose(
            grade.composite, expected_composite, abs_tol=1e-6
        ), f"row text={text!r} flagged={flagged} tier={tier}: composite {grade.composite} != {expected_composite}"
        assert grade.level == expected_level


# -- Config overrides -------------------------------------------------------


def test_config_override_structural_flags():
    """A subset config disables specific detectors without code changes."""
    cfg = {"structural_flags": ["invisible_unicode"]}
    # Payload trips base64 + invisible_unicode; with only invisible enabled,
    # base64 must not appear in the flag list.
    payload = "click​ here " + ("A" * 120)
    grade = grade_risk(payload, config=cfg)
    assert grade is not None
    assert grade.structural_flags == ["invisible_unicode"]
    assert math.isclose(grade.structural_score, 0.25, abs_tol=1e-9)


def test_config_override_weights():
    """Custom weights change how sub-scores combine in the composite."""
    cfg = {"weights": {"sanitizer": 0.9, "structural": 0.05, "sender": 0.05}}
    grade = grade_risk("payload", sanitizer_flagged=True, config=cfg)
    assert grade is not None
    # Composite = 1.0 * 0.9 = 0.9
    assert math.isclose(grade.composite, 0.9, abs_tol=1e-6)
    assert grade.level == "HIGH"


def test_config_override_thresholds():
    """Custom thresholds change the LOW/MEDIUM/HIGH boundaries."""
    cfg = {"thresholds": {"low": 0.05, "high": 0.3}}
    # sanitizer_flagged + UNKNOWN composite = 0.515 -> HIGH under cfg.
    grade = grade_risk("payload", sanitizer_flagged=True, sender_trust_tier="UNKNOWN", config=cfg)
    assert grade is not None
    assert grade.level == "HIGH"


def test_disabled_returns_none():
    """``risk_scoring.enabled: false`` skips the grader entirely."""
    cfg = {"enabled": False}
    assert grade_risk("anything", sanitizer_flagged=True, config=cfg) is None


# -- Failure modes ----------------------------------------------------------


def test_bad_config_returns_none_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.security.risk_grader")
    # ``risk_scoring`` is a string -- exactly the failure-path exercise from
    # the spec's Post-Deploy Verification section.
    grade = grade_risk("payload", config="not_a_dict")  # type: ignore[arg-type]
    assert grade is None
    assert any("config not a dict" in r.getMessage() for r in caplog.records)


def test_bad_weights_dict_type_returns_none(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.security.risk_grader")
    grade = grade_risk("payload", config={"weights": "not_a_dict"})
    assert grade is None
    assert any("weights must be a dict" in r.getMessage() for r in caplog.records)


def test_never_raises():
    """Per contract: any internal error is caught; the function returns None.

    Feed garbage types -- the function returns ``None`` rather than raising.
    """
    # Garbage config that survives the dict check but explodes elsewhere.
    grade = grade_risk("payload", config={"weights": {"sanitizer": "not_a_number"}})
    # The non-numeric weight is ignored (only int/float survive merge), so
    # this still produces a valid grade.
    assert grade is not None

    # Bytes for text is the wrong type; should not raise.
    bytes_grade = grade_risk(b"bytes not str")  # type: ignore[arg-type]
    # truthy bytes object short-circuits the empty check; some detectors
    # may raise on it -- the function must still return either a grade
    # or None, never propagate.
    assert bytes_grade is None or isinstance(bytes_grade, RiskGrade)


# -- Log emission -----------------------------------------------------------


def test_log_format_includes_all_fields(caplog):
    caplog.set_level(logging.INFO, logger="xibi.security.risk_grader")
    grade_risk(
        "click​ here",
        source="email_body",
        mode="content",
        sanitizer_flagged=True,
        sender_trust_tier="UNKNOWN",
    )
    rec = next(r for r in caplog.records if "risk_grade" in r.getMessage())
    msg = rec.getMessage()
    assert "source=email_body" in msg
    assert "mode=content" in msg
    assert "composite=" in msg
    assert "level=" in msg
    assert "sanitizer_flagged=true" in msg
    assert "structural=" in msg
    assert "sender_mod=" in msg
    assert "tier=UNKNOWN" in msg


def test_log_sanitizer_flagged_lowercase(caplog):
    """Spec-format: ``sanitizer_flagged=true`` / ``false`` (lowercase)."""
    caplog.set_level(logging.INFO, logger="xibi.security.risk_grader")
    grade_risk("text", sanitizer_flagged=False)
    rec = next(r for r in caplog.records if "risk_grade" in r.getMessage())
    assert "sanitizer_flagged=false" in rec.getMessage()
