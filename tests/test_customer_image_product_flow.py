import base64
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

from panel.app import PanelState, init_db


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class _Client:
    def __init__(self):
        self.image_calls = 0

    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]

    def get_chats(self, *_args):
        return [{"id": "customer@c.us", "name": "客户"}]

    def get_messages(self, *_args, **_kwargs):
        return []

    def send_image(self, *_args):
        self.image_calls += 1
        return {"id": f"wamid.customer-image.{self.image_calls}"}


class _Profiles:
    def effective_profile(self, *_args):
        return {"model": "fixture", "base_url": "https://example.invalid", "api_key": "hidden"}


class CustomerImageProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = _Client()
        self.state = PanelState(
            self.database, self.client, "fixture-key", admin_username="admin", admin_password="fixture-password",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.save_settings({"auto_reply_all_day": True}, "default")
        self.product = self.state.product_catalog.save_product({"product_key": "g5-plus", "product_name": "G5 Plus", "sku": "G5-PLUS"})
        self.asset = self.state.product_images.upload(
            "g5.png", "image/png", PNG, "G5 Plus", ["g5-plus"], "这是 {product_tag}",
            product_name="G5 Plus", sku="G5-PLUS", product_id=self.product["product_id"],
        )
        self.state.ai_profiles = _Profiles()
        self.state.glm = object()
        self.state._load_ai_image_content = lambda *_args: {"mime_type": "image/png", "data": base64.b64encode(PNG).decode("ascii")}

    def tearDown(self):
        self.state.stop_background_services()
        self.temp.cleanup()

    def _run(self, decision):
        self.state.classify_product_image = lambda *_args, **_kwargs: decision
        return self.state.handle_customer_product_image(
            "default", "customer@c.us", "customer-image-1", {"type": "image"}, "请识别这个产品", "客户发送图片",
        )

    def test_high_confidence_unique_match_sends_one_captioned_image(self):
        result = self._run({"decision": "AUTO_SEND", "reason_code": "HIGH_CONFIDENCE_UNIQUE", "confidence": .97, "confidence_bucket": "high", "product": {"product_id": self.product["product_id"], "product_key": "g5-plus", "product_name": "G5 Plus"}, "variant": "", "intent": "identify"})
        self.assertEqual(result["action"], "replied")
        self.assertEqual(self.client.image_calls, 1)
        self.assertEqual(self.state.product_images.daily_sent_count(), 1)

    def test_medium_match_asks_without_sending(self):
        result = self._run({"decision": "ASK_CLARIFICATION", "reason_code": "MEDIUM_CONFIDENCE", "confidence": .7, "confidence_bucket": "medium", "product": {"product_id": self.product["product_id"]}, "variant": "", "intent": "identify"})
        self.assertEqual(result["mode"], "product_match_clarification")
        self.assertEqual(self.client.image_calls, 0)

    def test_matching_audit_stores_hash_without_raw_image(self):
        self._run({"decision": "TEXT_ONLY", "reason_code": "LOW_CONFIDENCE", "confidence": .2, "confidence_bucket": "low", "product": {}, "variant": "", "intent": "unknown"})
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute("SELECT image_sha256,result_json FROM product_image_match_audits ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], hashlib.sha256(PNG).hexdigest())
        self.assertNotIn(base64.b64encode(PNG).decode("ascii"), row[1])


if __name__ == "__main__":
    unittest.main()
