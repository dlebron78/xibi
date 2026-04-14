import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta

PAGE_SIZE = 5
FETCH_SIZE = 50  # Pull a large batch from Himalaya to filter from

NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _friendly_date(date_str: str) -> str:
    """Convert an ISO date string to a human-friendly relative time."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str)
        now = datetime.now().astimezone()
        dt_local = dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)

        hour = dt_local.strftime("%I").lstrip("0") or "12"
        minute = dt_local.strftime("%M")
        period = dt_local.strftime("%p").lower()
        time_str = f"{hour}:{minute}{period}"

        if dt_local.date() == now.date():
            return time_str
        elif dt_local.date() == (now - timedelta(days=1)).date():
            return f"Yesterday {time_str}"

        delta = now - dt_local
        days_ago = delta.days
        if days_ago < 7:
            return f"{dt_local.strftime('%A')} {time_str}"
        elif days_ago < 60:
            weeks = days_ago // 7
            return f"{dt_local.strftime('%b')} {dt_local.day} ({weeks}w ago)"
        else:
            return f"{dt_local.strftime('%b')} {dt_local.day}"
    except Exception:
        return date_str


def format_page(emails: list, offset: int, total_unread: int) -> str:
    """Render a single page of emails as a numbered Telegram-ready string."""
    if not emails:
        return "📬 No unread emails"

    page_start = offset + 1
    page_end = offset + len(emails)
    if total_unread > page_end:
        header = f"📬 {page_start}–{page_end} of {total_unread} unread"
    elif page_start == 1:
        header = f"📬 {total_unread} unread"
    else:
        header = f"📬 {page_start}–{page_end} of {total_unread} unread"

    lines = []
    for i, email in enumerate(emails):
        emoji = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{offset + i + 1}."
        subject = email.get("subject", "No Subject")
        sender = email.get("from", {}).get("name") or email.get("from", {}).get("addr", "Unknown")
        date = _friendly_date(email.get("date", ""))
        lines.append(f"{emoji} {sender} — {date}\n   {subject}")

    body = "\n\n".join(lines)

    footer = '\n\n↓ Reply "more" for next 5' if total_unread > page_end else ""

    return header + "\n\n" + body + footer


def run(params):
    """List unread emails using himalaya. Returns formatted content + raw list for caching."""
    # Find himalaya binary
    himalaya_bin = shutil.which("himalaya")
    if not himalaya_bin:
        home = os.path.expanduser("~")
        local_path = os.path.join(home, ".local", "bin", "himalaya")
        himalaya_bin = local_path if subprocess.run(["test", "-x", local_path]).returncode == 0 else "himalaya"

    cmd = [
        himalaya_bin,
        "--output",
        "json",
        "envelope",
        "list",
        "--page-size",
        str(FETCH_SIZE),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"Himalaya error: {result.stderr or 'Unknown error'}",
            }

        all_emails = json.loads(result.stdout)

        # Filter to unread only (no "Seen" flag), newest first
        unread = [e for e in all_emails if "Seen" not in e.get("flags", [])]
        total_unread = len(unread)

        # First page only — BreggerCore caches the full list for pagination
        first_page = unread[:PAGE_SIZE]
        content = format_page(first_page, offset=0, total_unread=total_unread)

        return {
            "status": "success",
            "message": f"Found {total_unread} unread emails.",
            "content": content,
            # Raw list passed back for BreggerCore to cache
            "emails": unread,
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to run himalaya: {str(e)}"}
