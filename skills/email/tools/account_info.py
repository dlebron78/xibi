import os
import re
from pathlib import Path


def run(params):
    """
    Get the configured email account information.
    Reads from ~/.config/himalaya/config.toml, falls back to BREGGER_EMAIL_FROM.
    Use this to determine the user's own email address.
    """
    emails = []

    # 1. Check himalaya config (System of record)
    himalaya_config = Path.home() / ".config" / "himalaya" / "config.toml"
    if himalaya_config.exists():
        try:
            content = himalaya_config.read_text()
            # Match lines like: email = "dan@example.com"
            matches = re.findall(r'^[ \t]*email\s*=\s*["\']([^"\']+)["\']', content, flags=re.MULTILINE)
            for m in matches:
                if m not in emails:
                    emails.append(m)
        except Exception as e:
            print(f"⚠️ [account_info] Failed to read himalaya config: {e}", flush=True)

    # 2. Check environment variable fallback
    env_email = os.environ.get("BREGGER_EMAIL_FROM")
    if env_email and env_email not in emails:
        emails.append(env_email)

    # 3. Handle no accounts found
    if not emails:
        return {
            "status": "error",
            "message": "No email accounts found in Himalaya config or environment variables.",
            "suggestion": "Run the configure_email tool to set up an account.",
        }

    return {"status": "success", "data": {"configured_emails": emails, "primary_email": emails[0]}}
