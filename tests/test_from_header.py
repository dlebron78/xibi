"""Tests for xibi.email.from_header.build_from_header (step-110)."""

from __future__ import annotations

from xibi.email.from_header import build_from_header


ADDR = "hi.its.roberto@gmail.com"


def test_per_account_override(monkeypatch):
    monkeypatch.setenv("XIBI_OUTBOUND_FROM_NAME", "Daniel via Roberto")
    monkeypatch.setenv("XIBI_OUTBOUND_FROM_NAME_afya", "Daniel @ Afya via Roberto")
    assert build_from_header("afya", ADDR) == '"Daniel @ Afya via Roberto" <hi.its.roberto@gmail.com>'


def test_global_default(monkeypatch):
    monkeypatch.setenv("XIBI_OUTBOUND_FROM_NAME", "Custom Daniel")
    monkeypatch.delenv("XIBI_OUTBOUND_FROM_NAME_afya", raising=False)
    assert build_from_header("afya", ADDR) == '"Custom Daniel" <hi.its.roberto@gmail.com>'


def test_hardcoded_fallback(monkeypatch):
    monkeypatch.delenv("XIBI_OUTBOUND_FROM_NAME", raising=False)
    monkeypatch.delenv("XIBI_OUTBOUND_FROM_NAME_afya", raising=False)
    assert build_from_header(None, ADDR) == '"Daniel via Roberto" <hi.its.roberto@gmail.com>'


def test_missing_addr_returns_name_only(monkeypatch):
    monkeypatch.setenv("XIBI_OUTBOUND_FROM_NAME", "Custom")
    assert build_from_header(None, "") == "Custom"
