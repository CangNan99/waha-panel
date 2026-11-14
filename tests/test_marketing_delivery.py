import sqlite3
import tempfile
import unittest
from pathlib import Path

from panel.app import init_db
from panel.marketing_automation import MarketingAutomationService
from panel.marketing_delivery_service import MarketingDeliveryService


DAY = 24 * 60 * 60


class _Chat:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []
        self.inbound = False
        self.human = False
        self.opted_out = False
        self.text_state = "SENT"
        self.image_state = "SENT"

    def has_inbound_since(self, _session, _chat_id, _since):
        return self.inbound

    def is_human_takeover(self, _session, _chat_id):
        return self.human

    def is_marketing_opted_out(self, _session, _chat_id, history=None):
        return self.opted_out

    def send_automated_text(self, session, chat_id, text, request_id):
        self.text_calls.append((session, chat_id, text, request_id))
        if self.text_state != "SENT":
            return {"request_id": request_id, "state": self.text_state, "error_code": "TEXT_STATE"}
        return {"request_id": request_id, "state": "SENT", "message_ref": "text-ref"}

    def send_automated_image(self, session, chat_id, filename, mime, data, caption, request_id, origin="marketing"):
        self.image_calls.append((session, chat_id, filename, mime, data, caption, request_id, origin))
        if self.image_state != "SENT":
            return {"request_id": request_id, "state": self.image_state, "error_code": "IMAGE_STATE"}
        return {"request_id": request_id, "state": "SENT", "message_ref": "image-ref"}


class _Images:
    def __init__(self):
        self.reservations = []
        self.finishes = []
        self.asset = {
            "id": "asset-1",
            "product_id": "product-1",
            "original_filename": "product.png",
            "mime_type": "image/png",
            "caption_template": "Product one",
            "active": True,
        }
        self.data = b"png-bytes"

    def resolve_product_asset(self, product_id, variant="", role="primary"):
        if product_id != "product-1":
            return {"status": "NOT_FOUND", "code": "PRODUCT_NOT_FOUND"}
        return dict(self.asset)

    def preview(self, asset_id):
        if asset_id != "asset-1":
            raise RuntimeError("missing")
        return "image/png", self.data

    def reserve_auto_send(self, session, chat_id, message_key, product_id, asset_id, source, caption):
        request_id = f"image:{message_key}"
        self.reservations.append((session, chat_id, message_key, product_id, asset_id, source, caption))
        return {"request_id": request_id, "state": "PENDING", "reserved": True, "asset_id": asset_id}

    def finish_auto_send(self, request_id, state, message_id=None, error_code=None):
        self.finishes.append((request_id, state, message_id, error_code))
        return {"request_id": request_id, "state": state, "message_id": message_id, "error_code": error_code}


class MarketingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.clock = [1_700_000_000]
        self.chat = _Chat()
        self.images = _Images()
        self.delivery = MarketingDeliveryService(
            self.database,
            self.chat,
            self.images,
            clock=lambda: self.clock[0],
        )

    def tearDown(self):
        self.temp.cleanup()

    def _base(self, **overrides):
        payload = {
            "delivery_id": "delivery-1",
            "campaign_id": "campaign-1",
            "session_name": "default",
            "chat_id": "customer@c.us",
            "chat_key_hmac": "chat-key",
            "text": "您好，这是一条活动消息",
            "product_id": "product-1",
            "allow_image_send": True,
            "product_scope": ["product-1"],
            "customer_last_contact_at": self.clock[0] - DAY,
            "max_touches": 3,
            "touch_count": 0,
        }
        payload.update(overrides)
        return payload

    def test_text_only_delivery_is_idempotent(self):
        result = self.delivery.send_text_and_optional_image(self._base(allow_image_send=False))
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["text_state"], "SENT")
        self.assertEqual(result["image_state"], "SKIPPED")
        self.assertEqual(len(self.chat.text_calls), 1)
        again = self.delivery.send_text_and_optional_image(self._base(allow_image_send=False))
        self.assertEqual(again["delivery_id"], "delivery-1")
        self.assertEqual(len(self.chat.text_calls), 1)

    def test_text_then_one_scoped_product_image(self):
        result = self.delivery.send_text_and_optional_image(self._base())
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["text_state"], "SENT")
        self.assertEqual(result["image_state"], "SENT")
        self.assertEqual(len(self.chat.image_calls), 1)
        self.assertEqual(self.images.reservations[0][3], "product-1")

    def test_product_image_asset_id_field_is_supported(self):
        self.images.asset = {
            "asset_id": "asset-1",
            "product_id": "product-1",
            "original_filename": "product.png",
            "mime_type": "image/png",
            "caption_template": "Product one",
            "active": True,
        }
        result = self.delivery.send_text_and_optional_image(self._base(delivery_id="delivery-asset-id"))
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["asset_id"], "asset-1")
        self.assertEqual(self.images.reservations[0][4], "asset-1")

    def test_customer_reply_between_reservation_and_send_stops_delivery(self):
        self.chat.inbound = True
        result = self.delivery.send_text_and_optional_image(self._base())
        self.assertIn(result["state"], {"FAILED", "STOPPED"})
        self.assertEqual(len(self.chat.text_calls), 0)
        self.assertEqual(len(self.chat.image_calls), 0)

    def test_text_unknown_prevents_image(self):
        self.chat.text_state = "UNKNOWN"
        result = self.delivery.send_text_and_optional_image(self._base())
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["text_state"], "UNKNOWN")
        self.assertEqual(result["image_state"], "SKIPPED")
        self.assertEqual(self.chat.image_calls, [])

    def test_text_sent_image_unknown_is_unknown_and_reconcile_does_not_retry(self):
        self.chat.image_state = "UNKNOWN"
        result = self.delivery.send_text_and_optional_image(self._base())
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["text_state"], "SENT")
        self.assertEqual(result["image_state"], "UNKNOWN")
        reconciled = self.delivery.reconcile("delivery-1")
        self.assertEqual(reconciled["state"], "UNKNOWN")
        self.assertEqual(len(self.chat.image_calls), 1)

    def test_restart_reconciles_confirmed_text_and_finishes_requested_image(self):
        reserved = self.delivery.reserve(self._base(delivery_id="delivery-restart"))
        self.assertTrue(reserved["reserved"])
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO automated_send_requests "
                "(client_request_id,session_name,chat_id,payload_hash,state,waha_message_id,error_code,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    reserved["text_request_id"],
                    "default",
                    "customer@c.us",
                    "hash",
                    "SENT",
                    "text-recovered",
                    None,
                    self.clock[0],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        recovered = MarketingDeliveryService(
            self.database,
            self.chat,
            self.images,
            clock=lambda: self.clock[0],
        )
        result = recovered.send_text_and_optional_image(self._base(delivery_id="delivery-restart"))
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["text_state"], "SENT")
        self.assertEqual(result["image_state"], "SENT")
        self.assertEqual(len(self.chat.image_calls), 1)

    def test_restart_reconciles_confirmed_generic_image_transport(self):
        reserved = self.delivery.reserve(self._base(delivery_id="delivery-image-transport"))
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE marketing_deliveries SET text_state='SENT',image_state='IMAGE_PENDING',"
                "image_request_id=?,state='IMAGE_PENDING' WHERE delivery_id=?",
                ("image-transport-request", reserved["delivery_id"]),
            )
            connection.execute(
                "INSERT INTO automated_send_requests "
                "(client_request_id,session_name,chat_id,payload_hash,state,waha_message_id,error_code,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "image-transport-request",
                    "default",
                    "customer@c.us",
                    "hash",
                    "SENT",
                    "wamid.image-recovered",
                    None,
                    self.clock[0],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        recovered = MarketingDeliveryService(
            self.database,
            self.chat,
            self.images,
            clock=lambda: self.clock[0],
        )
        result = recovered.reconcile("delivery-image-transport")
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["image_state"], "SENT")
        self.assertEqual(self.chat.image_calls, [])

    def test_image_pending_reservation_counts_toward_daily_limit(self):
        first = self.delivery.reserve(self._base(delivery_id="delivery-pending-limit", daily_limit=1))
        self.assertTrue(first["reserved"])
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE marketing_deliveries SET state='IMAGE_PENDING' WHERE delivery_id=?",
                (first["delivery_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        second = self.delivery.reserve(
            self._base(
                delivery_id="delivery-pending-limit-2",
                chat_key_hmac="chat-key-2",
                daily_limit=1,
            )
        )
        self.assertEqual(second["state"], "SKIPPED")
        self.assertEqual(second["error_code"], "DAILY_LIMIT")

    def test_image_switch_is_reread_after_text_confirmation(self):
        state = {"enabled": True}
        service = MarketingDeliveryService(
            self.database,
            self.chat,
            self.images,
            settings_loader=lambda _session: {
                "marketing_product_image_enabled": "1" if state["enabled"] else "0",
                "product_image_global_kill_switch": "0",
            },
            clock=lambda: self.clock[0],
        )
        original = self.chat.send_automated_text

        def send_text(*args, **kwargs):
            result = original(*args, **kwargs)
            state["enabled"] = False
            return result

        self.chat.send_automated_text = send_text
        result = service.send_text_and_optional_image(self._base(delivery_id="delivery-switch-race"))
        self.assertEqual(result["text_state"], "SENT")
        self.assertEqual(result["image_state"], "SKIPPED")
        self.assertEqual(result["error_code"], "MARKETING_IMAGE_DISABLED")
        self.assertEqual(self.chat.image_calls, [])

    def test_marketing_automation_derives_distinct_delivery_ids_per_customer(self):
        captured = []

        class _Delivery:
            def send_text_and_optional_image(self, payload):
                captured.append(dict(payload))
                return {"delivery_id": payload["delivery_id"], "state": "SENT"}

        from panel.marketing_automation import MarketingAutomationService

        chat = _Chat()
        service = MarketingAutomationService(
            self.database,
            chat,
            settings_loader=lambda _session: {
                "marketing_product_image_enabled": "1",
                "product_image_global_kill_switch": "0",
                "ai_marketing_cooldown_days": "1",
            },
            sessions_loader=lambda: [],
            message_generator=lambda *_args: ("您好，继续为您介绍 product-1", "ai"),
            clock=lambda: self.clock[0],
            delivery_service=_Delivery(),
        )
        campaign = service.create_campaign({
            "campaign_id": "campaign-delivery-key",
            "name": "营销图片",
            "objective": "跟进产品",
            "allow_image_send": True,
            "product_scope": ["product-1"],
            "max_touches": 2,
        })
        settings = {
            "ai_marketing_cooldown_days": "1",
            "marketing_product_image_enabled": "1",
            "product_image_global_kill_switch": "0",
        }
        for suffix in ("one", "two"):
            candidate = {
                "chat_id": f"customer-{suffix}@c.us",
                "chat_key_hmac": f"chat-key-{suffix}",
                "last_inbound_at": self.clock[0] - DAY,
                "last_outbound_at": self.clock[0] - 2 * DAY,
                "history": [{"from_me": False, "body": "我想了解 product-1"}],
            }
            self.assertTrue(
                service._process_campaign_candidate(
                    "default", candidate, settings, campaign, self.clock[0], daily_limit=20
                )
            )
        self.assertEqual(len(captured), 2)
        self.assertNotEqual(captured[0]["delivery_id"], captured[1]["delivery_id"])

    def test_confirmed_image_failure_requires_explicit_reconciliation(self):
        self.chat.image_state = "FAILED"
        result = self.delivery.send_text_and_optional_image(self._base())
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["image_state"], "FAILED")
        self.chat.image_state = "SENT"
        reconciled = self.delivery.reconcile("delivery-1")
        self.assertEqual(reconciled["state"], "SENT")
        self.assertEqual(len(self.chat.image_calls), 2)

    def test_daily_limit_and_max_touches_are_checked_before_reservation(self):
        limited = self.delivery.send_text_and_optional_image(self._base(daily_limit=0))
        self.assertEqual(limited["state"], "SKIPPED")
        capped = self.delivery.send_text_and_optional_image(self._base(delivery_id="delivery-2", touch_count=3, max_touches=3))
        self.assertEqual(capped["state"], "SKIPPED")
        self.assertEqual(self.chat.text_calls, [])

    def test_marketing_table_never_stores_raw_customer_id(self):
        self.delivery.send_text_and_optional_image(self._base(allow_image_send=False))
        connection = sqlite3.connect(self.database)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(marketing_deliveries)")}
            values = [str(row) for row in connection.execute("SELECT * FROM marketing_deliveries")]
        finally:
            connection.close()
        self.assertNotIn("chat_id", columns)
        self.assertTrue(all("customer@c.us" not in row for row in values))

    def test_marketing_action_discards_out_of_scope_product_and_model_asset(self):
        service = MarketingAutomationService(
            self.database,
            self.chat,
            settings_loader=lambda _session: {},
            sessions_loader=lambda: [],
            message_generator=lambda *_args: {
                "message": "看看这个产品",
                "mode": "ai",
                "product_id": "product-2",
                "asset_id": "internal-secret",
                "url": "https://example.invalid/image.png",
            },
            clock=lambda: self.clock[0],
        )
        action = service.generate_marketing_action(
            "default",
            "customer@c.us",
            [{"body": "产品"}],
            {"objective": "跟进", "allow_image_send": True, "product_scope": ["product-1"]},
        )
        self.assertEqual(action["message"], "看看这个产品")
        self.assertFalse(action["allow_image_send"])
        self.assertIsNone(action["product_id"])
        self.assertNotIn("asset_id", action)
        self.assertNotIn("url", action)

    def test_marketing_action_selects_one_scoped_product_from_plain_text_generator(self):
        service = MarketingAutomationService(
            self.database,
            self.chat,
            settings_loader=lambda _session: {},
            sessions_loader=lambda: [],
            message_generator=lambda *_args: ("可以继续为您介绍 product-1", "ai"),
            clock=lambda: self.clock[0],
        )
        action = service.generate_marketing_action(
            "default",
            "customer@c.us",
            [{"body": "我想了解 product-1"}],
            {"objective": "跟进", "allow_image_send": True, "product_scope": ["product-1", "product-2"]},
        )
        self.assertTrue(action["allow_image_send"])
        self.assertEqual(action["product_id"], "product-1")


if __name__ == "__main__":
    unittest.main()
