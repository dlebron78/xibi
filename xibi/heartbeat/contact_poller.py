"""Poll sent mail and populate contacts + contact_channels tables."""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xibi.db import open_db
from xibi.signal_intelligence import _upsert_contact_core

logger = logging.getLogger(__name__)

# Himalaya account that holds Daniel's sent mail (used for contact population).
# Roberto's account (default) is the inbox; Daniel's personal Gmail has the sent history.
SENT_MAIL_ACCOUNT = "daniel"

# Step-110 finishing: nickname under oauth_accounts.metadata that the
# `daniel` himalaya account corresponds to. Used to stamp account_origin
# on contacts created from sent-folder polling. Override via env if the
# polling account is reconfigured to a different OAuth nickname.
SENT_MAIL_ACCOUNT_NICKNAME = os.environ.get("XIBI_SENT_MAIL_NICKNAME", "personal")

SENT_FOLDER_CANDIDATES = [
    "Sent",
    "[Gmail]/Sent Mail",
    "Sent Items",  # Outlook
    "Sent Messages",  # Some IMAP
    "INBOX.Sent",  # Dovecot
]


def find_himalaya() -> str:
    """Locate himalaya binary. Checks PATH, ~/.local/bin, ~/.cargo/bin."""
    himalaya_bin = shutil.which("himalaya")
    if not himalaya_bin:
        home = Path.home()
        for path in [home / ".local" / "bin" / "himalaya", home / ".cargo" / "bin" / "himalaya"]:
            if path.exists():
                himalaya_bin = str(path)
                break
    return himalaya_bin or "himalaya"


def _discover_sent_folder(himalaya_bin: str, db_path: Path) -> str | None:
    """Try candidate folder names, return the first that works. Cache in DB."""
    try:
        with open_db(db_path) as conn:
            cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'sent_folder_name'")
            row = cursor.fetchone()
            if row:
                return str(row[0])
    except Exception:
        pass

    for folder in SENT_FOLDER_CANDIDATES:
        try:
            cmd = [
                himalaya_bin,
                "--account",
                SENT_MAIL_ACCOUNT,
                "--output",
                "json",
                "envelope",
                "list",
                "--folder",
                folder,
                "--page-size",
                "1",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                # Validate output is a JSON array
                try:
                    data = json.loads(res.stdout)
                    if isinstance(data, list):
                        logger.info(f"Discovered sent folder: {folder}")
                        _cache_sent_folder(folder, db_path)
                        return folder
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue

    logger.warning("Could not discover sent folder.")
    return None


def _cache_sent_folder(folder: str, db_path: Path) -> None:
    try:
        with open_db(db_path) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('sent_folder_name', ?)", (folder,)
            )
    except Exception as e:
        logger.error(f"Failed to cache sent folder name: {e}")


def _list_envelopes(
    himalaya_bin: str,
    folder: str | None = None,
    page_size: int = 50,
    page: int = 1,
) -> list[dict]:
    """List envelopes from a specific folder (or inbox if None)."""
    cmd = [
        himalaya_bin,
        "--account",
        SENT_MAIL_ACCOUNT,
        "--output",
        "json",
        "envelope",
        "list",
        "--page-size",
        str(page_size),
        "--page",
        str(page),
    ]
    if folder:
        cmd.extend(["--folder", folder])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            logger.error(f"Himalaya list failed: {res.stderr}")
            return []
        data = json.loads(res.stdout)
        if not isinstance(data, list):
            return []
        return data
    except Exception as e:
        logger.error(f"Error listing envelopes: {e}")
        return []


def _extract_recipients(himalaya_bin: str, envelope: dict) -> list[dict]:
    """Extract all recipients from an envelope."""
    recipients = []

    to_field = envelope.get("to", [])
    cc_field = envelope.get("cc", [])
    bcc_field = envelope.get("bcc", [])

    if not to_field and not cc_field:
        return _fetch_recipients_full(himalaya_bin, envelope["id"])

    def parse_addr(raw: Any, role: str) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            return {"name": raw.get("name", ""), "addr": raw.get("addr", ""), "role": role}
        elif isinstance(raw, str):
            if "<" in raw and ">" in raw:
                name, addr = raw.split("<", 1)
                return {"name": name.strip(), "addr": addr.split(">", 1)[0].strip(), "role": role}
            return {"name": "", "addr": raw.strip(), "role": role}
        return None

    if isinstance(to_field, (str, dict)):
        to_field = [to_field]
    if isinstance(cc_field, (str, dict)):
        cc_field = [cc_field]
    if isinstance(bcc_field, (str, dict)):
        bcc_field = [bcc_field]

    for r in to_field:
        item = parse_addr(r, "to")
        if item:
            recipients.append(item)
    for r in cc_field:
        item = parse_addr(r, "cc")
        if item:
            recipients.append(item)
    for r in bcc_field:
        item = parse_addr(r, "bcc")
        if item:
            recipients.append(item)

    return recipients


def _fetch_recipients_full(himalaya_bin: str, email_id: str) -> list[dict]:
    """Fetch full RFC 5322 headers and parse To/CC/BCC."""
    try:
        cmd = [himalaya_bin, "--account", SENT_MAIL_ACCOUNT, "message", "export", "--full", str(email_id)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            return []

        recipients = []
        import email
        from email import policy
        from email.utils import getaddresses

        msg = email.message_from_string(res.stdout, policy=policy.default)

        for role in ["to", "cc", "bcc"]:
            addrs = msg.get_all(role, [])
            for addr_obj in addrs:
                for name, addr in getaddresses([str(addr_obj)]):
                    if addr:
                        recipients.append({"name": name, "addr": addr, "role": role})
        return recipients
    except Exception as e:
        logger.error(f"Error fetching full recipients for {email_id}: {e}")
        return []


def poll_sent_folder(
    himalaya_bin: str,
    db_path: Path,
    hours_back: int = 1,
    page_size: int = 50,
) -> dict:
    """Scan sent folder for recent emails, extract recipients, upsert contacts."""
    stats = {"emails_scanned": 0, "contacts_created": 0, "contacts_updated": 0, "errors": 0}

    folder = _discover_sent_folder(himalaya_bin, db_path)
    if not folder:
        stats["errors"] += 1
        return stats

    watermark = None
    try:
        with open_db(db_path) as conn:
            cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'sent_mail_watermark'")
            row = cursor.fetchone()
            if row:
                watermark = datetime.fromisoformat(row[0])
    except Exception:
        pass

    if not watermark:
        watermark = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    page = 1
    new_watermark = watermark
    reached_watermark = False

    while not reached_watermark:
        envelopes = _list_envelopes(himalaya_bin, folder, page_size, page)
        if not envelopes:
            break

        stats["emails_scanned"] += len(envelopes)

        for env in envelopes:
            try:
                env_date_str = env.get("date", "")
                if "T" in env_date_str:
                    env_date = datetime.fromisoformat(env_date_str.replace("Z", "+00:00"))
                    if env_date <= watermark:
                        reached_watermark = True
                        break
                    if env_date > new_watermark:
                        new_watermark = env_date

                recipients = _extract_recipients(himalaya_bin, env)
                for rec in recipients:
                    if rec["addr"]:
                        _upsert_contact_core(
                            email=rec["addr"],
                            display_name=rec["name"] or rec["addr"] or "",
                            organization=None,
                            db_path=db_path,
                            direction="outbound",
                            activity_date=env_date_str if "T" in env_date_str else None,
                            received_via_account=SENT_MAIL_ACCOUNT_NICKNAME,
                        )
            except Exception as e:
                logger.error(f"Failed to process sent email {env.get('id')}: {e}")
                stats["errors"] += 1

        if len(envelopes) < page_size:
            break
        page += 1

    if new_watermark > watermark:
        try:
            with open_db(db_path) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('sent_mail_watermark', ?)",
                    (new_watermark.isoformat(),),
                )
        except Exception:
            pass

    return stats


def backfill_contacts(
    himalaya_bin: str,
    db_path: Path,
    days_back: int = 90,
) -> dict:
    """One-time backfill: scan last N days of sent + received mail."""
    stats = {"sent_scanned": 0, "received_scanned": 0, "contacts_created": 0}
    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)

    # 1. Backfill Sent mail
    folder = _discover_sent_folder(himalaya_bin, db_path)
    if folder:
        page = 1
        page_size = 100
        reached_limit = False
        while not reached_limit:
            envelopes = _list_envelopes(himalaya_bin, folder, page_size, page)
            if not envelopes:
                break

            stats["sent_scanned"] += len(envelopes)
            for env in envelopes:
                try:
                    env_date_str = env.get("date", "")
                    if "T" in env_date_str:
                        env_date = datetime.fromisoformat(env_date_str.replace("Z", "+00:00"))
                        if env_date < after_date:
                            reached_limit = True
                            break

                    recipients = _extract_recipients(himalaya_bin, env)
                    for rec in recipients:
                        if rec["addr"]:
                            _upsert_contact_core(
                                email=rec["addr"],
                                display_name=rec["name"] or rec["addr"] or "",
                                organization=None,
                                db_path=db_path,
                                direction="outbound",
                                activity_date=env_date_str if "T" in env_date_str else None,
                                received_via_account=SENT_MAIL_ACCOUNT_NICKNAME,
                            )
                except Exception:
                    continue

            if len(envelopes) < page_size:
                break
            page += 1

    # 2. Backfill Received mail
    page = 1
    page_size = 100
    reached_limit = False
    while not reached_limit:
        envelopes = _list_envelopes(himalaya_bin, folder=None, page_size=page_size, page=page)
        if not envelopes:
            break

        stats["received_scanned"] += len(envelopes)
        for env in envelopes:
            try:
                env_date_str = env.get("date", "")
                if "T" in env_date_str:
                    env_date = datetime.fromisoformat(env_date_str.replace("Z", "+00:00"))
                    if env_date < after_date:
                        reached_limit = True
                        break

                sender = env.get("from", {})
                if isinstance(sender, dict):
                    addr = sender.get("addr")
                    name = sender.get("name") or addr
                else:
                    addr = str(sender)
                    name = addr

                if addr:
                    # v1 hotfix: tag inbound backfill with the polling account's
                    # nickname. Future: parse To:/Delivered-To: headers and call
                    # resolve_account_from_email_to for true per-email provenance.
                    _upsert_contact_core(
                        email=addr,
                        display_name=name or addr,
                        organization=None,
                        db_path=db_path,
                        direction="inbound",
                        activity_date=env_date_str if "T" in env_date_str else None,
                        received_via_account=SENT_MAIL_ACCOUNT_NICKNAME,
                    )
            except Exception:
                continue

        if len(envelopes) < page_size:
            break
        page += 1

    return stats
