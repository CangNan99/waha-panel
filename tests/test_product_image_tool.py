import base64
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from panel.app import PanelState, init_db


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Client:
    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]


class ProductImageToolTests(unittest.TestCase):
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
        self.state.save_settings({"auto_reply_all_day": True}, "default")
        self.product = self.state.product_catalog.save_product(
            {
                "product_key": "g5-plus",
                "product_name": "G5 Plus",
                "sku": "G5-PLUS",
                "aliases": ["高级版"],
            }
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

    def tearDown(self):
        self.state.stop_background_services()
        self.temp.cleanup()

    def test_schema_exposes_product_key_variant_caption_and_reason_only_when_enabled(self):
        settings = {
            "chat_product_image_enabled": True,
            "product_image_global_kill_switch": False,
        }
        schema = self.state.product_image_tool_schema("default", settings)
        self.assertEqual(len(schema), 1)
        function = schema[0]["function"]
        self.assertEqual(function["name"], "send_product_image")
        self.assertEqual(set(function["parameters"]["properties"]), {"product_key", "variant", "caption", "reason"})
        self.assertEqual(function["parameters"]["required"], ["product_key"])
        self.assertEqual(self.state.product_image_tool_schema("default", {**settings, "product_image_global_kill_switch": True}), [])

    def test_validation_accepts_explicit_image_comparison_and_effect_interest(self):
        settings = {"chat_product_image_enabled": True, "product_image_global_kill_switch": False}
        for text in ("请发 G5 Plus 图片", "G5 Plus 和基础版效果对比", "想看看 G5 Plus 的界面效果"):
            result = self.state.validate_product_image_tool_call(
                {"product_key": "g5-plus", "caption": "这是产品图片", "reason": "客户询问效果"},
                text,
                "",
            )
            self.assertIsInstance(result, dict)
            self.assertEqual(result["product"]["product_id"], self.product["product_id"])
            self.assertEqual(result["asset"]["asset_id"], self.asset["asset_id"])

    def test_validation_rejects_unknown_product_missing_interest_and_model_paths(self):
        cases = (
            ({"product_key": "missing"}, "请发 missing 图片"),
            ({"product_key": "g5-plus"}, "你好"),
            ({"product_key": "g5-plus", "url": "https://evil.test/a.png"}, "请发 G5 Plus 图片"),
            ({"product_key": "g5-plus", "asset_id": self.asset["asset_id"]}, "请发 G5 Plus 图片"),
            ({"product_key": "C:\\secret\\a.png"}, "请发图片"),
        )
        for arguments, text in cases:
            self.assertIsNone(self.state.validate_product_image_tool_call(arguments, text, ""))

    def test_validation_rejects_unsafe_or_overlong_caption(self):
        base = {"product_key": "g5-plus"}
        self.assertIsNone(
            self.state.validate_product_image_tool_call(
                {**base, "caption": "内部路径 C:\\secret\\g5.png"}, "请发 G5 Plus 图片", ""
            )
        )
        self.assertIsNone(
            self.state.validate_product_image_tool_call(
                {**base, "caption": "x" * 401}, "请发 G5 Plus 图片", ""
            )
        )

    def test_policy_switches_round_trip_and_global_kill_blocks_tool(self):
        self.state.save_settings({
            "customer_image_recognition_enabled": False,
            "chat_product_image_enabled": False,
            "marketing_product_image_enabled": False,
            "product_image_global_kill_switch": True,
        }, "default")
        payload = self.state.settings_payload("default")
        self.assertFalse(payload["customer_image_recognition_enabled"])
        self.assertFalse(payload["chat_product_image_enabled"])
        self.assertFalse(payload["marketing_product_image_enabled"])
        self.assertTrue(payload["product_image_global_kill_switch"])
        self.assertEqual(self.state.product_image_tool_schema("default", payload), [])

    def test_execute_path_revalidates_interest_and_server_policy(self):
        self.state.chat.is_human_takeover = lambda *_args: False
        result = self.state.execute_product_image_tool(
            "default", "customer@c.us", "message-1",
            {"product_key": "g5-plus", "caption": "产品图片", "reason": "效果"},
            "你好",
        )
        self.assertEqual(result["action"], "ignored")

    def test_validation_accepts_product_name_alias_identity(self):
        result = self.state.validate_product_image_tool_call(
            {"product_key": "G5 Plus", "caption": "产品图片"}, "请发 G5 Plus 图片", ""
        )
        self.assertEqual(result["product"]["product_key"], "g5-plus")


if __name__ == "__main__":
    unittest.main()
