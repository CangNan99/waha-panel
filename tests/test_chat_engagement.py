import sqlite3
import tempfile
import unittest
from pathlib import Path

from panel.app import init_db
from panel.chat_service import ChatService


class _Clock:
    def __init__(self, value=1005):
        self.value = value
    def __call__(self):
        return self.value


class _Codec:
    def __init__(self): self.values = {}; self.cipher = self
    def encode(self, kind, session, value, _ttl):
        token = f"{kind}:{session}:{len(self.values)}"; self.values[token] = value; return token
    def decode(self, token, kind, session):
        return self.values[token]
    def encrypt_json(self, value): return "ENC:" + str(value)
    def decrypt_json(self, token): return {"summary": "第二份"}


class _Client:
    def __init__(self): self.calls = 0; self.mode = "ok"
    def get_chats(self, *_args):
        return [{"id": "chat-1@c.us", "name": "客户"}]
    def get_messages(self, _session, _chat_id, _limit, _offset, _before=None, download_media=False):
        return [{"id": "m1", "timestamp": 1000, "fromMe": False, "body": "hello"}]
    def send_text(self, *_args):
        self.calls += 1
        if self.mode == "unknown": raise TimeoutError("timeout")
        if self.mode == "failed": raise ValueError("failed")
        return {"id": "wamid.manual"}


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


class ChatServiceEngagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.clock = _Clock()
        self.client = _Client()
        self.service = ChatService(self.database, self.client, _Codec(), b"s" * 32, clock=self.clock)
        self.item = self.service.overview("default")["items"][0]

    def tearDown(self):
        self.temp.cleanup()

    def test_takeover_without_manual_message_uses_click_time(self):
        state = self.service.takeover("default", self.item["chat_ref"])
        self.assertEqual(state["auto_resume_at"], 1_005 + 18_000)

    def test_successful_manual_send_moves_auto_resume_from_last_manual_send(self):
        result = self.service.send_text("default", self.item["chat_ref"], "人工回复", "00000000-0000-0000-0000-000000000001")
        self.assertEqual(result["state"], "SENT")
        state = self.service.takeover_state("default", self.item["chat_ref"])
        self.assertEqual(state["last_manual_sent_at"], 1_005)
        self.assertEqual(state["auto_resume_at"], 19_005)

    def test_summary_upsert_replaces_current_report_but_preserves_manual_labels(self):
        self.service.add_manual_label("default", self.item["chat_ref"], "VIP")
        self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "第一份"}, ["高意向"])
        self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "第二份"}, ["待报价"])
        self.assertEqual(self.service.current_summary("default", self.item["chat_ref"])["summary"], "第二份")
        self.assertEqual(self.service.customer_labels("default", self.item["chat_ref"])["manual"], ["VIP"])
        self.assertEqual(self.service.customer_labels("default", self.item["chat_ref"])["ai"], ["待报价"])

    def test_automated_send_repeated_uuid_calls_waha_once(self):
        first = self.service.send_automated_text("default", "chat-1@c.us", "自动", "00000000-0000-0000-0000-000000000002")
        second = self.service.send_automated_text("default", "chat-1@c.us", "自动", "00000000-0000-0000-0000-000000000002")
        self.assertEqual(first, second); self.assertEqual(self.client.calls, 1)

    def test_automated_unknown_is_stored_without_auto_retry(self):
        self.client.mode = "unknown"
        with self.assertRaises(Exception): self.service.send_automated_text("default", "chat-1@c.us", "自动", "00000000-0000-0000-0000-000000000003")
        with self.assertRaises(Exception): self.service.send_automated_text("default", "chat-1@c.us", "自动", "00000000-0000-0000-0000-000000000003")
        self.assertEqual(self.client.calls, 1)

    def test_summary_requires_encrypting_cipher(self):
        self.service.codec.cipher = None
        with self.assertRaises(Exception): self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "x"}, [])
