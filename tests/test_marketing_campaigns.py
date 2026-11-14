import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from panel.app import init_db
from panel.marketing_automation import MarketingAutomationError, MarketingAutomationService


DAY = 24 * 60 * 60


class _FakeChat:
    def __init__(self):
        self.sent = []
        self.human = set()
        self.opted_out = set()
        self.recent = set()

    def marketing_candidates(self, session, now, idle_days, limit=200):
        return [{
            "chat_id": "customer-1@c.us",
            "chat_key_hmac": "customer-key-1",
            "last_inbound_at": now - max(int(idle_days), 7) * DAY,
            "last_outbound_at": now - max(int(idle_days), 8) * DAY,
            "history": [{"timestamp": now - max(int(idle_days), 7) * DAY, "from_me": False, "body": "想了解产品"}],
        }]

    def is_human_takeover(self, session, chat_id):
        return chat_id in self.human

    def is_marketing_opted_out(self, session, chat_id, history=None):
        return chat_id in self.opted_out

    def has_inbound_since(self, session, chat_id, since_timestamp):
        return chat_id in self.recent

    def send_automated_text(self, session, chat_id, text, client_request_id):
        self.sent.append((session, chat_id, text, client_request_id))
        return {"request_id": client_request_id, "state": "SENT", "kind": "text"}


class MarketingCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.clock = [1_700_000_000]
        self.chat = _FakeChat()
        self.settings = {
            "ai_marketing_enabled": "1",
            "ai_marketing_idle_days": "7",
            "ai_marketing_daily_limit": "20",
            "ai_marketing_scan_interval_minutes": "5",
            "ai_marketing_cooldown_days": "30",
            "ai_marketing_prompt": "提醒客户了解新品",
        }
        self.service = MarketingAutomationService(
            self.database,
            self.chat,
            settings_loader=lambda _session: dict(self.settings),
            sessions_loader=lambda: ["default"],
            message_generator=lambda *_args: ("您好，需要我介绍一下新品吗？", "ai"),
            clock=lambda: self.clock[0],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_campaign_tables_are_additive_and_idempotent(self):
        init_db(self.database, seed_business=False)
        init_db(self.database, seed_business=False)
        connection = sqlite3.connect(self.database)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            state_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(marketing_contact_states)"
            )}
            event_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(marketing_events)"
            )}
        finally:
            connection.close()
        self.assertTrue({"marketing_campaigns", "marketing_contact_states", "marketing_events"} <= tables)
        self.assertIn("chat_key_hmac", state_columns)
        self.assertNotIn("chat_id", state_columns)
        self.assertIn("chat_key_hmac", event_columns)
        self.assertNotIn("chat_id", event_columns)

    def test_campaign_validation_and_round_trip(self):
        campaign = self.service.create_campaign({
            "name": "沉睡客户唤醒",
            "objective": "介绍新品并邀请客户咨询",
            "target_rules": {"idle_days": 7, "sessions": ["default"], "include_labels": ["有兴趣"]},
            "product_scope": ["product-1", "product-2"],
            "allow_image_send": True,
            "cadence": {"first_delay_hours": 0, "follow_up_hours": 24},
            "max_touches": 3,
            "quiet_hours": {"start": "22:00", "end": "08:00"},
            "language_policy": "customer",
        })
        self.assertRegex(campaign["campaign_id"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(campaign["max_touches"], 3)
        self.assertEqual(campaign["target_rules"]["idle_days"], 7)
        self.assertTrue(campaign["active"])
        self.assertEqual(self.service.list_campaigns()["total"], 1)
        self.assertEqual(self.service.update_campaign(campaign["campaign_id"], {"max_touches": 5})["max_touches"], 5)
        paused = self.service.pause_campaign(campaign["campaign_id"], "人工调整")
        self.assertFalse(paused["active"])
        self.assertEqual(paused["paused_reason"], "人工调整")
        resumed = self.service.resume_campaign(campaign["campaign_id"])
        self.assertTrue(resumed["active"])
        with self.assertRaises(MarketingAutomationError):
            self.service.create_campaign({"name": "", "objective": "x"})
        with self.assertRaises(MarketingAutomationError):
            self.service.create_campaign({"name": "x", "objective": "x", "max_touches": 0})
        with self.assertRaises(MarketingAutomationError):
            self.service.create_campaign({"name": "x", "objective": "x", "quiet_hours": {"start": "bad"}})
        with self.assertRaises(MarketingAutomationError):
            self.service.create_campaign({"name": "x", "objective": "x", "target_rules": {"chat_ids": ["raw"]}})

    def test_contact_state_transitions_are_persisted_and_terminal_guards_apply(self):
        campaign = self.service.create_campaign({"name": "跟进", "objective": "跟进", "max_touches": 2})
        base = {"campaign_id": campaign["campaign_id"], "session_name": "default", "chat_key_hmac": "hmac-1"}
        state = self.service.transition_contact(base, "eligible", self.clock[0])
        self.assertEqual(state["stage"], "ELIGIBLE")
        state = self.service.transition_contact(state, "contacted", self.clock[0])
        self.assertEqual((state["stage"], state["touch_count"]), ("CONTACTED", 1))
        state = self.service.transition_contact(state, "follow_up_due", self.clock[0] + DAY)
        self.assertEqual(state["stage"], "FOLLOW_UP_DUE")
        state = self.service.transition_contact(state, "contacted", self.clock[0] + DAY)
        self.assertEqual((state["stage"], state["touch_count"]), ("CONTACTED", 2))
        state = self.service.transition_contact(state, "max_touches", self.clock[0] + 2 * DAY)
        self.assertEqual(state["stage"], "COMPLETED")
        unchanged = self.service.transition_contact(state, "contacted", self.clock[0] + 3 * DAY)
        self.assertEqual(unchanged["stage"], "COMPLETED")
        persisted = self.service.contact_state("default", "hmac-1", campaign["campaign_id"])
        self.assertEqual(persisted["stage"], "COMPLETED")

    def test_reply_opt_out_and_human_takeover_stop_campaign_contact(self):
        campaign = self.service.create_campaign({"name": "停止条件", "objective": "跟进", "max_touches": 3})
        base = {"campaign_id": campaign["campaign_id"], "session_name": "default", "chat_key_hmac": "hmac-2"}
        state = self.service.transition_contact(base, "contacted", self.clock[0])
        state = self.service.transition_contact(state, "customer_reply", self.clock[0] + 10)
        self.assertEqual(state["stage"], "REPLIED")
        self.assertIsNone(state["next_action_at"])
        state = self.service.transition_contact(state, "qualified", self.clock[0] + 20)
        self.assertEqual(state["stage"], "QUALIFIED")
        state = self.service.transition_contact(state, "human_takeover", self.clock[0] + 30)
        self.assertEqual(state["stage"], "HUMAN_HANDOFF")

        optout = {"campaign_id": campaign["campaign_id"], "session_name": "default", "chat_key_hmac": "hmac-3"}
        optout = self.service.transition_contact(optout, "eligible", self.clock[0])
        optout = self.service.transition_contact(optout, "opt_out", self.clock[0] + 1)
        self.assertTrue(optout["opt_out"])
        self.assertEqual(optout["stage"], "OPTED_OUT")
        self.assertEqual(self.service.transition_contact(optout, "contacted", self.clock[0] + 2)["stage"], "OPTED_OUT")

    def test_active_campaign_uses_per_contact_state_and_legacy_path_without_campaign(self):
        # Legacy mode remains unchanged when no campaign exists.
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.assertEqual(len(self.chat.sent), 1)
        campaign = self.service.create_campaign({
            "name": "活动模式",
            "objective": "活动跟进",
            "target_rules": {"idle_days": 7},
            "cadence": {"first_delay_hours": 0, "follow_up_hours": 1},
            "max_touches": 1,
        })
        self.service._last_scan.clear()
        self.clock[0] += 31 * DAY
        # An active campaign takes over from the legacy path and records its
        # first touch; the next scan is suppressed after max_touches.
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.service._last_scan.clear()
        self.clock[0] += 2 * DAY
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        state = self.service.contact_state("default", "customer-key-1", campaign["campaign_id"])
        self.assertIsNotNone(state)
        self.assertEqual(state["stage"], "COMPLETED")

    def test_campaign_follow_up_uses_a_distinct_delivery_key_per_touch(self):
        campaign = self.service.create_campaign({
            "name": "多次跟进",
            "objective": "跟进",
            "cadence": {"first_delay_hours": 0, "follow_up_hours": 1},
            "max_touches": 3,
        })
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        self.service._last_scan.clear()
        self.clock[0] += 2 * 60 * 60
        self.assertEqual(self.service.run_once(self.clock[0]), 1)
        state = self.service.contact_state("default", "customer-key-1", campaign["campaign_id"])
        self.assertEqual((state["stage"], state["touch_count"]), ("CONTACTED", 2))
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT campaign_key,state FROM ai_marketing_send_requests "
                "WHERE session_name=? AND chat_key_hmac=? ORDER BY id",
                ("default", "customer-key-1"),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[1] == "SENT" for row in rows))
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_quiet_hours_and_daily_limit_skip_campaign_contacts(self):
        campaign = self.service.create_campaign({
            "name": "安静时段",
            "objective": "跟进",
            "quiet_hours": {"start": "00:00", "end": "23:59"},
            "max_touches": 2,
        })
        self.settings["ai_marketing_daily_limit"] = "0"
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        state = self.service.contact_state("default", "customer-key-1", campaign["campaign_id"])
        self.assertIsNone(state)

    def test_first_delay_defers_initial_campaign_touch(self):
        campaign = self.service.create_campaign({
            "name": "延迟触达",
            "objective": "跟进",
            "cadence": {"first_delay_hours": 2, "follow_up_hours": 24},
            "max_touches": 2,
        })
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 0)
        state = self.service.contact_state("default", "customer-key-1", campaign["campaign_id"])
        self.assertEqual(state["stage"], "ELIGIBLE")
        self.clock[0] += 2 * 60 * 60
        self.service._last_scan.clear()
        self.assertEqual(self.service.run_once(self.clock[0]), 1)

    def test_customer_message_updates_existing_contact_without_storing_raw_chat_id(self):
        campaign = self.service.create_campaign({"name": "回复停止", "objective": "跟进"})
        state = self.service.transition_contact({
            "campaign_id": campaign["campaign_id"],
            "session_name": "default",
            "chat_key_hmac": "hmac-4",
        }, "contacted", self.clock[0])

        class _HashChat(_FakeChat):
            @staticmethod
            def _chat_key(session, chat_id):
                return "hmac-4"

            def marketing_stop_reason(self, session, chat_id, since_timestamp=0, history=None):
                return "CUSTOMER_REPLIED"

        self.service.chat_service = _HashChat()
        self.assertEqual(self.service.record_customer_message("default", "raw@c.us", "我想了解", self.clock[0] + 1), 1)
        updated = self.service.contact_state("default", "hmac-4", campaign["campaign_id"])
        self.assertEqual(updated["stage"], "REPLIED")
        connection = sqlite3.connect(self.database)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(marketing_events)")}
            rows = connection.execute("SELECT metadata_json FROM marketing_events").fetchall()
        finally:
            connection.close()
        self.assertNotIn("chat_id", columns)
        self.assertNotIn("raw@c.us", " ".join(row[0] for row in rows))


if __name__ == "__main__":
    unittest.main()
