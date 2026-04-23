from pathlib import Path


def _migrated_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite DB at tmp_path/'test.db' with all migrations
    applied. Return the db_path for use by test fixtures that need the
    full production schema (e.g. signals 29-column shape).
    """
    from xibi.db import migrate
    db_path = tmp_path / "test.db"
    migrate(db_path)
    return db_path
