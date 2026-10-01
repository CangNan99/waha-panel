import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from panel.app import init_db
from panel.chat_security import DataCipher, chat_key_hmac
from panel.customer_memory_service import CustomerMemoryService


class CustomerMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.cipher = DataCipher(Fernet.generate_key().decode("ascii"))
        self.hmac_secret = b"memory-test-secret-0123456789"
        self.enabled = True
        self.calls = []

        def settings_loader(_session_name):
            return {
                "customer_memory_enabled": self.enabled,
                "ai_model": "memory-test-model",
            }

        def completion_fn(settings, system_prompt, user_prompt):
            self.calls.append((settings, system_prompt, user_prompt))
            return {
                "profile": "VIP 客户",
                "preferences": ["偏好中文说明"],
                "customer_intent": "准备下单",
                "confirmed_items": ["需要人工确认库存"],
                "unresolved_items": ["等待库存结果"],
                "manual_reply_style": "先确认需求，再给出下一步",
            }

        self.service = CustomerMemoryService(
            self.database,
            self.cipher,
            self.hmac_secret,
            settings_loader,
            completion_fn,
            clock=lambda: 100,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN")
            connection.executemany(
                "INSERT INTO conversation_messages "
                "(session_name,chat_id,direction,origin,message_id,content,created_at) "
                "VALUES ('default','chat-1@c.us',?,?,?,?,?)",
                [
                    ("inbound", "unknown", "in-1", "我想购买大号", 1),
                    ("outbound", "manual", "out-1", "我先帮您确认库存", 2),
                    ("outbound", "auto_ai", "out-2", "稍后给您准确答复", 3),
                ],
            )
            connection.commit()

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def test_rebuild_includes_manual_and_ai_messages_and_returns_context(self):
        result = self.service.rebuild("default", "chat-1@c.us")

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["version"], 1)
        self.assertEqual(len(self.calls), 1)
        prompt = self.calls[0][2]
        self.assertIn("我先帮您确认库存", prompt)
        self.assertIn("稍后给您准确答复", prompt)
        context = self.service.context_for_reply("default", "chat-1@c.us")
        self.assertIn("VIP 客户", context)
        self.assertIn("先确认需求", context)

    def test_enqueue_is_deduplicated_per_customer(self):
        self.service.enqueue("default", "chat-1@c.us")
        self.service.enqueue("default", "chat-1@c.us")

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT state,chat_id_ciphertext FROM conversation_memory_jobs "
                "WHERE session_name='default' AND chat_key_hmac=?",
                (chat_key_hmac(self.hmac_secret, "default", "chat-1@c.us"),),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "PENDING")
        self.assertTrue(rows[0][1])

    def test_disabled_memory_does_not_enqueue_or_inject_context(self):
        self.enabled = False
        self.service.enqueue("default", "chat-1@c.us")

        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM conversation_memory_jobs").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.service.context_for_reply("default", "chat-1@c.us"), "")

    def test_failed_rebuild_keeps_previous_memory_and_marks_failure(self):
        self.service.rebuild("default", "chat-1@c.us")

        def failing_completion(_settings, _system_prompt, _user_prompt):
            raise RuntimeError("upstream unavailable")

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO conversation_messages "
                "(session_name,chat_id,direction,origin,message_id,content,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("default", "chat-1@c.us", "inbound", "unknown", "in-2", "还有库存吗", 4),
            )
            connection.commit()
        self.service.completion_fn = failing_completion
        result = self.service.rebuild("default", "chat-1@c.us")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("VIP 客户", self.service.context_for_reply("default", "chat-1@c.us"))
        with closing(sqlite3.connect(self.database)) as connection:
            status, error_code = connection.execute(
                "SELECT status,last_error_code FROM customer_memories "
                "WHERE session_name='default' AND chat_key_hmac=?",
                (chat_key_hmac(self.hmac_secret, "default", "chat-1@c.us"),),
            ).fetchone()
        self.assertEqual(status, "FAILED")
        self.assertEqual(error_code, "MEMORY_GENERATION_FAILED")

    def test_clear_removes_memory_and_job_but_preserves_messages(self):
        self.service.rebuild("default", "chat-1@c.us")
        self.service.enqueue("default", "chat-1@c.us")

        result = self.service.clear("default", "chat-1@c.us")

        self.assertTrue(result["cleared"])
        with closing(sqlite3.connect(self.database)) as connection:
            memory_count = connection.execute("SELECT COUNT(*) FROM customer_memories").fetchone()[0]
            job_count = connection.execute("SELECT COUNT(*) FROM conversation_memory_jobs").fetchone()[0]
            message_count = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        self.assertEqual(memory_count, 0)
        self.assertEqual(job_count, 0)
        self.assertEqual(message_count, 3)


if __name__ == "__main__":
    unittest.main()
