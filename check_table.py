import sqlite3
from pathlib import Path
db_path = Path("test_check.db")
from xibi.db.migrations import migrate
migrate(db_path)
conn = sqlite3.connect(db_path)
cursor = conn.execute("PRAGMA table_info(observation_cycles)")
columns = [row[1] for row in cursor.fetchall()]
print(f"Columns: {columns}")
db_path.unlink()
