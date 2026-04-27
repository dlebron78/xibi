"""Per-account body signature templating for outbound email.

Resolution precedence (per step-110):
  1. ``XIBI_SIGNATURE_<account>`` env var (e.g. XIBI_SIGNATURE_afya)
  2. ``XIBI_SIGNATURE`` env var (single fallback)
  3. ``""`` — no signature appended

Env-var values may use literal ``\\n`` for newlines (shell-friendly); they are
normalized to real newlines before any downstream comparison so dedup matches
the user-visible signature, not the raw env literal (C7 in TRR).
"""

from __future__ import annotations

import os


def resolve_signature(account: str | None) -> str:
    """Return the normalized signature text for an account context.

    Order: XIBI_SIGNATURE_<account> > XIBI_SIGNATURE > "".
    Literal-``\\n`` escapes in the env value are converted to real newlines
    BEFORE returning so all callers downstream see the canonical form.
    """
    if account:
        per_account = os.environ.get(f"XIBI_SIGNATURE_{account}")
        if per_account:
            return per_account.replace("\\n", "\n").strip()
    fallback = os.environ.get("XIBI_SIGNATURE", "")
    return fallback.replace("\\n", "\n").strip()


def should_append_signature(body: str, signature: str) -> bool:
    """Return True if the signature is non-empty AND not already present.

    Conservative substring check on the last 200 chars of the body. Both
    body and the signature's first line are lowercased for the comparison
    so casing differences don't cause spurious double-append. Inputs are
    assumed already normalized (real newlines, not literal ``\\n``).
    """
    if not signature:
        return False
    sig_first_line = signature.split("\n")[0].strip().lower()
    if not sig_first_line:
        return False
    tail = body.strip().lower()[-200:]
    return sig_first_line not in tail


def apply_signature(body: str, signature: str) -> str:
    """Append signature with a single blank-line separator if needed."""
    if not should_append_signature(body, signature):
        return body
    return f"{body.rstrip()}\n\n{signature}"
