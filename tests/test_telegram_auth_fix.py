"""Tests for the TelegramAdapter._is_authorized fix (step-122).

Pre-step-122, ``_is_authorized`` re-read ``XIBI_TELEGRAM_ALLOWED_CHAT_IDS``
on every call, ignoring ``self.allowed_chats`` set by the constructor.
That made tests and runtime overrides invisible to the auth check, and
diverged from the two other check sites (~lines 967, ~1092) that
already used ``self.allowed_chats``.

The fix consolidates on the constructor as the single source of truth.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import xibi.channels.telegram as telegram_mod
from xibi.channels.telegram import TelegramAdapter


def _stub_adapter(allowed_chats):
    """Build a stand-in object with the attributes ``_is_authorized`` reads.

    The real ``TelegramAdapter.__init__`` requires a Config, SkillRegistry,
    a valid bot token, and a writable DB path -- none of which the auth
    check itself depends on. ``_is_authorized`` only consults
    ``self.allowed_chats``, so we bind it to a SimpleNamespace and call
    the unbound method to exercise the post-fix behavior in isolation.
    """
    return SimpleNamespace(allowed_chats=allowed_chats)


def test_is_authorized_uses_instance_var_not_env(monkeypatch):
    """``_is_authorized`` honors ``self.allowed_chats``, ignoring the env var.

    Stub seeded with ``["12345"]``; env unset. If the method re-read
    the env var (the pre-fix behavior) it would treat the allowlist as
    empty and reject. With the fix it consults ``self.allowed_chats``
    and accepts.
    """
    monkeypatch.delenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    stub = _stub_adapter(["12345"])
    assert TelegramAdapter._is_authorized(stub, "12345") is True
    assert TelegramAdapter._is_authorized(stub, "99999") is False


def test_is_authorized_returns_false_when_allowlist_empty(monkeypatch):
    """Empty ``self.allowed_chats`` denies access (fail-closed)."""
    monkeypatch.delenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    stub = _stub_adapter([])
    assert TelegramAdapter._is_authorized(stub, "12345") is False


def test_is_authorized_ignores_late_env_override(monkeypatch):
    """Setting the env var post-construction does not extend the allowlist.

    Confirms the constructor really is the single source of truth: a
    chat id present in the env var but not in ``self.allowed_chats``
    must still be rejected.
    """
    stub = _stub_adapter(["12345"])
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "12345,67890")
    assert TelegramAdapter._is_authorized(stub, "67890") is False
    assert TelegramAdapter._is_authorized(stub, "12345") is True


def test_is_authorized_consistent_with_other_check_sites(tmp_path):
    """All three check sites consult ``self.allowed_chats`` (no env re-read).

    Lines ~269 (``_is_authorized``), ~967, and ~1092 are the three
    places the adapter decides whether to act on a chat. Pre-fix, only
    269 re-read the env var. Post-fix, the source code for all three
    references ``self.allowed_chats`` and none re-reads
    ``XIBI_TELEGRAM_ALLOWED_CHAT_IDS`` outside the constructor.
    """
    src = inspect.getsource(telegram_mod)
    # Count actual reads of the env var, not docstring/comment mentions.
    # The constructor is the only place allowed to read it.
    env_reads = src.count('os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS"') + src.count(
        'os.getenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS"'
    )
    assert env_reads == 1, (
        f"expected exactly one env read (constructor); found {env_reads}"
    )
    # All check sites use self.allowed_chats; the method body is
    # short enough to assert via inspect. The docstring may mention
    # the env var by name (explaining why the method *doesn't* read
    # it), so we check for actual reads, not bare string mentions.
    method_src = inspect.getsource(TelegramAdapter._is_authorized)
    assert "self.allowed_chats" in method_src
    assert 'os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS"' not in method_src
    assert 'os.getenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS"' not in method_src
