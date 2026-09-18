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
    def __init__(self): self.values = {}; self.encrypted = {}; self.cipher = self
    def encode(self, kind, session, value, _ttl):
        token = f"{kind}:{session}:{len(self.values)}"; self.values[token] = value; return token
    def decode(self, token, kind, session):
        if not str(token).startswith(f"{kind}:{session}:"):
            raise ValueError("invalid reference")
        return self.values[token]
    def encrypt_json(self, value):
        token = f"ENC:{len(self.encrypted)}"; self.encrypted[token] = dict(value); return token
    def decrypt_json(self, token): return self.encrypted[token]


class _Client:
    def __init__(self):
        self.calls = 0; self.mode = "ok"
        self.messages = [{"id": "m1", "timestamp": 1000, "fromMe": False, "body": "hello"}]
    def get_chats(self, *_args):
        return [{"id": "chat-1@c.us", "name": "客户"}]
    def get_messages(self, _session, _chat_id, _limit, _offset, _before=None, download_media=False):
        offset = int(_offset or 0)
        return self.messages[offset:offset + int(_limit)]
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
            "follow_up_tasks", "customer_labels", "conversation_summaries", "automated_send_requests"
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

    def test_automated_failed_is_stored_without_auto_retry(self):
        self.client.mode = "failed"
        with self.assertRaises(Exception): self.service.send_automated_text("default", "chat-1@c.us", "失败", "00000000-0000-0000-0000-000000000007")
        with self.assertRaises(Exception): self.service.send_automated_text("default", "chat-1@c.us", "失败", "00000000-0000-0000-0000-000000000007")
        self.assertEqual(self.client.calls, 1)

    def test_automated_request_id_conflict_is_rejected(self):
        request_id = "00000000-0000-0000-0000-000000000008"
        self.service.send_automated_text("default", "chat-1@c.us", "first", request_id)
        with self.assertRaises(Exception): self.service.send_automated_text("default", "chat-1@c.us", "second", request_id)

    def test_summary_requires_encrypting_cipher(self):
        self.service.codec.cipher = None
        with self.assertRaises(Exception): self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "x"}, [])
        connection = sqlite3.connect(self.database)
        self.assertIsNone(connection.execute("SELECT summary_ciphertext FROM conversation_summaries").fetchone())
        connection.close()

    def test_summary_ciphertext_is_not_readable_json(self):
        self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "secret"}, [])
        connection = sqlite3.connect(self.database)
        ciphertext = connection.execute("SELECT summary_ciphertext FROM conversation_summaries").fetchone()[0]
        connection.close()
        self.assertNotEqual(ciphertext, '{"summary":"secret"}')
        self.assertNotIn("secret", ciphertext)

    def test_history_is_capped_and_sorted_without_chat_reference(self):
        self.client.messages = [
            {"id": "b", "timestamp": 3, "fromMe": True, "body": "b"},
            {"id": "a", "timestamp": 2, "fromMe": False, "body": "a"},
            {"id": "c", "timestamp": 2, "fromMe": True, "body": "c"},
        ]
        history = self.service.history_for_id("default", "chat-1@c.us", limit=3)
        self.assertEqual([item["message_id"] for item in history], ["a", "c", "b"])
        self.client.messages = [{"id": str(i), "timestamp": i, "fromMe": False, "body": "x"} for i in range(25)]
        self.assertEqual(len(self.service.history_for_id("default", "chat-1@c.us", limit=100)), 20)
        self.assertTrue(all("chat_ref" not in item for item in history))

    def test_chat_reference_is_session_bound(self):
        with self.assertRaises(Exception): self.service.chat_identity("sales", self.item["chat_ref"])

    def test_inbound_activity_fast_path_and_waha_fallback(self):
        connection = sqlite3.connect(self.database)
        connection.execute("INSERT INTO conversation_activity(session_name,chat_id,last_incoming_at) VALUES ('default','chat-1@c.us',2000)")
        connection.commit(); connection.close()
        self.assertTrue(self.service.has_inbound_since("default", "chat-1@c.us", 1000))
        connection = sqlite3.connect(self.database)
        connection.execute("DELETE FROM conversation_activity")
        connection.commit(); connection.close()
        self.client.messages = [
            {"id": "new", "timestamp": 1010, "fromMe": False, "body": "new"},
            {"id": "old", "timestamp": 1000, "fromMe": False, "body": "old"},
        ]
        self.assertTrue(self.service.has_inbound_since("default", "chat-1@c.us", 1005))

    def test_automated_send_does_not_touch_manual_or_takeover_tables(self):
        result = self.service.send_automated_text("default", "chat-1@c.us", "自动", "00000000-0000-0000-0000-000000000004")
        self.assertEqual(result["state"], "SENT")
        connection = sqlite3.connect(self.database)
        self.assertIsNone(connection.execute("SELECT 1 FROM manual_send_requests").fetchone())
        self.assertIsNone(connection.execute("SELECT 1 FROM chat_takeovers").fetchone())
        connection.close()

    def test_failed_and_unknown_manual_sends_do_not_move_timer(self):
        self.service.takeover("default", self.item["chat_ref"])
        self.client.mode = "failed"
        with self.assertRaises(Exception): self.service.send_text("default", self.item["chat_ref"], "失败", "00000000-0000-0000-0000-000000000005")
        state = self.service.takeover_state("default", self.item["chat_ref"])
        self.assertIsNone(state["last_manual_sent_at"])
        self.assertEqual(state["auto_resume_at"], 19005)
        self.client.mode = "unknown"
        with self.assertRaises(Exception): self.service.send_text("default", self.item["chat_ref"], "未知", "00000000-0000-0000-0000-000000000006")
        state = self.service.takeover_state("default", self.item["chat_ref"])
        self.assertIsNone(state["last_manual_sent_at"])
        self.assertEqual(state["auto_resume_at"], 19005)

    def test_manual_labels_reject_ai_source_and_overlong_values(self):
        with self.assertRaises(Exception): self.service.add_manual_label("default", self.item["chat_ref"], "VIP", source="AI")
        with self.assertRaises(Exception): self.service.add_manual_label("default", self.item["chat_ref"], "x" * 101)
        with self.assertRaises(Exception): self.service.update_manual_label("default", self.item["chat_ref"], "AI", "VIP", source="AI")

    def test_summary_and_labels_roll_back_together(self):
        self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "before"}, ["old-ai"])
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TRIGGER fail_ai_insert BEFORE INSERT ON customer_labels WHEN NEW.source='AI' BEGIN SELECT RAISE(ABORT, 'boom'); END")
        connection.commit(); connection.close()
        with self.assertRaises(Exception): self.service.save_summary_and_ai_labels("default", self.item["chat_ref"], {"summary": "after"}, ["new-ai"])
        self.assertEqual(self.service.current_summary("default", self.item["chat_ref"])["summary"], "before")
        self.assertEqual(self.service.customer_labels("default", self.item["chat_ref"])["ai"], ["old-ai"])
