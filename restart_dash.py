import os
import subprocess
import time

os.system("pkill -9 -f bregger_dashboard.py")
time.sleep(1)
deploy_dir = os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "bregger_deployment"))
dashboard_script = os.path.join(deploy_dir, "bregger_dashboard.py")
subprocess.Popen(
    ["python3", dashboard_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
)
