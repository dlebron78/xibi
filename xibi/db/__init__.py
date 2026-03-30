from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from xibi.db.migrations import SchemaManager, migrate


@contextmanager
def open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connections with WAL mode for crash resilience."""
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


__all__ = ["SchemaManager", "migrate", "init_workdir", "open_db"]
