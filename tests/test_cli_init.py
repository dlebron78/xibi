from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from xibi.cli.init import cmd_init


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    xibi_home = tmp_path / ".xibi"
    xibi_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import xibi.cli
    import xibi.cli.init
    import xibi.config
    import xibi.secrets.manager

    config_path = xibi_home / "config.yaml"
    monkeypatch.setattr(xibi.config, "CONFIG_PATH", config_path)
    # We no longer patch xibi.cli.CONFIG_PATH as it's not used there anymore

    monkeypatch.setattr(xibi.secrets.manager, "SECRETS_DIR", xibi_home / "secrets")
    monkeypatch.setattr(xibi.secrets.manager, "MASTER_KEY_FILE", xibi_home / "secrets" / ".master.key")
    monkeypatch.setattr(xibi.secrets.manager, "ENCRYPTED_SECRETS_FILE", xibi_home / "secrets" / "secrets.enc")

    return xibi_home


def test_init_interactive_wizard_creates_config(isolated_config):
    # Mock inputs: channel, telegram token, provider, model name, admin id
    inputs = iter(["telegram", "my-token", "ollama", "qwen3.5:9b", "12345"])

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with patch("builtins.input", lambda _: next(inputs)), patch("requests.get") as mock_get:
        # Mock Ollama validation response
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}

        cmd_init(args)

    config_file = isolated_config / "config.yaml"
    assert config_file.exists()

    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config["channel"] == "telegram"
    assert config["admin_user_id"] == 12345
    assert config["models"]["text"]["fast"]["model"] == "qwen3.5:9b"


def test_init_creates_database_schema(isolated_config):
    from xibi.db.migrations import SCHEMA_VERSION

    inputs = iter(["telegram", "", "openai", "gpt-4o", ""])

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with patch("builtins.input", lambda _: next(inputs)):
        cmd_init(args)

    db_path = isolated_config / "data" / "xibi.db"
    assert db_path.exists()

    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cursor.fetchone()
    assert row[0] == SCHEMA_VERSION
    conn.close()


def test_init_stores_telegram_credentials(isolated_config):
    from xibi.secrets import manager as secrets_manager

    inputs = iter(["telegram", "super-secret-token", "openai", "gpt-4o", ""])

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    # Mock keyring to use fallback to avoid issues in environment
    with patch("builtins.input", lambda _: next(inputs)), patch("xibi.secrets.manager.keyring", None):
        cmd_init(args)

    assert secrets_manager.load("telegram_token") == "super-secret-token"


def test_init_validates_model_exists_ollama(isolated_config, capsys):
    inputs = iter(["telegram", "", "ollama", "invalid-model", "n", "valid-model", ""])

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with patch("builtins.input", lambda prompt: next(inputs)), patch("requests.get") as mock_get:
        # Mock Ollama validation response: first call returns no models, second call returns valid-model
        mock_get.return_value.status_code = 200
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"models": []}),
            MagicMock(status_code=200, json=lambda: {"models": [{"name": "valid-model"}]}),
        ]

        cmd_init(args)

    captured = capsys.readouterr().out
    assert "Model invalid-model not found in Ollama" in captured
    assert "Model valid-model found" in captured


def test_init_ollama_endpoint_unreachable(isolated_config, capsys):
    inputs = iter(["telegram", "", "ollama", "qwen3.5:9b", ""])

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with (
        patch("builtins.input", lambda _: next(inputs)),
        patch("requests.get", side_effect=Exception("Connection refused")),
    ):
        cmd_init(args)

    captured = capsys.readouterr().out
    assert "Could not connect to Ollama" in captured
    assert "Configuration saved" in captured
