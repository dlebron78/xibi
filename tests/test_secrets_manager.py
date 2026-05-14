"""Dedicated tests for xibi.secrets.manager.

Exercises the store/load/delete API of the secrets manager directly:
- Roundtrip via the Fernet-encrypted file fallback when keyring is
  unavailable.
- Nonexistent-key lookup.
- Corrupted ``secrets.enc`` returns ``None`` (the manager's
  ``_load_encrypted_secrets`` catches decryption errors and returns an
  empty dict, so ``load()`` returns ``None`` rather than raising).
- ``_get_fallback_key`` idempotency — calling it twice does not regenerate
  the master key file.

Pre-existing coverage in test_cli_init.py only exercises the manager
indirectly through the init wizard.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    """Point manager file paths at ``tmp_path/.xibi/secrets``.

    Mirrors the isolation in ``test_cli_init.py`` but without invoking the
    CLI — tests here operate on ``xibi.secrets.manager`` directly.
    """
    xibi_home = tmp_path / ".xibi"
    secrets_dir = xibi_home / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    import xibi.secrets.manager as manager

    monkeypatch.setattr(manager, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(manager, "MASTER_KEY_FILE", secrets_dir / ".master.key")
    monkeypatch.setattr(manager, "ENCRYPTED_SECRETS_FILE", secrets_dir / "secrets.enc")

    return secrets_dir


def test_store_load_roundtrip(isolated_secrets):
    """Store a value via the Fernet fallback, then load it back unchanged."""
    from xibi.secrets import manager

    # Force the keyring path off so we exercise the file fallback in both
    # store() and load().
    with patch.object(manager, "keyring", None):
        manager.store("telegram_token", "abc-123-secret")
        assert manager.load("telegram_token") == "abc-123-secret"


def test_load_nonexistent_returns_none(isolated_secrets):
    """``load()`` returns ``None`` for a key that was never stored."""
    from xibi.secrets import manager

    with patch.object(manager, "keyring", None):
        assert manager.load("never_set") is None


def test_keyring_unavailable_uses_fernet_fallback(isolated_secrets):
    """With keyring forced unavailable, the Fernet file is created and used."""
    from xibi.secrets import manager

    with patch.object(manager, "keyring", None):
        manager.store("anthropic_api_key", "sk-ant-xyz")

    # The encrypted file must exist on disk after a fallback write.
    assert manager.ENCRYPTED_SECRETS_FILE.exists()

    with patch.object(manager, "keyring", None):
        assert manager.load("anthropic_api_key") == "sk-ant-xyz"


def test_corrupted_encrypted_file_returns_none(isolated_secrets):
    """Garbage in ``secrets.enc`` produces ``None``, not an exception.

    The manager's ``_load_encrypted_secrets`` catches all decryption
    errors and returns ``{}``, so ``load()`` cannot find the key and
    returns ``None``. We assert that contract here rather than asserting
    a raise — changing the manager to raise would be a behavior change
    outside this cleanup step's scope (TRR condition C1, step-130).
    """
    from xibi.secrets import manager

    # Make sure the master key file exists so the Fernet load path is the
    # one that fails on garbage ciphertext (not a missing-key path).
    manager._get_fallback_key()
    manager.ENCRYPTED_SECRETS_FILE.write_bytes(b"not-a-valid-fernet-token")

    with patch.object(manager, "keyring", None):
        assert manager.load("anything") is None


def test_master_key_generation_idempotent(isolated_secrets):
    """Calling ``_get_fallback_key`` twice yields the same key, file unchanged."""
    from xibi.secrets import manager

    first = manager._get_fallback_key()
    mtime_first = Path(manager.MASTER_KEY_FILE).stat().st_mtime_ns
    second = manager._get_fallback_key()
    mtime_second = Path(manager.MASTER_KEY_FILE).stat().st_mtime_ns

    assert first == second
    assert mtime_first == mtime_second
