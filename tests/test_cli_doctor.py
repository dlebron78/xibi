from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from xibi.cli import cmd_doctor
from xibi.db.migrations import SCHEMA_VERSION, SchemaManager


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    xibi_home = tmp_path / ".xibi"
    xibi_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import xibi.cli
    import xibi.config
    import xibi.secrets.manager

    config_path = xibi_home / "config.yaml"
    monkeypatch.setattr(xibi.config, "CONFIG_PATH", config_path)

    monkeypatch.setattr(xibi.secrets.manager, "SECRETS_DIR", xibi_home / "secrets")
    monkeypatch.setattr(xibi.secrets.manager, "MASTER_KEY_FILE", xibi_home / "secrets" / ".master.key")
    monkeypatch.setattr(xibi.secrets.manager, "ENCRYPTED_SECRETS_FILE", xibi_home / "secrets" / "secrets.enc")

    return xibi_home


def strip_ansi(text):
    import re

    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def test_doctor_all_checks_pass(isolated_config, capsys):
    config = {
        "channel": "telegram",
        "admin_user_id": 12345,
        "skill_dir": str(isolated_config / "skills"),
        "db_path": str(isolated_config / "data" / "xibi.db"),
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen3.5:9b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    config_file = isolated_config / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config, f)

    db_path = isolated_config / "data" / "xibi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sm = SchemaManager(db_path)
    sm.migrate()

    # Create a skill
    skill_dir = isolated_config / "skills" / "test_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "manifest.yaml").write_text("name: test_skill\ndescription: test\ntools: []")

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with patch("xibi.secrets.manager.load", return_value="my-token"), patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}

        cmd_doctor(args)

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✓] Config file" in captured
    assert f"[✓] Database at {db_path} (schema version {SCHEMA_VERSION})" in captured
    assert "[✓] Telegram token configured" in captured
    assert "[✓] Ollama endpoint responding (qwen3.5:9b available)" in captured
    assert "[✓] Skill manifest directory found (1 skills loaded)" in captured
    assert "[✓] Admin telegram user ID configured" in captured


def test_doctor_missing_config_reports_error(isolated_config, capsys):
    config_file = isolated_config / "config.yaml"
    if config_file.exists():
        config_file.unlink()

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with pytest.raises(SystemExit) as exc:
        cmd_doctor(args)
    assert exc.value.code == 1

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✗] Config file" in captured


def test_doctor_ollama_unreachable(isolated_config, capsys):
    config = {
        "channel": "telegram",
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen3.5:9b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:9999"}},
        "skill_dir": str(isolated_config / "skills"),
        "db_path": str(isolated_config / "data" / "xibi.db"),
    }
    with open(isolated_config / "config.yaml", "w") as f:
        yaml.dump(config, f)

    db_path = isolated_config / "data" / "xibi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sm = SchemaManager(db_path)
    sm.migrate()

    (isolated_config / "skills").mkdir(parents=True, exist_ok=True)

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with (
        patch("xibi.secrets.manager.load", return_value="my-token"),
        patch("requests.get", side_effect=Exception("Connection refused")),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_doctor(args)
        assert exc.value.code == 1

    captured = strip_ansi(capsys.readouterr().out)
    assert "[✗] Ollama endpoint unreachable" in captured


def test_doctor_db_schema_version_mismatch(isolated_config, capsys):
    db_path = isolated_config / "data" / "xibi.db"
    config = {
        "channel": "telegram",
        "db_path": str(db_path),
        "skill_dir": str(isolated_config / "skills"),
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen3.5:9b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    with open(isolated_config / "config.yaml", "w") as f:
        yaml.dump(config, f)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP, description TEXT)"
    )
    conn.execute("INSERT INTO schema_version (version, description) VALUES (0, 'Legacy')")
    conn.commit()
    conn.close()

    (isolated_config / "skills").mkdir(parents=True, exist_ok=True)

    args = MagicMock()
    args.workdir = str(isolated_config)
    args.config = None

    with patch("xibi.secrets.manager.load", return_value="my-token"), patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}

        with pytest.raises(SystemExit) as exc:
            cmd_doctor(args)
        assert exc.value.code == 1

    captured = strip_ansi(capsys.readouterr().out)
    assert f"[✗] Database at {db_path} (schema version mismatch: got 0, expected {SCHEMA_VERSION})" in captured
