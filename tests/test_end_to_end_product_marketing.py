import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from panel.app import PanelState, init_db


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
DAY = 24 * 60 * 60


class _Client:
    """Deterministic WAHA-shaped adapter; no network is used by this test."""

    def __init__(self):
        self.image_calls = []
        self.text_calls = []

    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]

    def get_chats(self, *_args, **_kwargs):
        return [{"id": "customer@c.us", "name": "客户"}]

    def get_messages(self, *_args, **_kwargs):
        return []

    def send_image(self, session, chat_id, filename, mimetype, data_b64, caption=""):
        self.image_calls.append(
            {
                "session": session,
                "chat_id": chat_id,
                "filename": filename,
                "mimetype": mimetype,
                "data": data_b64,
                "caption": caption,
            }
        )
        return {"id": f"wamid.image.{len(self.image_calls)}"}

    def send_text(self, session, chat_id, text):
        self.text_calls.append({"session": session, "chat_id": chat_id, "text": text})
        return {"id": f"wamid.text.{len(self.text_calls)}"}


class _Profiles:
    def effective_profile(self, *_args):
        return {
            "provider": "glm",
            "model": "fixture",
            "base_url": "https://example.invalid",
            "api_key": "redacted-fixture-key",
        }

    def profile_payload(self, *_args):
        return {}


class EndToEndProductMarketingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.now = int(time.time()) + 10
        self.client = _Client()
        self.state = PanelState(
            self.database,
            self.client,
            "fixture-webhook-key",
            admin_username="admin",
            admin_password="fixture-password",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        # Keep all clocks in the temporary fixture deterministic.  The small
        # future offset keeps the webhook timestamp newer than AutoReplyPolicy's
        # connection epoch while remaining within the accepted clock skew.
        self.state.clock = lambda: self.now
        self.state.product_images.clock = lambda: self.now
        self.state.sleep_fn = lambda *_args: None
        self.state.uniform_fn = lambda *_args: 0
        self.state.save_settings(
            {
                "auto_reply_all_day": True,
                "auto_reply_enabled": True,
                "customer_image_recognition_enabled": True,
                "product_image_auto_send_enabled": True,
                "chat_product_image_enabled": True,
                "marketing_product_image_enabled": True,
                "product_image_shadow_mode": False,
                "product_image_global_kill_switch": False,
                "ai_marketing_enabled": True,
                "ai_marketing_idle_days": 7,
                "ai_marketing_daily_limit": 20,
                "ai_marketing_scan_interval_minutes": 5,
                "ai_marketing_cooldown_days": 30,
                "ai_marketing_prompt": "跟进客户对产品的兴趣",
            },
            "default",
        )
        self.product = self.state.product_catalog.save_product(
            {"product_key": "g5-plus", "product_name": "G5 Plus", "sku": "G5-PLUS"}
        )
        self.asset = self.state.product_images.upload(
            "g5.png",
            "image/png",
            PNG,
            "G5 Plus",
            ["g5-plus"],
            "这是 {product_tag}",
            product_name="G5 Plus",
            sku="G5-PLUS",
            product_id=self.product["product_id"],
        )
        self.state.ai_profiles = _Profiles()
        self.state.glm = object()
        self.state._load_ai_image_content = lambda *_args: {
            "mime_type": "image/png",
            "data": base64.b64encode(PNG).decode("ascii"),
        }

    def tearDown(self):
        self.state.stop_background_services()
        self.temp.cleanup()

    def _image_webhook(self, message_id="customer-image-1"):
        event = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": message_id,
                "from": "customer@c.us",
                "to": "customer@c.us",
                "fromMe": False,
                "body": "请识别这个产品",
                "type": "image",
                "hasMedia": True,
                "media": {"mimetype": "image/png", "data": base64.b64encode(PNG).decode("ascii")},
                "timestamp": self.now + 1,
            },
        }
        return self.state.handle_webhook(json.dumps(event).encode("utf-8"), dispatch=True)

    def _text_webhook(self, message_id="customer-reply-1"):
        event = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": message_id,
                "from": "customer@c.us",
                "to": "customer@c.us",
                "fromMe": False,
                "body": "我还有一个问题，请继续介绍",
                "type": "chat",
                "timestamp": self.now + 2,
            },
        }
        return self.state.handle_webhook(json.dumps(event).encode("utf-8"), dispatch=True)

    def test_customer_image_to_marketing_pause_retry_is_audited_and_idempotent(self):
        """Exercise webhook recognition, marketing delivery and suppression together."""
        decision = {
            "decision": "AUTO_SEND",
            "reason_code": "HIGH_CONFIDENCE_UNIQUE",
            "confidence": 0.98,
            "confidence_bucket": "high",
            "product": {
                "product_id": self.product["product_id"],
                "product_key": "g5-plus",
                "product_name": "G5 Plus",
            },
            "variant": "",
            "intent": "identify",
        }
        self.state.classify_product_image = lambda *_args, **_kwargs: decision
        image_result = self._image_webhook()
        self.assertEqual(image_result["action"], "replied")
        self.assertEqual(image_result["mode"], "product_image")
        self.assertEqual(len(self.client.image_calls), 1)

        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM product_image_match_audits").fetchone()[0],
                1,
            )
            customer_send = connection.execute(
                "SELECT state,tag_key,asset_id FROM product_image_send_requests"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(customer_send, [("SENT", self.product["product_id"], self.asset["asset_id"])])

        # The campaign candidate uses the activity timestamp written by the
        # webhook.  A strict-greater-than reply check therefore does not treat
        # the same image as a later customer reply during this first scan.
        chat_key = self.state.chat._chat_key("default", "customer@c.us")
        candidate = {
            "chat_id": "customer@c.us",
            "chat_key_hmac": chat_key,
            "last_inbound_at": self.now,
            "last_outbound_at": self.now - DAY,
            "history": [
                {"timestamp": self.now, "from_me": False, "body": "我想了解 g5-plus"}
            ],
        }
        self.state.marketing.chat_service.marketing_candidates = (
            lambda _session, _now, _idle_days, limit=200: [candidate][:limit]
        )
        self.state.marketing.message_generator = lambda *_args: {
            "message": "您好，我可以继续为您介绍 G5 Plus。",
            "mode": "ai",
            "product_id": self.product["product_id"],
            "caption": "G5 Plus 产品图片",
        }
        campaign = self.state.marketing.create_campaign(
            {
                "campaign_id": "e2e-product-follow-up",
                "name": "产品图片跟进",
                "objective": "跟进客户对产品的兴趣",
                "allow_image_send": True,
                "product_scope": [self.product["product_id"]],
                "max_touches": 2,
            }
        )
        self.assertTrue(campaign["active"])
        self.assertEqual(self.state.marketing.run_once(self.now), 1)
        self.assertEqual(len(self.client.text_calls), 1)
        self.assertEqual(len(self.client.image_calls), 2)

        connection = sqlite3.connect(self.database)
        try:
            marketing_delivery = connection.execute(
                "SELECT state,text_state,image_state FROM marketing_deliveries"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(marketing_delivery, [("SENT", "SENT", "SENT")])

        # A real follow-up webhook marks the existing contact REPLIED before
        # the normal AI text reply is produced.
        self.state._ai_reply = lambda *_args, **_kwargs: ("好的，我来继续协助您。", "ai")
        reply_result = self._text_webhook()
        self.assertEqual(reply_result["action"], "replied")
        state = self.state.marketing.contact_state("default", chat_key, campaign["campaign_id"])
        self.assertEqual(state["stage"], "REPLIED")

        paused = self.state.marketing.pause_campaign(campaign["campaign_id"], "人工暂停营销")
        self.assertFalse(paused["active"])
        self.state.marketing._last_scan.clear()
        self.assertEqual(self.state.marketing.run_once(self.now + 600), 0)
        # Neither the paused campaign nor a replayed scan may send another
        # text/image pair; the original delivery remains the only ledger row.
        self.assertEqual(len(self.client.text_calls), 2)  # campaign + normal AI reply
        self.assertEqual(len(self.client.image_calls), 2)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM marketing_deliveries").fetchone()[0],
                1,
            )
            events = [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM marketing_events ORDER BY created_at,event_id"
                )
            ]
        finally:
            connection.close()
        self.assertIn("customer_reply", events)

    def test_product_image_migrations_and_global_kill_shadow_routes_are_safe(self):
        # Re-running both bootstrap paths against the same temporary SQLite
        # database and image directory must remain idempotent.
        init_db(self.database, seed_business=False)
        self.state.product_images._ensure_schema()
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("product_image_assets", tables)
        self.assertIn("product_image_match_audits", tables)

        decision = {
            "decision": "AUTO_SEND",
            "reason_code": "HIGH_CONFIDENCE_UNIQUE",
            "confidence": 0.98,
            "confidence_bucket": "high",
            "product": {"product_id": self.product["product_id"], "product_key": "g5-plus"},
            "variant": "",
            "intent": "identify",
        }
        self.state.classify_product_image = lambda *_args, **_kwargs: decision
        self.state.save_settings({"product_image_global_kill_switch": True}, "default")
        blocked = self.state.handle_customer_product_image(
            "default",
            "customer@c.us",
            "kill-image",
            {"type": "image"},
            "请识别这个产品",
            "客户发送图片",
        )
        self.assertEqual(blocked["reason_code"], "RECOGNITION_SWITCH_OFF")
        self.assertEqual(self.state.product_image_tool_schema("default", self.state.settings_payload("default")), [])
        self.assertEqual(len(self.client.image_calls), 0)

        self.state.save_settings(
            {"product_image_global_kill_switch": False, "product_image_shadow_mode": True},
            "default",
        )
        shadow = self.state.handle_customer_product_image(
            "default",
            "customer@c.us",
            "shadow-image",
            {"type": "image"},
            "请识别这个产品",
            "客户发送图片",
        )
        self.assertEqual(shadow["action"], "shadow")
        self.assertEqual(shadow["reason_code"], "SHADOW_MODE")
        self.assertEqual(len(self.client.image_calls), 0)


if __name__ == "__main__":
    unittest.main()
