import sqlite3
from pathlib import Path
from xibi.db.migrations import migrate

db_path = Path("test_migration.db")
if db_path.exists():
    db_path.unlink()

migrate(db_path)

conn = sqlite3.connect(db_path)
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cursor.fetchall()]
conn.close()

print(f"Tables: {tables}")
assert "priority_context" in tables
assert "review_traces" in tables

db_path.unlink()
print("Migration verified successfully.")
