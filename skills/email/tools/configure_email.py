import os
from pathlib import Path

def run(params):
    """Configure himalaya config.toml with provided credentials."""
    email = params.get("email")
    imap_host = params.get("imap_host")
    smtp_host = params.get("smtp_host")
    password = params.get("password")
    
    # Defensive check for schema leakage
    for key, val in [("email", email), ("imap_host", imap_host), ("smtp_host", smtp_host), ("password", password)]:
        if isinstance(val, dict):
            return {
                "status": "error",
                "message": f"It looks like I got confused and sent a schema for '{key}' instead of the value. Please try again with plain text."
            }
    
    if not all([email, imap_host, smtp_host, password]):
        return {"status": "error", "message": "Missing required configuration parameters (email, imap_host, smtp_host, password)."}

    config_content = f"""
[bregger]
default = true
email = "{email}"

[bregger.backend]
type = "imap"
host = "{imap_host}"
port = 993
login = "{email}"
passwd.cmd = "echo {password}"

[bregger.sender]
type = "smtp"
host = "{smtp_host}"
port = 465
login = "{email}"
passwd.cmd = "echo {password}"
"""
    
    config_dir = Path.home() / ".config" / "himalaya"
    config_file = config_dir / "config.toml"

    try:
        # 1. Create directory
        config_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Write file
        with open(config_file, "w") as f:
            f.write(config_content.strip() + "\n")
        
        # 3. Set permissions (600 - Read/Write for owner only)
        os.chmod(config_file, 0o600)
        
        return {
            "status": "success",
            "message": f"Successfully configured email for {email}. Permissions set to 600."
        }
    except Exception as e:
        return {"status": "error", "message": f"Configuration failed: {str(e)}"}
