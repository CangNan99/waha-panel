import sqlite3
import tempfile
import unittest
from pathlib import Path
from cryptography.fernet import Fernet

from panel.app import init_db
from panel.chat_security import DataCipher, ReferenceCodec
from panel.chat_service import ChatService
from panel.chat_service import ChatSendError
from panel.marketing_automation import MarketingAutomationService


DAY = 24 * 60 * 60


class _FakeChat:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.human = set()
        self.opted_out = set()
        self.recent = set()
        self.sent = []
        self.send_error = None

    def marketing_candidates(self, session, now, idle_days, limit=200):
        return list(self.candidates)[:limit]

    def is_human_takeover(self, session, chat_id):
        return chat_id in self.human

    def is_marketing_opted_out(self, session, chat_id, history=None):
        return chat_id in self.opted_out

    def has_inbound_since(self, session, chat_id, since_timestamp):
        return chat_id in self.recent

    def send_automated_text(self, session, chat_id, text, client_request_id):
        self.sent.append((session, chat_id, text, client_request_id))
        if self.send_error is not None:
            raise self.send_error
        return {"request_id": client_request_id, "state": "SENT", "kind": "text"}


class _ListClient:
    def __init__(self, chats, messages):
        self.chats = chats
        self.messages = messages

    def get_chats(self, _session, _limit, offset):
        return self.chats[int(offset):int(offset) + int(_limit)]

    def get_messages(self, _session, chat_id, limit, offset, _before=None, download_media=False):
        rows = self.messages.get(chat_id, [])
        return rows[int(offset):int(offset) + int(limit)]


class MarketingAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.clock = [1_700_000_000]
        self.chat = _FakeChat([
            {
                "chat_id": "customer-1@c.us",
                "chat_key_hmac": "customer-key-1",
                "last_inbound_at": self.clock[0] - 8 * DAY,
                "last_outbound_at": self.clock[0] - 9 * DAY,
                "history": [{"timestamp": self.clock[0] - 8 * DAY, "from_me": False, "body": "想了解套餐"}],
            }
        ])
        self.settings = {
            "ai_marketing_enabled": "1",
            "ai_marketing_idle_days": "7",
            "ai_marketing_daily_limit": "20",
            "ai_marketing_scan_interval_minutes": "5",
            "ai_marketing_cooldown_days": "30",
            "ai_marketing_prompt": "提醒客户查看最新套餐",
        }
        self.generated = []

        def generate(session, chat_id, history, prompt):
            self.generated.append((session, chat_id, history, prompt))
            return "您好，最近有新的套餐，需要我为您介绍吗？", "ai"

        self.service = MarketingAutomationService(
            self.database,
            self.chat,
            settings_loader=lambda _session: dict(self.settings),
            sessions_loader=lambda: ["default"],
            message_generator=generate,
            clock=lambda: self.clock[0],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_present_and_idempotent(self):
        init_db(self.database, seed_business=False)
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(ai_marketing_send_requests)"
                )
            }
        finally:
            connection.close()
        self.assertIn("ai_marketing_send_requests", tables)
        self.assertTrue({"session_name", "chat_key_hmac", "campaign_key", "state", "client_request_id"} <= columns)

    def test_sends_idle_customer_and_does_not_duplicate_same_monthly_batch(self):
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.assertEqual(len(self.chat.sent), 1)
        request_id = self.chat.sent[0][3]
        self.assertTrue(request_id.startswith("ai-marketing:default:"))
        self.clock[0] += 5 * 60
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(len(self.chat.sent), 1)
        self.clock[0] += 15 * DAY
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(len(self.chat.sent), 1)
        self.clock[0] += 31 * DAY
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertNotEqual(self.chat.sent[0][3], self.chat.sent[1][3])

    def test_marketing_is_disabled_by_the_pause_switch(self):
        self.settings["ai_marketing_enabled"] = "0"
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.generated, [])

    def test_skips_group_human_takeover_opt_out_and_recent_inbound(self):
        self.chat.candidates = [
            {"chat_id": "group@g.us", "chat_key_hmac": "g", "last_inbound_at": 1, "history": [{"body": "x"}]},
            {"chat_id": "human@c.us", "chat_key_hmac": "h", "last_inbound_at": 1, "history": [{"body": "x"}]},
            {"chat_id": "optout@c.us", "chat_key_hmac": "o", "last_inbound_at": 1, "history": [{"body": "x"}]},
            {"chat_id": "recent@c.us", "chat_key_hmac": "r", "last_inbound_at": 1, "history": [{"body": "x"}]},
        ]
        self.chat.human.add("human@c.us")
        self.chat.opted_out.add("optout@c.us")
        self.chat.recent.add("recent@c.us")
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.generated, [])

    def test_daily_limit_counts_only_successful_sends(self):
        self.settings["ai_marketing_daily_limit"] = "1"
        self.chat.candidates.append({
            "chat_id": "customer-2@c.us",
            "chat_key_hmac": "customer-key-2",
            "last_inbound_at": self.clock[0] - 9 * DAY,
            "last_outbound_at": 0,
            "history": [{"timestamp": self.clock[0] - 9 * DAY, "from_me": False, "body": "hello"}],
        })
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.assertEqual(len(self.chat.sent), 1)

    def test_empty_or_failed_generation_never_sends(self):
        self.service.message_generator = lambda *_args: ("", "ai_empty")
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(self.chat.sent, [])
        self.service._last_scan.clear()
        self.service.message_generator = lambda *_args: (_ for _ in ()).throw(RuntimeError("provider down"))
        self.clock[0] += 5 * 60
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(self.chat.sent, [])

    def test_failed_send_is_not_counted_and_is_recorded(self):
        self.chat.send_error = ChatSendError("unknown", "UNKNOWN", "WAHA_SEND_UNKNOWN")
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(len(self.chat.sent), 1)
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT state,error_code FROM ai_marketing_send_requests"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("UNKNOWN", "WAHA_SEND_UNKNOWN"))

    def test_reconciles_generic_send_ledger_before_retrying_pending_marketing_row(self):
        candidate = self.chat.candidates[0]
        prompt = self.settings["ai_marketing_prompt"]
        campaign_key = self.service._campaign_key("default", prompt, self.clock[0])
        request_id = self.service._request_id("default", candidate["chat_key_hmac"], campaign_key)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO ai_marketing_send_requests "
                "(session_name,chat_key_hmac,campaign_key,client_request_id,message_hash,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'PENDING',?,?)",
                ("default", candidate["chat_key_hmac"], campaign_key, request_id, "hash", self.clock[0], self.clock[0]),
            )
            connection.execute(
                "INSERT INTO automated_send_requests "
                "(client_request_id,session_name,chat_id,payload_hash,state,waha_message_id,error_code,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (request_id, "default", candidate["chat_id"], "hash", "SENT", "wamid.reconciled", None, self.clock[0]),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        self.assertEqual(self.chat.sent, [])
        connection = sqlite3.connect(self.database)
        try:
            state = connection.execute(
                "SELECT state FROM ai_marketing_send_requests WHERE client_request_id=?", (request_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "SENT")

    def test_chat_service_returns_only_idle_private_chats_and_detects_opt_out(self):
        client = _ListClient(
            [
                {"id": "idle@c.us"},
                {"id": "group@g.us"},
                {"id": "active@c.us"},
                {"id": "outbound-recent@c.us"},
            ],
            {
                "idle@c.us": [
                    {"id": "i1", "timestamp": self.clock[0] - 8 * DAY, "fromMe": False, "body": "套餐"}
                ],
                "group@g.us": [
                    {"id": "g1", "timestamp": self.clock[0] - 8 * DAY, "fromMe": False, "body": "群聊"}
                ],
                "active@c.us": [
                    {"id": "a1", "timestamp": self.clock[0] - 1 * DAY, "fromMe": False, "body": "刚刚咨询"}
                ],
                "outbound-recent@c.us": [
                    {"id": "o1", "timestamp": self.clock[0] - 8 * DAY, "fromMe": False, "body": "旧咨询"},
                    {"id": "o2", "timestamp": self.clock[0] - 1 * DAY, "fromMe": True, "body": "已回复"},
                ],
            },
        )
        cipher = DataCipher(Fernet.generate_key().decode("ascii"))
        chat = ChatService(
            self.database,
            client,
            ReferenceCodec(cipher, clock=lambda: self.clock[0]),
            b"marketing-test-secret-1234567890",
            clock=lambda: self.clock[0],
        )
        candidates = chat.marketing_candidates("default", self.clock[0], 7)
        self.assertEqual([item["chat_id"] for item in candidates], ["idle@c.us"])
        key = chat._chat_key("default", "idle@c.us")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO customer_labels(session_name,chat_key_hmac,source,label,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                ("default", key, "MANUAL", "退订", self.clock[0], self.clock[0]),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(chat.is_marketing_opted_out("default", "idle@c.us", candidates[0]["history"]))


if __name__ == "__main__":
    unittest.main()
