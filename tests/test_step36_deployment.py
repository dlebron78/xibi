import json
import os
import re
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "xibi").mkdir()
    (home / ".xibi").mkdir()
    (home / "bregger_remote").mkdir()
    (home / "bregger_deployment").mkdir()
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    return home


def test_config_migrate_produces_valid_schema(fake_home):
    bregger_config = {"model": "llama3.2:latest", "llm": {"model": "llama3.2:latest"}}
    config_path = fake_home / "bregger_remote" / "config.json"
    with open(config_path, "w") as f:
        json.dump(bregger_config, f)

    # Secrets
    secrets_path = fake_home / "bregger_deployment" / "secrets.env"
    secrets_path.write_text("export BREGGER_TELEGRAM_TOKEN=123")

    script_py = Path("scripts/xibi_config_migrate.py").absolute()
    output_config = fake_home / ".xibi" / "config.json"

    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    # Run python script
    subprocess.run(
        ["python3", str(script_py), "--input", str(config_path), "--output", str(output_config)], check=True, env=env
    )

    assert output_config.exists()
    with open(output_config) as f:
        xibi_config = json.load(f)

    assert "models" in xibi_config
    assert "providers" in xibi_config
    assert "default" in xibi_config["models"]
    assert xibi_config["models"]["default"]["model"] == "llama3.2:latest"
    assert "_bregger_legacy" in xibi_config


def test_cutover_script_dry_asyncio.run(run(fake_home)):
    # Setup requirements for cutover script
    (fake_home / ".xibi" / "config.json").write_text("{}")
    (fake_home / ".xibi" / "secrets.env").write_text("")

    script_sh = Path("scripts/xibi_cutover.sh").absolute()

    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    # Run first time
    result1 = subprocess.asyncio.run(run(["bash", str(script_sh)), "--dry-run"], check=True, capture_output=True, text=True, env=env)

    assert "DRY RUN" in result1.stdout
    assert "Stopping Bregger services" in result1.stdout
    assert "[dry-run] systemctl --user stop bregger-telegram" in result1.stdout
    assert result1.stderr == ""

    # Run second time (idempotency check)
    result2 = subprocess.asyncio.run(run(["bash", str(script_sh)), "--dry-run"], check=True, capture_output=True, text=True, env=env)

    # Strip timestamps for comparison
    def strip_ts(text):
        return re.sub(r"\[\d{2}:\d{2}:\d{2}\]", "[XX:XX:XX]", text)

    assert strip_ts(result2.stdout) == strip_ts(result1.stdout)
    assert result2.stderr == ""


def test_service_files_use_specifiers():
    for svc in ["systemd/xibi-telegram.service", "systemd/xibi-heartbeat.service"]:
        content = Path(svc).read_text()
        assert "%h" in content
        # %U / User= is intentionally absent: user-mode systemd units must not set User=
        # (invalid in --user context, causes 216/GROUP crash)
        assert "User=%U" not in content
        assert "/home/dlebron" not in content
        assert "User=dlebron" not in content
