from __future__ import annotations

import json
from pathlib import Path

from xibi.db.migrations import SchemaManager, migrate


def init_workdir(workdir: Path) -> None:
    """Bootstrap a new Xibi workdir with directory structure, config, and database."""
    # 1. Create directory structure
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "skills").mkdir(exist_ok=True)
    (workdir / "data").mkdir(exist_ok=True)

    # 2. Create config.json if it doesn't exist
    config_path = workdir / "config.json"
    if not config_path.exists():
        example_config = Path("config.example.json")
        if example_config.exists():
            config_path.write_text(example_config.read_text())
        else:
            default_config: dict[str, dict] = {
                "models": {},
                "providers": {},
            }
            config_path.write_text(json.dumps(default_config, indent=2))

    # 3. Run migrations
    db_path = workdir / "data" / "xibi.db"
    migrate(db_path)


__all__ = ["SchemaManager", "migrate", "init_workdir"]
