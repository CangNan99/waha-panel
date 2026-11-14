import base64
import json
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from panel.app import PanelHandler, PanelState, init_db
from panel.product_match_service import ProductMatchService


class _Client:
    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]


class AdminProductMarketingRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.state = PanelState(
            self.database,
            _Client(),
            "fixture-key",
            admin_username="admin",
            admin_password="fixture-password",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        handler = type("AdminProductMarketingHandler", (PanelHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.authorization = "Basic " + base64.b64encode(b"admin:fixture-password").decode("ascii")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.state.stop_background_services()
        self.temp.cleanup()

    def request(self, path, authenticated=True, method="GET", payload=None, csrf=False):
        headers = {"Authorization": self.authorization} if authenticated else {}
        if csrf:
            headers["X-CSRF-Token"] = self.state.csrf_token
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            response = urlopen(Request(self.base + path, headers=headers, data=body, method=method), timeout=5)
        except HTTPError as error:
            response = error
        with closing(response):
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            data = json.loads(raw.decode("utf-8")) if content_type.startswith("application/json") else raw.decode("utf-8")
            return response.status, data

    def test_pages_and_apis_require_admin_and_render(self):
        for path in ("/product-images", "/marketing"):
            self.assertEqual(self.request(path, authenticated=False)[0], 401)
            status, html = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("<script>", html)
        self.assertEqual(self.request("/api/product-catalog", authenticated=False)[0], 401)
        self.assertEqual(self.request("/api/marketing/campaigns", authenticated=False)[0], 401)

    def test_product_catalog_crud_payload_is_redacted(self):
        payload = {
            "product_key": "g5-plus",
            "product_name": "G5 Plus",
            "sku": "G5-PLUS",
            "aliases": ["高级版"],
            "description": "可公开介绍",
            "allow_ai_image_send": True,
            "allow_marketing_image_send": True,
        }
        status, created = self.request("/api/product-catalog", method="POST", payload=payload, csrf=True)
        self.assertEqual(status, 201)
        self.assertEqual(created["product_key"], "g5-plus")
        encoded = json.dumps(created, ensure_ascii=False)
        self.assertNotIn("api_key", encoded.lower())
        self.assertNotIn("relative_path", created)
        product_id = created["product_id"]
        status, listed = self.request("/api/product-catalog")
        self.assertEqual(status, 200)
        self.assertEqual(listed["items"][0]["product_id"], product_id)
        status, updated = self.request(
            f"/api/product-catalog/{product_id}",
            method="PUT",
            payload={"product_name": "G5 Plus 2026", "product_key": "g5-plus", "description": "更新"},
            csrf=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["product_name"], "G5 Plus 2026")
        status, disabled = self.request(f"/api/product-catalog/{product_id}", method="DELETE", csrf=True)
        self.assertEqual(status, 200)
        self.assertFalse(disabled["active"])

    def test_campaign_crud_pause_resume_and_redacted_stats(self):
        campaign = {
            "campaign_id": "spring-2026",
            "name": "春季活动",
            "objective": "介绍新品",
            "target_rules": {"sessions": ["default"], "idle_days": 7},
            "product_scope": ["g5-plus"],
            "allow_image_send": True,
            "max_touches": 2,
        }
        status, created = self.request("/api/marketing/campaigns", method="POST", payload=campaign, csrf=True)
        self.assertEqual(status, 201)
        self.assertEqual(created["campaign_id"], "spring-2026")
        self.assertNotIn("chat_key_hmac", json.dumps(created))
        status, listed = self.request("/api/marketing/campaigns")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["campaign_id"] == "spring-2026" for item in listed["items"]))
        status, paused = self.request("/api/marketing/campaigns/spring-2026/pause", method="POST", payload={"reason": "人工暂停"}, csrf=True)
        self.assertEqual(status, 200)
        self.assertFalse(paused["active"])
        status, resumed = self.request("/api/marketing/campaigns/spring-2026/resume", method="POST", payload={}, csrf=True)
        self.assertEqual(status, 200)
        self.assertTrue(resumed["active"])
        status, contacts = self.request("/api/marketing/campaigns/spring-2026/contacts")
        self.assertEqual(status, 200)
        self.assertIn("stats", contacts)
        self.assertNotIn("chat_key_hmac", json.dumps(contacts))

    def test_product_image_settings_round_trip_and_global_kill(self):
        payload = {
            "customer_image_recognition_enabled": False,
            "chat_product_image_enabled": False,
            "marketing_product_image_enabled": False,
            "product_image_shadow_mode": True,
            "product_image_global_kill_switch": True,
            "product_image_confidence_threshold": 0.92,
            "product_image_cooldown_seconds": 3600,
            "daily_limit": 7,
        }
        status, saved = self.request("/api/product-image-settings?session=default", method="PUT", payload=payload, csrf=True)
        self.assertEqual(status, 200)
        status, loaded = self.request("/api/product-image-settings?session=default")
        self.assertEqual(status, 200)
        for key in payload:
            self.assertEqual(loaded[key], payload[key], key)
        self.assertEqual(self.state.product_image_tool_schema("default", loaded), [])

    def test_confidence_threshold_and_global_kill_are_server_authoritative(self):
        catalog = [{"product_id": "p1", "product_key": "g5-plus", "product_name": "G5 Plus", "allow_ai_image_send": True}]
        decision = ProductMatchService.evaluate(
            [{"product_key": "g5-plus", "confidence": 0.92}], catalog, "identify",
            confidence_threshold=0.95,
        )
        self.assertNotEqual(decision["decision"], "AUTO_SEND")
        self.state.save_settings({"product_image_global_kill_switch": True}, "default")
        self.state.ensure_managed_session("sales")
        self.assertTrue(self.state.settings_payload("sales")["product_image_global_kill_switch"])
        self.assertEqual(
            self.state.product_image_tool_schema(
                "sales", {"chat_product_image_enabled": True, "product_image_global_kill_switch": False}
            ),
            [],
        )

    def test_invalid_mixed_image_settings_do_not_partially_write(self):
        before = self.request("/api/product-image-settings")[1]
        status, _ = self.request(
            "/api/product-image-settings?session=default",
            method="PUT",
            payload={
                "auto_send_enabled": False,
                "daily_limit": 7,
                "product_image_confidence_threshold": 2,
            },
            csrf=True,
        )
        self.assertEqual(status, 400)
        after = self.request("/api/product-image-settings")[1]
        self.assertEqual(after, before)

    def test_nested_sensitive_campaign_and_catalog_metadata_is_rejected(self):
        status, _ = self.request(
            "/api/marketing/campaigns",
            method="POST",
            payload={
                "campaign_id": "sensitive-campaign",
                "name": "敏感数据测试",
                "objective": "测试",
                "target_rules": {"nested": {"token": "SECRET", "path": "C:\\private\\customer"}},
            },
            csrf=True,
        )
        self.assertEqual(status, 400)
        status, _ = self.request(
            "/api/product-catalog",
            method="POST",
            payload={
                "product_key": "sensitive-product",
                "product_name": "测试产品",
                "language_content": {"en": {"token": "SECRET", "path": "/private/x", "chat_id": "raw"}},
            },
            csrf=True,
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
