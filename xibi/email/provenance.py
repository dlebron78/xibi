"""Inbound email provenance: match To/Delivered-To header against connected accounts.

Roberto reads ONE himalaya inbox; mail forwarded from Daniel's other addresses
(e.g. lebron@afya.fit, dannylebron@gmail.com) preserves the original `To:` (and
more reliably `Delivered-To:`) header. This module turns that header into a
matched `oauth_accounts` row so downstream readers can tag each email with
``received_via_account`` / ``received_via_email_alias``.

Matching is against ``oauth_accounts.metadata.email_alias`` (populated by step-108
at OAuth callback time from Google's userinfo endpoint). ``email_alias`` is the
canonical identity bound to the OAuth grant — nicknames are user-renamable
display labels and are NOT trusted for routing.

Resolution rules:
  - ``Delivered-To`` is checked first (more reliable for forwarded mail).
  - Then ``To`` candidates, in order, with case-insensitive comparison.
  - First match wins; ties (CC'd to two of your aliases) log INFO.
  - No fall-through to a "default" account on miss — return ``None`` and log
    WARNING so unmatched aliases stay observable.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# RFC 5322 angle-addr / addr-spec extractor. Greedy-enough for the realistic
# `"Display Name" <addr@domain>, addr2@domain` shapes we see; not a full grammar.
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def _instance_user_id() -> str:
    return os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")


def parse_addresses_from_header(header: str | None) -> list[str]:
    """Extract email addresses from a header value.

    Handles ``addr@x.com``, ``"Name" <addr@x.com>``, comma-separated mixes,
    and malformed input (returns ``[]``). Output is lowercased, deduplicated,
    and order-preserving (first occurrence wins).
    """
    if not header:
        return []
    found = _EMAIL_RE.findall(str(header))
    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        addr = raw.strip().lower()
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def resolve_account_from_email_to(
    to_addresses: list[str] | str | None,
    delivered_to: str | None = None,
    db_path: str | Path | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Match inbound email's To/Delivered-To against ``oauth_accounts.email_alias``.

    Returns the matched account row (without secret material) or ``None``.
    Logs WARNING when there's no match — operator visibility for forwarding
    rules that drift away from configured aliases.

    ``Delivered-To`` is consulted before ``To`` because forwarded mail often
    keeps the ORIGINAL ``To:`` (which may be a list address or another alias)
    while ``Delivered-To`` reflects the forwarder's actual destination.
    """
    if db_path is None:
        return None

    # Normalize to_addresses input to a list of header strings.
    if to_addresses is None:
        to_headers: list[str] = []
    elif isinstance(to_addresses, str):
        to_headers = [to_addresses]
    else:
        to_headers = [t for t in to_addresses if t]

    # Build candidate list: Delivered-To first, then To (in input order), deduped.
    candidates: list[str] = []
    if delivered_to:
        for addr in parse_addresses_from_header(delivered_to):
            if addr not in candidates:
                candidates.append(addr)
    for header in to_headers:
        for addr in parse_addresses_from_header(header):
            if addr not in candidates:
                candidates.append(addr)

    if not candidates:
        return None

    uid = user_id or _instance_user_id()

    matches: list[tuple[str, dict[str, Any]]] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for addr in candidates:
                row = conn.execute(
                    "SELECT id, user_id, provider, nickname, scopes, metadata, status, "
                    "created_at, last_used_at "
                    "FROM oauth_accounts "
                    "WHERE user_id = ? "
                    "AND lower(json_extract(metadata, '$.email_alias')) = ?",
                    (uid, addr),
                ).fetchone()
                if row:
                    import json as _json

                    metadata_raw = row["metadata"] or "{}"
                    try:
                        metadata = _json.loads(metadata_raw)
                    except (ValueError, TypeError):
                        metadata = {}
                    matches.append(
                        (
                            addr,
                            {
                                "id": row["id"],
                                "user_id": row["user_id"],
                                "provider": row["provider"],
                                "nickname": row["nickname"],
                                "scopes": row["scopes"],
                                "status": row["status"],
                                "metadata": metadata,
                                "created_at": row["created_at"],
                                "last_used_at": row["last_used_at"],
                                "email_alias": metadata.get("email_alias"),
                                "matched_address": addr,
                            },
                        )
                    )
    except sqlite3.Error as e:
        logger.warning(f"email_provenance_lookup_error err={type(e).__name__}:{e}")
        return None

    if not matches:
        joined = ",".join(candidates[:3])
        logger.warning(f"email_provenance_unmatched to_candidates={joined}")
        return None

    chosen_addr, chosen_row = matches[0]
    if len(matches) > 1:
        addrs = ",".join(a for a, _ in matches)
        logger.info(f"email_provenance_multiple_match to_addrs={addrs} chose={chosen_row['nickname']}")
    else:
        logger.info(f"email_provenance_resolved to={chosen_addr} account={chosen_row['nickname']}")
    return chosen_row
