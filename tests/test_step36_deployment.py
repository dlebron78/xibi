import json
import os
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
    return home

def test_config_migrate_produces_valid_schema(fake_home):
    bregger_config = {
        "model": "llama3.2:latest",
        "llm": {"model": "llama3.2:latest"}
    }
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
    subprocess.run([
        "python3", str(script_py),
        "--input", str(config_path),
        "--output", str(output_config)
    ], check=True, env=env)

    assert output_config.exists()
    with open(output_config) as f:
        xibi_config = json.load(f)

    assert "models" in xibi_config
    assert "providers" in xibi_config
    assert "default" in xibi_config["models"]
    assert xibi_config["models"]["default"]["model"] == "llama3.2:latest"
    assert "_bregger_legacy" in xibi_config

def test_cutover_script_dry_run(fake_home):
    # Setup requirements for cutover script
    (fake_home / ".xibi" / "config.json").write_text("{}")
    (fake_home / ".xibi" / "secrets.env").write_text("")

    script_sh = Path("scripts/xibi_cutover.sh").absolute()

    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    # Need to make xibi importable
    subprocess.run(["pip", "install", "-e", "."], check=True)

    result = subprocess.run([
        "bash", str(script_sh), "--dry-run"
    ], check=True, capture_output=True, text=True, env=env)

    assert "DRY RUN" in result.stdout
    assert "Stopping Bregger services" in result.stdout
    assert "Installing Xibi systemd services" in result.stdout
    assert "Starting Xibi services" in result.stdout
    assert "[dry-run] systemctl --user stop bregger-telegram" in result.stdout
