"""One-shot migration: legacy GOOGLE_CALENDAR_* env vars → DB-backed account.

Idempotent — safe to run on every deploy. If the (default-owner,
google_calendar, default) row already exists AND the secret is already
stored, the migration logs `legacy_calendar_creds_migrated_skipped` and
exits 0. If only one of the two halves is present, the missing half is
filled in and a new `legacy_calendar_creds_migrated` line is logged.

Intended call sites:
  - service-init hook (xibi-telegram.service ExecStartPre, etc.)
  - manual one-shot: ``python3 scripts/migrate_calendar_envvars.py``
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Allow running from the repo root without setting PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xibi.db import migrate as db_migrate  # noqa: E402
from xibi.oauth.store import OAuthStore  # noqa: E402
from xibi.secrets import manager as secrets_manager  # noqa: E402

logger = logging.getLogger("migrate_calendar_envvars")

USER_ID = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
PROVIDER = "google_calendar"
NICKNAME = "default"


def _db_path() -> Path:
    return Path(os.environ.get("XIBI_DB_PATH", str(Path.home() / ".xibi" / "data" / "xibi.db")))


def _secret_key() -> str:
    return f"oauth:{USER_ID}:{PROVIDER}:{NICKNAME}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    client_id = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        logger.info("legacy_calendar_creds_migrated_skipped reason=env_vars_absent")
        return 0

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db_migrate(db)  # ensure migration_39 has run

    store = OAuthStore(db)
    existing_row = store.get_account(USER_ID, PROVIDER, NICKNAME)
    existing_secret = secrets_manager.load(_secret_key())

    if existing_row and existing_secret:
        logger.info(f"legacy_calendar_creds_migrated_skipped reason=already_present nickname={NICKNAME}")
        return 0

    if existing_row and not existing_secret:
        # Row but no secret — fill the secret half.
        import json as _json

        secrets_manager.store(
            _secret_key(),
            _json.dumps(
                {
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scopes": "",
                }
            ),
        )
        logger.warning(f"legacy_calendar_creds_migrated nickname={NICKNAME} half=secret_only")
        return 0

    # No row — full insert.
    store.add_account(
        user_id=USER_ID,
        provider=PROVIDER,
        nickname=NICKNAME,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        scopes="https://www.googleapis.com/auth/calendar",
        metadata={},
    )
    logger.warning(f"legacy_calendar_creds_migrated nickname={NICKNAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
