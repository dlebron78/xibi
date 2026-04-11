import sqlite3
from pathlib import Path
from xibi.db.migrations import migrate
from xibi.signal_intelligence import upsert_contact
from xibi.db import open_db

def test_repro():
    db_path = Path("repro.db")
    if db_path.exists():
        db_path.unlink()

    print("Running migrations...")
    migrate(db_path)

    print("Running upsert_contact...")
    upsert_contact("alice@acme.com", "Alice", "Acme Corp", db_path)

    print("Verifying results...")
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts").fetchone()
        if row:
            print(f"Contact found: {dict(row)}")
        else:
            print("Contact NOT found")

        row = conn.execute("SELECT * FROM contact_channels").fetchone()
        if row:
            print(f"Channel found: {dict(row)}")
        else:
            print("Channel NOT found")

if __name__ == "__main__":
    test_repro()
