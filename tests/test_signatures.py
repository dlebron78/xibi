"""Tests for xibi.email.signatures (step-110, includes C7 normalization)."""

from __future__ import annotations

from xibi.email.signatures import apply_signature, resolve_signature, should_append_signature


def test_per_account_signature(monkeypatch):
    monkeypatch.setenv("XIBI_SIGNATURE", "Best,\nDaniel")
    monkeypatch.setenv("XIBI_SIGNATURE_afya", "Best,\nDaniel Lebron\nChief of Staff @ Afya")
    sig = resolve_signature("afya")
    assert "Chief of Staff" in sig
    assert sig.startswith("Best,")


def test_global_default_signature(monkeypatch):
    monkeypatch.setenv("XIBI_SIGNATURE", "Best,\nDaniel")
    monkeypatch.delenv("XIBI_SIGNATURE_afya", raising=False)
    assert resolve_signature("afya") == "Best,\nDaniel"


def test_no_signature(monkeypatch):
    monkeypatch.delenv("XIBI_SIGNATURE", raising=False)
    monkeypatch.delenv("XIBI_SIGNATURE_afya", raising=False)
    assert resolve_signature("afya") == ""


def test_dedup_when_already_present():
    body = "thanks, will review by Friday\n\nBest,\nDaniel"
    sig = "Best,\nDaniel"
    assert apply_signature(body, sig) == body
    assert should_append_signature(body, sig) is False


def test_blank_line_separator_added():
    body = "thanks, will review"
    sig = "Best,\nDaniel"
    out = apply_signature(body, sig)
    assert out == "thanks, will review\n\nBest,\nDaniel"


def test_literal_backslash_n_in_env_normalizes(monkeypatch):
    """Env values like 'Best,\\nDaniel' (literal escape) become real newlines."""
    monkeypatch.setenv("XIBI_SIGNATURE_afya", "Best,\\nDaniel Lebron\\nAfya")
    sig = resolve_signature("afya")
    assert "\\n" not in sig
    assert sig == "Best,\nDaniel Lebron\nAfya"
    body = "ok, thanks"
    out = apply_signature(body, sig)
    assert out.endswith("Best,\nDaniel Lebron\nAfya")
    assert "\\n" not in out


def test_apply_signature_empty_body():
    sig = "Best,\nDaniel"
    assert apply_signature("", sig) == "\n\nBest,\nDaniel"


def test_apply_signature_empty_signature():
    assert apply_signature("body", "") == "body"
