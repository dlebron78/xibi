import sqlite3
from datetime import datetime


class ReasoningSeeder:
    def __init__(self, db_path):
        self.db_path = db_path

    def seed_beliefs(self, beliefs):
        """
        beliefs: list of dicts with {key, value, type, visibility}
        """
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            for b in beliefs:
                conn.execute(
                    "INSERT INTO beliefs (key, value, type, visibility, metadata, valid_from, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (b["key"], b["value"], b["type"], b.get("visibility", "user"), "{}", now, now),
                )
            conn.commit()

    def seed_ledger(self, ledger_entries):
        """
        ledger_entries: list of dicts with {id, category, content}
        """
        with sqlite3.connect(self.db_path) as conn:
            for e in ledger_entries:
                conn.execute(
                    "INSERT INTO ledger (id, category, content, created_at) VALUES (?, ?, ?, ?)",
                    (e.get("id", e["category"]), e["category"], e["content"], datetime.now()),
                )
            conn.commit()

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM beliefs")
            conn.execute("DELETE FROM ledger")
            conn.commit()
