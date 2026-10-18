"""Synthetic callbacks prove that history and AI failures never send messages."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from panel.app import PanelState, init_db, settings_page


class Client:
    def __init__(self):
        self.states = {"default": "WORKING", "sales": "WORKING"}
        self.sent = []

    def get_sessions(self):
        return [{"name": name, "status": status} for name, status in self.states.items()]

    def get_messages(self, *args, **kwargs):
        return []

    def send_text(self, session, chat, text):
        self.sent.append((session, chat, text))
        return {"id": "sent-" + str(len(self.sent))}


class AutoReplyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.0
        self.clock_patch = patch("panel.app.time.time", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.database = Path(self.temp.name) / "policy.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = Client()
        self.state = self.make_state()
        self.now += 10
        self.state.save_settings({"auto_reply_all_day": True})
        self.now += 2

    def make_state(self):
        return PanelState(self.database, self.client, "synthetic-key", sleep_fn=lambda _: None,
                          uniform_fn=lambda *_: 0)

    def event(self, timestamp="new", session="default", message_id="incoming-1"):
        payload = {"id": message_id, "from": "synthetic@c.us", "body": "请报价", "type": "chat"}
        if timestamp is not None:
            payload["timestamp"] = self.now - 1 if timestamp == "new" else timestamp
        return json.dumps({"event": "message", "session": session, "payload": payload}).encode()

    def reply(self, body=None):
        with patch.object(self.state, "_ai_reply", return_value=("有效回复", "ai")):
            return self.state.handle_webhook(body or self.event())

    def status(self, status):
        self.client.states["default"] = status
        return self.state.handle_webhook(json.dumps({"event": "session.status", "session": "default",
            "timestamp": self.now * 1000, "payload": {"status": status}}).encode())

    def test_history_is_archived_without_ai_or_hot_activity(self):
        with patch.object(self.state, "_ai_reply", side_effect=AssertionError("history must not call AI")):
            result = self.state.handle_webhook(self.event(self.now - 100))
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.sent, [])
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT created_at FROM conversation_messages WHERE message_id='incoming-1'").fetchone()
            self.assertEqual(row[0], int(self.now - 100))
            self.assertEqual(connection.execute("SELECT count(*) FROM conversation_activity").fetchone()[0], 0)

    def test_new_message_sends_once_and_history_remains_context(self):
        self.reply(self.event(self.now - 100, message_id="history"))
        body = self.event()
        self.assertEqual(self.reply(body)["action"], "replied")
        self.assertEqual(self.reply(body)["action"], "ignored")
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("请报价", self.state._conversation_context("synthetic@c.us"))

    def test_missing_invalid_future_and_boundary_timestamps_never_send(self):
        for number, timestamp in enumerate([None, "bad", True, 0, float("nan"), self.now + 1000, self.now - 2]):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.reply(self.event(timestamp, message_id=str(number)))["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_millisecond_message_timestamp_is_accepted(self):
        self.assertEqual(self.reply(self.event((self.now - 1) * 1000))["action"], "replied")

    def test_reenable_excludes_backlog_but_ordinary_settings_do_not_reset(self):
        self.state.save_settings({"auto_reply_all_day": False, "auto_reply_enabled": False})
        self.now += 10
        backlog = self.event(self.now - 1)
        self.state.save_settings({"auto_reply_enabled": True, "auto_reply_all_day": True})
        self.now += 2
        self.assertEqual(self.reply(backlog)["action"], "ignored")
        new = self.event(message_id="new")
        self.state.save_settings({"persona": "客服", "theme": "night"})
        self.assertEqual(self.reply(new)["action"], "replied")

    def test_process_restart_excludes_backlog(self):
        backlog = self.event()
        self.now += 10
        self.state = self.make_state()
        self.now += 2
        self.assertEqual(self.reply(backlog)["action"], "ignored")
        self.assertEqual(self.reply(self.event(message_id="new"))["action"], "replied")

    def test_reconnect_resets_only_affected_session_and_duplicate_ready_does_not_reset(self):
        self.state.save_settings({"auto_reply_all_day": True}, "sales")
        self.now += 2
        sales = self.event(session="sales")
        self.status("STOPPED")
        self.now += 10
        self.status("WORKING")
        self.now += 2
        self.assertEqual(self.reply(sales)["action"], "replied")
        self.assertEqual(self.reply(self.event(self.now - 10))["action"], "ignored")
        new = self.event(message_id="after-reconnect")
        self.status("WORKING")
        self.assertEqual(self.reply(new)["action"], "replied")

    def test_disable_or_new_epoch_during_ai_cancels_send(self):
        for reenable in [False, True]:
            with self.subTest(reenable=reenable):
                self.state.save_settings({"auto_reply_all_day": True})
                self.now += 2
                def generate(*args, **kwargs):
                    self.state.save_settings({"auto_reply_all_day": False, "auto_reply_enabled": False})
                    if reenable:
                        self.state.save_settings({"auto_reply_all_day": True})
                    return "不应发送", "ai"
                with patch.object(self.state, "_ai_reply", side_effect=generate):
                    result = self.state.handle_webhook(self.event(message_id=str(reenable)))
                self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_disconnect_during_wait_cancels_send(self):
        self.state.sleep_fn = lambda _: self.client.states.update(default="STOPPED")
        self.assertEqual(self.reply()["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_ai_missing_configuration_never_uses_saved_fixed_text(self):
        self.state.save_settings({"default_reply_text": "旧固定文案"})
        self.assertEqual(self.state.handle_webhook(self.event())["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_ai_invalid_empty_error_and_tool_responses_never_send(self):
        self.state.save_settings({"ai_base_url": "https://ai.invalid/v1", "ai_model": "test", "ai_api_key": "synthetic"})
        responses = [{"choices": [{"message": {"content": " "}}]},
                     {"choices": []}, {"choices": [{"message": {"content": {"bad": "value"}}}]},
                     {"choices": [{"message": {"content": "partial", "tool_calls": [{"function": {"name": "unknown"}}]}}]},
                     {"choices": [{"message": {"content": None, "tool_calls": [{"function": {"name": "query_order", "arguments": '{"query_type":"order_status"}'}}]}}]},
                     {"choices": [{"finish_reason": "length", "message": {"content": "未完成的回复"}}]},
                     {"choices": [{"message": {"content": "部分内容", "refusal": "unsupported"}}]}]
        for number, response in enumerate(responses):
            with self.subTest(response=response), patch("panel.app.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                self.assertEqual(self.state.handle_webhook(self.event(message_id=str(number)))["action"], "ignored")
        with patch("panel.app.urlopen", side_effect=TimeoutError("synthetic timeout")):
            self.assertEqual(self.state.handle_webhook(self.event(message_id="timeout"))["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_schedule_does_not_replay_messages_before_current_window(self):
        from datetime import datetime
        from panel.app import SHANGHAI_TZ, WEEKDAYS
        current = datetime.fromtimestamp(self.now, SHANGHAI_TZ)
        start = current.replace(second=0, microsecond=0)
        self.state.save_settings({"auto_reply_all_day": False, "auto_reply_enabled": True,
            "weekly_reply_windows": {WEEKDAYS[current.weekday()]: [{"start": start.strftime("%H:%M"), "end": "23:59"}]}})
        self.now += 2
        self.assertEqual(self.reply(self.event(start.timestamp() - 1))["action"], "ignored")
        self.assertEqual(self.reply(self.event(message_id="scheduled-new"))["action"], "replied")

    def test_stale_ready_event_does_not_reopen_disconnected_session(self):
        self.status("STOPPED")
        self.now += 10
        self.state.handle_webhook(json.dumps({"event":"session.status","session":"default", "timestamp":(self.now-20)*1000,
            "payload":{"status":"WORKING"}}).encode())
        self.assertEqual(self.reply()["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_same_second_disconnect_reconnect_cancels_inflight_reply(self):
        self.now += .4
        def generate(*args, **kwargs):
            for status in ("STOPPED", "WORKING"):
                self.client.states["default"] = status
                self.state.handle_webhook(json.dumps({"event":"session.status","session":"default",
                    "timestamp":int(self.now),"payload":{"status":status}}).encode())
            return "旧周期结果", "ai"
        with patch.object(self.state, "_ai_reply", side_effect=generate):
            self.assertEqual(self.state.handle_webhook(self.event())["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_delayed_poll_does_not_overwrite_newer_ready_event(self):
        self.status("STOPPED")
        def get_old_snapshot():
            snapshot = [{"name":"default","status":"STOPPED"}, {"name":"sales","status":"WORKING"}]
            self.now += 2
            self.status("WORKING")
            return snapshot
        with patch.object(self.client, "get_sessions", side_effect=get_old_snapshot):
            self.state.sessions_payload()
        self.now += 2
        self.assertEqual(self.reply()["action"], "replied")

    def test_manual_order_tool_failure_never_sends_waiting_text(self):
        from dataclasses import replace
        self.state.commerce_config = replace(self.state.commerce_config, enabled=True)
        with patch.object(self.state, "_ai_reply", return_value=('{"query_type":"order_status"}',"order_tool")), \
             patch.object(self.state.commerce.woocommerce, "list_orders_by_billing_phone", return_value=[]), \
             patch.object(self.state.commerce.wordpress, "find_orders_by_phone", return_value=[]):
            self.assertEqual(self.state.handle_webhook(self.event())["action"], "ignored")
        self.assertEqual(self.client.sent, [])

    def test_takeover_then_resume_during_ai_cancels_old_reply(self):
        from cryptography.fernet import Fernet
        self.state.enable_chat_services(Fernet.generate_key().decode())
        chat_ref = self.state.chat._encode_chat("default", "synthetic@c.us")
        def generate(*args, **kwargs):
            self.state.chat.takeover("default", chat_ref)
            self.state.chat.resume_ai("default", chat_ref)
            return "接管前结果", "ai"
        with patch.object(self.state, "_ai_reply", side_effect=generate):
            self.assertEqual(self.state.handle_webhook(self.event())["action"], "ignored")
        self.assertEqual(self.client.sent, [])
        self.now += 2
        self.assertEqual(self.reply(self.event(message_id="after-resume"))["action"], "replied")

    def test_fixed_reply_field_is_absent_and_empty_value_stays_empty_after_init(self):
        self.assertNotIn('id="defaultText"', settings_page())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE settings SET value='' WHERE key='default_reply_text'")
        init_db(self.database, seed_business=False)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key='default_reply_text'").fetchone()
            self.assertTrue(row is None or row[0] == "")
