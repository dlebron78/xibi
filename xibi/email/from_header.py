"""Outbound From-header builder for Roberto.

Roberto sends from a single SMTP-authenticated address (``BREGGER_EMAIL_FROM``);
only the *display name* varies per account. Industry consensus (verified
2026-04-27) discourages send-as spoofing of the user's alias. Reply-To is
the routing primitive (see ``xibi.email.reply_to``); From-name is the
brand surface.
"""

from __future__ import annotations

import os

DEFAULT_FROM_NAME = "Daniel via Roberto"


def build_from_header(account: str | None) -> str:
    """Compose ``"<display_name>" <addr>`` for the From header.

    Display-name precedence:
      1. ``XIBI_OUTBOUND_FROM_NAME_<account>`` (per-account override)
      2. ``XIBI_OUTBOUND_FROM_NAME`` (global override)
      3. ``"Daniel via Roberto"`` (hardcoded fallback)

    Address is always ``BREGGER_EMAIL_FROM``. If that env var is unset,
    return just the display name — SMTP layer will fail with a clearer
    error than this helper trying to cover for missing config.
    """
    addr = os.environ.get("BREGGER_EMAIL_FROM", "").strip()
    name = (
        (account and os.environ.get(f"XIBI_OUTBOUND_FROM_NAME_{account}"))
        or os.environ.get("XIBI_OUTBOUND_FROM_NAME")
        or DEFAULT_FROM_NAME
    )
    if addr:
        return f'"{name}" <{addr}>'
    return name
