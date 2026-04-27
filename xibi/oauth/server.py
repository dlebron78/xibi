"""HTTP callback server for OAuth flows.

Runs as a separate systemd unit on localhost:8765 (override via
XIBI_OAUTH_CALLBACK_PORT). The on_account_added callback is responsible
for routing the success notification — typically via Telegram. The lookup
of the destination chat_id happens inside the callback (see
xibi-oauth-callback.service for how it is wired): for single-user
deployments the value comes from XIBI_TELEGRAM_CHAT_ID; for future
multi-tenant deployments it would come from a users table indexed by
``user_id``.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from xibi.oauth.google import (
    exchange_code_for_refresh_token,
    fetch_userinfo,
    refresh_access_token,
)
from xibi.oauth.store import OAuthStore

logger = logging.getLogger(__name__)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: OAuthCallbackServer  # narrows the parent attr for type-checkers

    def log_message(self, format: str, *args: Any) -> None:
        # Route to logging instead of stderr so spans/logs stay coherent.
        logger.info("oauth_callback_http " + (format % args))

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if not self.path.startswith("/oauth/callback"):
            return self._respond(404, "Not found")

        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = next(iter(params.get("code") or []), None)
        state = next(iter(params.get("state") or []), None)
        error = next(iter(params.get("error") or []), None)

        if error:
            logger.warning(f"oauth_callback_provider_error error={error}")
            return self._respond(400, "Provider returned an error; please retry from Telegram.")

        if not code or not state:
            return self._respond(400, "Missing code or state.")

        store = OAuthStore(self.server.db_path)
        pending = store.consume_pending_state(state)
        if not pending:
            logger.warning(f"oauth_csrf_state_mismatch state={state}")
            return self._respond(403, "Invalid or expired state token.")

        # Exchange auth code → refresh + access token.
        try:
            creds = exchange_code_for_refresh_token(code)
        except Exception as e:
            logger.error(f"oauth_token_exchange_failed nickname={pending['nickname']} err={type(e).__name__}")
            return self._respond(500, "Token exchange failed; check server logs.")

        # Best-effort: capture the authenticated account's primary email so
        # step-109 can route inbound mail to the right calendar context.
        # Failure here is logged but does NOT abort onboarding.
        metadata: dict[str, Any] = {}
        try:
            access_token = creds.get("access_token")
            if not access_token:
                access_token, _ = refresh_access_token(
                    creds["refresh_token"],
                    creds["client_id"],
                    creds["client_secret"],
                )
            userinfo = fetch_userinfo(access_token)
            email = (userinfo.get("email") or "").strip().lower()
            if email:
                metadata["email_alias"] = email
        except Exception as e:
            logger.warning(f"oauth_userinfo_fetch_failed nickname={pending['nickname']} err={type(e).__name__}")

        try:
            store.add_account(
                user_id=pending["user_id"],
                provider=pending["provider"],
                nickname=pending["nickname"],
                refresh_token=creds["refresh_token"],
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                scopes=creds.get("scope", ""),
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"oauth_account_persist_failed nickname={pending['nickname']} err={type(e).__name__}")
            return self._respond(500, "Failed to persist account; check server logs.")

        if self.server.on_account_added:
            try:
                self.server.on_account_added(
                    pending["user_id"],
                    pending["provider"],
                    pending["nickname"],
                    metadata.get("email_alias"),
                )
            except Exception as e:
                logger.warning(f"oauth_on_added_callback_failed err={type(e).__name__}")

        return self._respond(200, "✅ Connected. You can close this tab.")

    def _respond(self, status: int, message: str) -> None:
        body = (
            f"<!doctype html><html><body style='font-family: sans-serif; "
            f"text-align: center; margin-top: 6em;'><h1>{status}</h1>"
            f"<p>{message}</p></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OAuthCallbackServer(HTTPServer):
    db_path: Path
    on_account_added: Callable[[str, str, str, str | None], None] | None


def _default_db_path() -> Path:
    return Path(os.environ.get("XIBI_DB_PATH", str(Path.home() / ".xibi" / "data" / "xibi.db")))


def _default_telegram_notifier(user_id: str, provider: str, nickname: str, email_alias: str | None) -> None:
    """Send a Telegram message announcing account-added.

    Uses XIBI_TELEGRAM_CHAT_ID for single-user instances. Future Stage 2
    deployments would look up chat_id by user_id from a users table.
    """
    chat_id = (
        os.environ.get("XIBI_TELEGRAM_CHAT_ID")
        or os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")[0].strip()
    )
    token = os.environ.get("XIBI_TELEGRAM_TOKEN")
    if not chat_id or not token:
        logger.info(
            f"oauth_account_added user_id={user_id} provider={provider} nickname={nickname} (no telegram creds)"
        )
        return

    suffix = f" ({email_alias})" if email_alias else ""
    text = f"✅ Connected your '{nickname}' calendar{suffix}.\nTry: \"what's on my {nickname} calendar this week?\""
    import urllib.request as _urlreq

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = _urlreq.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        _urlreq.urlopen(req, timeout=10).read()
    except Exception as e:
        logger.warning(f"oauth_telegram_notify_failed err={type(e).__name__}")


def run_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    db_path: Path | None = None,
    on_account_added: Callable[[str, str, str, str | None], None] | None = None,
) -> None:
    """Start the callback server. Blocks forever."""
    port = port or int(os.environ.get("XIBI_OAUTH_CALLBACK_PORT", "8765"))
    server = OAuthCallbackServer((host, port), OAuthCallbackHandler)
    server.db_path = db_path or _default_db_path()
    server.on_account_added = on_account_added or _default_telegram_notifier
    logger.info(f"oauth_callback_server_listening host={host} port={port} db_path={server.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_server()
