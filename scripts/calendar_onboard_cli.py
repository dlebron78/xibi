"""Headless / scripted calendar onboarding fallback.

Use when the Telegram + browser flow is not available (e.g. server with no
GUI, automated provisioning). Prints the OAuth URL, waits for the user to
paste back the auth code, then completes the same DB write the HTTP
callback server would have done.

Usage:
    python3 scripts/calendar_onboard_cli.py <nickname>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xibi.db import migrate as db_migrate  # noqa: E402
from xibi.oauth.google import (  # noqa: E402
    DEFAULT_CALENDAR_SCOPES,
    build_authorization_url,
    exchange_code_for_refresh_token,
    fetch_userinfo,
    refresh_access_token,
)
from xibi.oauth.store import OAuthStore  # noqa: E402

logger = logging.getLogger("calendar_onboard_cli")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="Headless OAuth onboarding for Xibi.")
    parser.add_argument("nickname", help="Account nickname (e.g. afya)")
    parser.add_argument(
        "--provider",
        default="google_calendar",
        help="OAuth provider id. v1 only supports google_calendar.",
    )
    args = parser.parse_args()

    db = Path(os.environ.get("XIBI_DB_PATH", str(Path.home() / ".xibi" / "data" / "xibi.db")))
    db.parent.mkdir(parents=True, exist_ok=True)
    db_migrate(db)

    store = OAuthStore(db)
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    if store.get_account(user_id, args.provider, args.nickname):
        print(f"Account '{args.nickname}' already exists for {args.provider}. Disconnect it first.")
        return 2

    state_token = store.create_pending_state(user_id, args.provider, args.nickname, ttl_minutes=10)
    url = build_authorization_url(state_token, scopes=DEFAULT_CALENDAR_SCOPES)
    print()
    print(
        "Open this URL in a browser, complete the consent screen, then paste the code parameter from the redirect URL:"
    )
    print()
    print(url)
    print()
    code = input("auth code> ").strip()
    if not code:
        print("No code provided; aborting.")
        return 2

    pending = store.consume_pending_state(state_token)
    if not pending:
        print("State token expired or already consumed; rerun the script.")
        return 2

    creds = exchange_code_for_refresh_token(code)
    metadata = {}
    try:
        access_token = (
            creds.get("access_token")
            or refresh_access_token(creds["refresh_token"], creds["client_id"], creds["client_secret"])[0]
        )
        userinfo = fetch_userinfo(access_token)
        if userinfo.get("email"):
            metadata["email_alias"] = userinfo["email"].lower()
    except Exception as e:
        logger.warning(f"oauth_userinfo_fetch_failed err={type(e).__name__}")

    store.add_account(
        user_id=user_id,
        provider=args.provider,
        nickname=args.nickname,
        refresh_token=creds["refresh_token"],
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        scopes=creds.get("scope", ""),
        metadata=metadata,
    )
    suffix = f" ({metadata['email_alias']})" if metadata.get("email_alias") else ""
    print(f"\n✅ Connected '{args.nickname}'{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
