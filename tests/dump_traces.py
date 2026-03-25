import os
import sqlite3
import json
import sys

data_dir = os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "bregger_remote"))
db_path = os.path.join(data_dir, "data", "bregger.db")

try:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Test 1 is the 20 runs before the last 20 runs
        rows_test1 = conn.execute(
            "SELECT started_at, steps_detail FROM traces WHERE intent='react' ORDER BY started_at DESC LIMIT 20 OFFSET 20"
        ).fetchall()

        failed_t1 = []
        for r in rows_test1:
            try:
                js = json.loads(r["steps_detail"])
                if len(js) > 0:
                    first_tool = js[0].get("tool")
                    if first_tool != "list_files":
                        failed_t1.append(js)
            except Exception as e:
                pass

        print(json.dumps(failed_t1[:2]))
except Exception as e:
    print(f"DB Error: {e}")
