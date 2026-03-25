import os
import re
from pathlib import Path

def run(params):
    """Configure TAVILY_API_KEY by updating secrets.env and current process environment."""
    api_key = params.get("api_key")
    if not api_key:
        return {"status": "error", "message": "Missing api_key parameter."}
    
    if isinstance(api_key, dict):
        return {
            "status": "error", 
            "message": "It looks like I got confused and sent a configuration schema instead of your API key. Please try again or provide the key in plain text."
        }

    # Secrets file path on k12
    # We use environment variable for deployment stability
    deploy_dir = os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "bregger_deployment"))
    secrets_path = Path(deploy_dir) / "secrets.env"
    
    try:
        content = ""
        # Assuming the operator sets permissions correctly before running this script
        if secrets_path.exists():
            content = secrets_path.read_text()
        
        # Update or add the TAVILY_API_KEY line
        new_line = f"export TAVILY_API_KEY={api_key}"
        
        if "TAVILY_API_KEY=" in content:
            # Replace existing line
            content = re.sub(r"export TAVILY_API_KEY=.*", new_line, content)
        else:
            # Append new line
            if content and not content.endswith("\n"):
                content += "\n"
            content += new_line + "\n"
            
        secrets_path.write_text(content)
        os.chmod(secrets_path, 0o600)
        
        # Update current process environment so it works immediately
        os.environ["TAVILY_API_KEY"] = api_key
        
        return {
            "status": "success",
            "message": "Tavily API key configured successfully. El Guardian can now search the web!"
        }
    except PermissionError:
        return {"status": "error", "message": "Permission denied: Could not write to secrets.env. Please unlock the file first with 'chmod 600'."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to configure Tavily: {str(e)}"}
