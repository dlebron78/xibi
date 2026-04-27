"""Outbound From-header builder for Roberto.

Roberto sends from a single SMTP-authenticated address; only the
*display name* varies per account. Industry consensus (verified
2026-04-27) discourages send-as spoofing of the user's alias. Reply-To
is the routing primitive (see ``xibi.email.reply_to``); From-name is
the brand surface.

The caller (``skills/email/tools/send_email.py``) supplies the SMTP
address — keeping this helper provider-agnostic and free of any legacy
env-var literal in the xibi/ package.
"""

from __future__ import annotations

import os

DEFAULT_FROM_NAME = "Daniel via Roberto"


def build_from_header(account: str | None, addr: str = "") -> str:
    """Compose ``"<display_name>" <addr>`` for the From header.

    Display-name precedence:
      1. ``XIBI_OUTBOUND_FROM_NAME_<account>`` (per-account override)
      2. ``XIBI_OUTBOUND_FROM_NAME`` (global override)
      3. ``"Daniel via Roberto"`` (hardcoded fallback)

    ``addr`` is the SMTP-authenticated address provided by the caller.
    When empty (no SMTP user configured), returns just the display name —
    the SMTP layer will fail with a clearer error than this helper trying
    to cover for missing config.
    """
    name = (
        (account and os.environ.get(f"XIBI_OUTBOUND_FROM_NAME_{account}"))
        or os.environ.get("XIBI_OUTBOUND_FROM_NAME")
        or DEFAULT_FROM_NAME
    )
    addr = (addr or "").strip()
    if addr:
        return f'"{name}" <{addr}>'
    return name
