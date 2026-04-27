"""
Shared Google OAuth2 token-refresh utility, multi-account aware.

Token resolution path (per call):
  1. caller passes ``account="<nickname>"`` (defaults to "default")
  2. ``get_access_token`` looks up (user_id, "google_calendar", nickname) in
     the oauth_accounts table via xibi.oauth.store
  3. on cache hit, returns the cached access_token
  4. on miss, exchanges the per-account refresh_token for a fresh access_token
     under a per-account threading.Lock so concurrent callers do not stampede
     Google's token endpoint
  5. ``invalid_grant`` from Google → mark the row revoked + raise
     OAuthRevokedError so callers (calendar tools, poller) can surface the
     right structured error and the heartbeat can nudge the user

Backward compat:
  - ``XIBI_CALENDARS=label:cal_id`` (old single-account format) still parses.
  - The legacy env vars ``GOOGLE_CALENDAR_*`` are NOT consulted by this
    module any more; the one-shot migration script
    ``scripts/migrate_calendar_envvars.py`` lifts them into the DB at
    deploy time.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-account in-memory cache + per-account locks. The locks dict is itself
# guarded by a top-level lock so that the first caller for a brand-new
# account doesn't race in creating it.
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _get_lock(account: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        if account not in _CACHE_LOCKS:
            _CACHE_LOCKS[account] = threading.Lock()
        return _CACHE_LOCKS[account]


def _instance_user_id() -> str:
    return os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")


def _default_db_path() -> Path:
    return Path(os.environ.get("XIBI_DB_PATH", str(Path.home() / ".xibi" / "data" / "xibi.db")))


def load_calendar_config() -> list[dict]:
    """Parse XIBI_CALENDARS env var into list of {label, account, calendar_id}.

    Two formats are accepted (per-entry, mix-and-match permitted):
      - Old:  ``label:cal_id``                    → account defaults to 'default'
      - New:  ``label=account:cal_id``            → explicit account dimension

    Falls back to a single entry ``[{label: 'default', account: 'default',
    calendar_id: 'primary'}]`` if XIBI_CALENDARS is unset.
    """
    raw = os.environ.get("XIBI_CALENDARS")
    if not raw:
        logger.warning("XIBI_CALENDARS not set, falling back to default:primary")
        raw = "default:primary"

    calendars: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue

        # Detect new format: presence of '=' before the ':' separator.
        head, cal_id = entry.split(":", 1)
        cal_id = cal_id.strip()
        if "=" in head:
            label, account = head.split("=", 1)
            label = label.strip()
            account = account.strip() or "default"
        else:
            label = head.strip()
            account = "default"
        if not label or not cal_id:
            continue
        calendars.append({"label": label, "account": account, "calendar_id": cal_id})

    if not calendars:
        return [{"label": "default", "account": "default", "calendar_id": "primary"}]
    return calendars


def get_calendar_label(calendar_id: str) -> str:
    """Reverse lookup: given a calendar_id, return its label. Falls back to calendar_id."""
    for cal in load_calendar_config():
        if cal["calendar_id"] == calendar_id:
            return str(cal["label"])
    return calendar_id


def resolve_calendar_id(label_or_id: str) -> str:
    """Resolve a friendly label to a Google Calendar ID. Pass-through for raw IDs."""
    for cal in load_calendar_config():
        if cal["label"].lower() == label_or_id.lower():
            return str(cal["calendar_id"])
    return label_or_id


def resolve_email_alias_target(label_or_id: str) -> dict | None:
    """Try resolving an email-style ``label_or_id`` against connected accounts.

    Returns ``{"label", "account", "calendar_id"}`` shaped like a regular
    ``load_calendar_config`` entry when the input matches an account's
    ``metadata.email_alias`` (step-108 captures it from Google's userinfo).
    For email_alias matches, ``calendar_id`` defaults to ``"primary"`` of
    that account.

    Returns ``None`` when input has no ``@`` (cheap fast-path), when no
    account matches, or when the OAuth store can't be opened — keeps callers
    falling back to their existing behaviour.
    """
    if not label_or_id or "@" not in label_or_id:
        return None
    try:
        from xibi.oauth.store import OAuthStore

        user_id = _instance_user_id()
        store = OAuthStore(_default_db_path())
        row = store.find_by_email_alias(user_id, label_or_id)
        if not row:
            return None
        nickname = row.get("nickname") or "default"
        return {
            "label": nickname,
            "account": nickname,
            "calendar_id": "primary",
        }
    except Exception as e:
        logger.warning(
            f"calendar_email_alias_resolve_error label={label_or_id} err={type(e).__name__}"
        )
        return None


def resolve_account_for_label(label_or_id: str) -> str:
    """Return the account nickname owning the named label. Defaults to 'default'."""
    for cal in load_calendar_config():
        if cal["label"].lower() == label_or_id.lower():
            return str(cal.get("account", "default"))
        if cal["calendar_id"] == label_or_id:
            return str(cal.get("account", "default"))
    return "default"


def format_date_label(iso_str: str, today: datetime) -> str:
    """Convert an ISO date/datetime string to a human-readable label (Today, Tomorrow, …)."""
    try:
        date_part = iso_str[:10]
        event_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        today_date = today.date()
        delta = (event_date - today_date).days
        if delta == 0:
            return "Today"
        elif delta == 1:
            return "Tomorrow"
        else:
            return event_date.strftime("%A, %b %-d")
    except Exception:
        return iso_str


def format_event_time(iso_str: str) -> str:
    """Convert an ISO datetime string to '3:00 PM', or 'All day' for date-only."""
    if len(iso_str) <= 10:
        return "All day"
    try:
        return datetime.fromisoformat(iso_str).strftime("%-I:%M %p")
    except Exception:
        return iso_str


def get_access_token(account: str = "default") -> str:
    """Return a valid access token for the named account.

    Raises:
        OAuthRevokedError if Google returns invalid_grant. The corresponding
            oauth_accounts row is marked status='revoked' before re-raise so
            future calls short-circuit without the network round-trip.
        RuntimeError if the account is unknown / not configured.
    """
    # Imported lazily so importing this module does not pull the OAuth stack
    # (and through it, sqlite path resolution) for tests that just need the
    # formatting helpers.
    from xibi.oauth.google import OAuthRevokedError, refresh_access_token
    from xibi.oauth.store import OAuthStore

    now = time.time()
    cached = _TOKEN_CACHE.get(account)
    if cached and cached.get("expires_at", 0) - now > 60:
        return str(cached["access_token"])

    with _get_lock(account):
        # Re-check after acquiring the lock — another thread may have refreshed.
        cached = _TOKEN_CACHE.get(account)
        if cached and cached.get("expires_at", 0) - now > 60:
            return str(cached["access_token"])

        user_id = _instance_user_id()
        store = OAuthStore(_default_db_path())
        creds = store.get_account(user_id, "google_calendar", account)
        if not creds:
            raise RuntimeError(f"No OAuth account '{account}' configured. Use /connect_calendar {account} to add it.")
        if creds.get("status") == "revoked":
            raise OAuthRevokedError(account=account)
        if not creds.get("refresh_token"):
            raise RuntimeError(f"OAuth account '{account}' is missing its refresh_token (corrupt store?)")

        try:
            access_token, expires_in = refresh_access_token(
                creds["refresh_token"],
                creds["client_id"],
                creds["client_secret"],
            )
        except OAuthRevokedError:
            store.mark_revoked(user_id, "google_calendar", account)
            logger.warning(f"oauth_token_revoked account={account} provider=google_calendar")
            raise
        except Exception as e:
            logger.warning(
                f"oauth_token_refresh_error account={account} provider=google_calendar err={type(e).__name__}"
            )
            raise

        _TOKEN_CACHE[account] = {
            "access_token": access_token,
            "expires_at": now + expires_in,
        }
        store.touch_last_used(user_id, "google_calendar", account)
        return access_token


def gcal_request(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    account: str = "default",
) -> dict:
    """Make an authenticated request to the Google Calendar API v3."""
    token = get_access_token(account=account)
    url = f"https://www.googleapis.com/calendar/v3{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    import json as _json

    data = _json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = _json.loads(resp.read())
        if not isinstance(result, dict):
            return {}
        return result
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Calendar API error {e.code}: {err_body}") from e
