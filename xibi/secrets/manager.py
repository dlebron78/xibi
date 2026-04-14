from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import cast

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

SECRETS_DIR = Path.home() / ".xibi" / "secrets"
MASTER_KEY_FILE = SECRETS_DIR / ".master.key"
ENCRYPTED_SECRETS_FILE = SECRETS_DIR / "secrets.enc"


def _get_fallback_key() -> bytes:
    """Derive a master key from the user's home directory hash if not already present."""
    if MASTER_KEY_FILE.exists():
        return MASTER_KEY_FILE.read_bytes()

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    # Derive a key from home directory path
    salt = hashlib.sha256(str(Path.home()).encode()).digest()
    key = base64.urlsafe_b64encode(salt)
    MASTER_KEY_FILE.write_bytes(key)
    return key


def _load_encrypted_secrets() -> dict[str, str]:
    if not ENCRYPTED_SECRETS_FILE.exists():
        return {}

    key = _get_fallback_key()
    f = Fernet(key)
    try:
        encrypted_data = ENCRYPTED_SECRETS_FILE.read_bytes()
        decrypted_data = f.decrypt(encrypted_data)
        return cast(dict[str, str], json.loads(decrypted_data.decode()))
    except Exception as e:
        logger.error(f"Failed to decrypt secrets: {e}")
        return {}


def _save_encrypted_secrets(secrets: dict[str, str]) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    key = _get_fallback_key()
    f = Fernet(key)
    encrypted_data = f.encrypt(json.dumps(secrets).encode())
    ENCRYPTED_SECRETS_FILE.write_bytes(encrypted_data)


def store(key: str, value: str) -> None:
    """Store a credential securely."""
    stored_in_keyring = False
    if keyring:
        try:
            keyring.set_password("xibi", key, value)
            stored_in_keyring = True
        except Exception as e:
            logger.warning(f"Keyring storage failed, falling back to encrypted file: {e}")

    if not stored_in_keyring:
        secrets = _load_encrypted_secrets()
        secrets[key] = value
        _save_encrypted_secrets(secrets)


def load(key: str) -> str | None:
    """Retrieve a stored credential."""
    if keyring:
        try:
            value = keyring.get_password("xibi", key)
            if value is not None:
                return cast(str, value)
        except Exception as e:
            logger.warning(f"Keyring retrieval failed: {e}")

    secrets = _load_encrypted_secrets()
    return secrets.get(key)


def delete(key: str) -> None:
    """Remove a credential."""
    if keyring:
        try:
            keyring.delete_password("xibi", key)
        except (keyring.errors.PasswordDeleteError, Exception) as e:
            if not isinstance(e, keyring.errors.PasswordDeleteError):
                logger.warning(f"Keyring deletion failed: {e}")

    # Always attempt to delete from encrypted file too
    secrets = _load_encrypted_secrets()
    if key in secrets:
        del secrets[key]
        _save_encrypted_secrets(secrets)
