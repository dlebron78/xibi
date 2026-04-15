from pathlib import Path
import unittest
import sqlite3
from xibi.subagent.db import create_run, cleanup_expired_runs
from xibi.subagent.models import SubagentRun
from datetime import datetime, timedelta

class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("test_cleanup.db")
        if self.db_path.exists():
            self.db_path.unlink()

        from xibi.db.migrations import migrate
        migrate(self.db_path)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_cleanup_expired_runs(self):
        # Create an expired run
        expired_at = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        run1 = SubagentRun(
            id="expired",
            agent_id="test",
            status="DONE",
            trigger="manual",
            output_ttl_hours=1,
            completed_at=expired_at
        )
        create_run(self.db_path, run1)

        # Create a non-expired run
        run2 = SubagentRun(
            id="active",
            agent_id="test",
            status="DONE",
            trigger="manual",
            output_ttl_hours=1,
            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        create_run(self.db_path, run2)

        count = cleanup_expired_runs(self.db_path)
        self.assertEqual(count, 1)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT id FROM subagent_runs").fetchall()
            ids = [r[0] for r in rows]
            self.assertIn("active", ids)
            self.assertNotIn("expired", ids)

if __name__ == "__main__":
    unittest.main()
