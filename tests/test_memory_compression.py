import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from xibi.db import migrate, open_db
from xibi.memory import compress_session_turns
from xibi.session import SessionContext


class TestMemoryCompression(unittest.TestCase):
    def setUp(self):
        self.db_file = Path("test_xibi.db")
        if self.db_file.exists():
            self.db_file.unlink()
        migrate(self.db_file)
        self.session_id = "test-session"
        self.mock_model = MagicMock()

    def tearDown(self):
        if self.db_file.exists():
            self.db_file.unlink()

    def _add_turns(self, count, offset=0):
        with open_db(self.db_file) as conn:
            for i in range(count):
                idx = i + offset
                conn.execute(
                    "INSERT INTO session_turns (turn_id, session_id, query, answer) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), self.session_id, f"query {idx}", f"answer {idx}"),
                )

    def test_compress_when_turn_threshold_exceeded(self):
        self._add_turns(50)
        self.mock_model.generate.return_value = "User: Likes tests\nOngoing: Memory compression"

        result = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["turn_range"], "1-20")

        with open_db(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM belief_summaries WHERE session_id = ?", (self.session_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["turn_range"], "1-20")
            self.assertIn("User: Likes tests", row["summary"])

    def test_no_compress_when_below_threshold(self):
        self._add_turns(49)
        result = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )
        self.assertEqual(result["status"], "skipped")
        self.assertIn("below threshold", result["reason"])

    def test_belief_summary_format_correct(self):
        self._add_turns(50)
        self.mock_model.generate.return_value = "User: Loves Python\nOngoing: Step 57"

        compress_session_turns(self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20)

        with open_db(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT summary FROM belief_summaries").fetchone()
            lines = row["summary"].splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].startswith("User: "))
            self.assertTrue(lines[1].startswith("Ongoing: "))

    def test_compression_is_idempotent(self):
        self._add_turns(50)
        self.mock_model.generate.return_value = "Summary"

        res1 = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )
        res2 = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )

        self.assertEqual(res1["status"], "success")
        self.assertEqual(res2["status"], "skipped")
        self.assertIn("already compressed", res2["reason"])

        with open_db(self.db_file) as conn:
            count = conn.execute("SELECT COUNT(*) FROM belief_summaries").fetchone()[0]
            self.assertEqual(count, 1)

    def test_react_includes_summaries_in_prompt(self):
        # Setup: add turns and a manual summary
        self._add_turns(5)
        with open_db(self.db_file) as conn:
            conn.execute(
                "INSERT INTO belief_summaries (id, session_id, summary) VALUES (?, ?, ?)",
                ("id1", self.session_id, "User: Likes cake"),
            )

        session_ctx = SessionContext(self.session_id, self.db_file)
        prompt_block = session_ctx.get_context_block()

        self.assertIn("Summary of earlier conversation:", prompt_block)
        self.assertIn("User: Likes cake", prompt_block)

    def test_graceful_degradation_on_llm_failure(self):
        self._add_turns(50)
        self.mock_model.generate.side_effect = Exception("LLM Down")

        result = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("LLM Down", result["message"])

    def test_turn_range_recorded_correctly(self):
        self._add_turns(50)
        self.mock_model.generate.return_value = "Summary"

        compress_session_turns(self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20)

        with open_db(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT turn_range FROM belief_summaries").fetchone()
            self.assertEqual(row["turn_range"], "1-20")

    def test_multiple_summaries_per_session(self):
        # 1. First compression at 50 turns (1-20)
        self._add_turns(50)
        self.mock_model.generate.return_value = "Summary 1"
        res1 = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["turn_range"], "1-20")

        # 2. Add more turns to hit next batch (21-40)
        # To trigger 21-40, we need total_turns >= 40 + (50-20) = 70
        self._add_turns(20, offset=50)  # Total 70
        self.mock_model.generate.return_value = "Summary 2"
        res2 = compress_session_turns(
            self.db_file, self.session_id, self.mock_model, turn_threshold=50, compression_batch=20
        )
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["turn_range"], "21-40")

        with open_db(self.db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM belief_summaries WHERE session_id = ?", (self.session_id,)
            ).fetchone()[0]
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
