from __future__ import annotations

import os
from pathlib import Path
import yaml
import pytest
from unittest.mock import patch, MagicMock
from xibi.cli.init import cmd_init
from xibi.config import CONFIG_PATH
from xibi.db.migrations import SCHEMA_VERSION

@pytest.fixture
def clean_xibi_home(tmp_path, monkeypatch):
    xibi_home = tmp_path / ".xibi"
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("xibi.config.CONFIG_PATH", xibi_home / "config.yaml")
    monkeypatch.setattr("xibi.secrets.manager.SECRETS_DIR", xibi_home / "secrets")
    monkeypatch.setattr("xibi.secrets.manager.MASTER_KEY_FILE", xibi_home / "secrets" / ".master.key")
    monkeypatch.setattr("xibi.secrets.manager.ENCRYPTED_SECRETS_FILE", xibi_home / "secrets" / "secrets.enc")
    return xibi_home

def test_init_interactive_wizard_creates_config(clean_xibi_home):
    # Mock inputs: channel, telegram token, provider, model name, admin id
    inputs = iter(["telegram", "my-token", "ollama", "qwen3.5:9b", "12345"])

    with patch("builtins.input", lambda _: next(inputs)), \
         patch("requests.get") as mock_get:

        # Mock Ollama validation response
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}

        cmd_init(MagicMock())

    config_file = clean_xibi_home / "config.yaml"
    assert config_file.exists()

    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config["channel"] == "telegram"
    assert config["admin_user_id"] == 12345
    assert config["models"]["text"]["fast"]["model"] == "qwen3.5:9b"

def test_init_creates_database_schema(clean_xibi_home):
    inputs = iter(["telegram", "", "openai", "gpt-4o", ""])

    with patch("builtins.input", lambda _: next(inputs)):
        cmd_init(MagicMock())

    db_path = clean_xibi_home / "data" / "xibi.db"
    assert db_path.exists()

    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cursor.fetchone()
    assert row[0] == SCHEMA_VERSION
    conn.close()

def test_init_stores_telegram_credentials(clean_xibi_home):
    from xibi.secrets import manager as secrets_manager
    inputs = iter(["telegram", "super-secret-token", "openai", "gpt-4o", ""])

    # Mock keyring to use fallback to avoid issues in environment
    with patch("builtins.input", lambda _: next(inputs)), \
         patch("xibi.secrets.manager.keyring", None):
        cmd_init(MagicMock())

    assert secrets_manager.load("telegram_token") == "super-secret-token"

def test_init_validates_model_exists_ollama(clean_xibi_home, capsys):
    # 1. Invalid model, then "n" to re-prompt (but our loop is simplified, let's test it breaks on error or succeeds)
    # Actually my implementation breaks the loop on Ollama error to be user friendly if Ollama is not running.
    # Let's test the validation logic specifically.

    inputs = iter(["telegram", "", "ollama", "invalid-model", "n", "valid-model", ""])

    with patch("builtins.input", lambda prompt: next(inputs)), \
         patch("requests.get") as mock_get:

        # Mock Ollama validation response: first call returns no models, second call returns valid-model
        mock_get.return_value.status_code = 200
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"models": []}),
            MagicMock(status_code=200, json=lambda: {"models": [{"name": "valid-model"}]})
        ]

        cmd_init(MagicMock())

    captured = capsys.readouterr().out
    assert "Model invalid-model not found in Ollama" in captured
    assert "Model valid-model found" in captured

def test_init_ollama_endpoint_unreachable(clean_xibi_home, capsys):
    inputs = iter(["telegram", "", "ollama", "qwen3.5:9b", ""])

    with patch("builtins.input", lambda _: next(inputs)), \
         patch("requests.get", side_effect=Exception("Connection refused")):

        cmd_init(MagicMock())

    captured = capsys.readouterr().out
    assert "Could not connect to Ollama" in captured
    assert "Configuration saved" in captured # It should still save config even if validation fails
