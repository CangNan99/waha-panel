import sqlite3
import tempfile
import unittest
from pathlib import Path

from panel.app import init_db


class EngagementSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_engagement_schema_is_created_and_idempotent(self):
        init_db(self.database, seed_business=False)
        init_db(self.database, seed_business=False)
        connection = sqlite3.connect(self.database)
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        takeover_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(chat_takeovers)")
        }
        connection.close()
        self.assertTrue({
            "follow_up_tasks", "customer_labels", "conversation_summaries"
        } <= tables)
        self.assertTrue({"last_manual_sent_at", "auto_resume_at"} <= takeover_columns)
