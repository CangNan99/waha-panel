import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from panel.app import init_db
from panel.product_image_service import ProductImageService


def _image_bytes(image_format="PNG", size=(2, 2), color=(12, 34, 56, 255)):
    image = Image.new("RGB" if image_format == "JPEG" else "RGBA", size, color[:3] if image_format == "JPEG" else color)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class ProductImageServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.service = ProductImageService(self.database, self.root / "panel-data")

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_is_created_and_idempotent_with_default_settings(self):
        init_db(self.database, seed_business=False)
        init_db(self.database, seed_business=False)
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(product_image_assets)")
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "product_image_settings",
                "product_image_assets",
                "product_image_tags",
                "product_image_asset_tags",
                "product_image_send_requests",
            } <= tables
        )
        self.assertTrue({"product_name", "sku", "aliases_json"} <= columns)
        self.assertEqual(
            self.service.settings_payload(),
            {"auto_send_enabled": True, "daily_limit": 20},
        )
        self.assertTrue(self.service.image_root.is_dir())

    def test_valid_upload_returns_safe_metadata_and_resolves_exact_tag(self):
        result = self.service.upload(
            "产品.png",
            "image/png",
            _image_bytes(),
            "产品图",
            [" IPTV  ", "Premium"],
            "这是 {product_tag}",
        )
        self.assertTrue(result["asset_id"])
        self.assertEqual(result["mime_type"], "image/png")
        self.assertFalse(Path(result["relative_path"]).is_absolute())
        self.assertNotIn(str(self.root), result["relative_path"])
        self.assertTrue((self.service.data_root / result["relative_path"]).is_file())
        resolved = self.service.resolve_for_send("iptv")
        self.assertEqual(resolved["status"], "FOUND")
        self.assertEqual(resolved["asset_id"], result["asset_id"])

    def test_upload_rejects_adversarial_inputs_without_leaving_files(self):
        invalid = [
            ("../../escape.png", "image/png", b"not-an-image"),
            ("photo.jpg", "image/jpeg", _image_bytes("PNG")),
            ("photo.png", "image/png", _image_bytes("JPEG")),
            ("photo.png", "image/png", b"not-an-image"),
            ("photo.png", "image/png", b"x" * (10 * 1024 * 1024 + 1)),
        ]
        for filename, declared_type, data in invalid:
            with self.subTest(filename=filename, declared_type=declared_type):
                with self.assertRaises(ValueError):
                    self.service.upload(filename, declared_type, data, "bad", ["bad"])
        oversized_pixels = _image_bytes("PNG", size=(6501, 6200), color=(1, 2, 3, 255))
        with self.assertRaises(ValueError):
            self.service.upload("large.png", "image/png", oversized_pixels, "large", ["large"])
        files = list((self.service.data_root / "product-images").rglob("*")) if (self.service.data_root / "product-images").exists() else []
        self.assertEqual([path for path in files if path.is_file()], [])

    def test_duplicate_hash_reuses_existing_asset_without_merging_metadata(self):
        data = _image_bytes("WEBP")
        first = self.service.upload("first.webp", "image/webp", data, "第一张", ["a"])
        second = self.service.upload("second.webp", "image/webp", data, "第二张", ["b"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["asset_id"], first["asset_id"])
        self.assertEqual(self.service.get_asset(first["asset_id"])["title"], "第一张")
        self.assertEqual(self.service.list_assets()["total"], 1)

    def test_resolution_reports_not_found_ambiguous_and_soft_disable(self):
        first = self.service.upload("one.png", "image/png", _image_bytes(), "一", ["same"])
        second = self.service.upload("two.png", "image/png", _image_bytes(color=(99, 1, 2, 255)), "二", ["same"])
        self.assertEqual(self.service.resolve_for_send("missing")["status"], "NOT_FOUND")
        self.assertEqual(self.service.resolve_for_send("same")["status"], "AMBIGUOUS")
        self.service.update_asset(first["asset_id"], {"is_primary": True, "priority": 10})
        self.service.update_asset(second["asset_id"], {"is_primary": True, "priority": 10})
        self.assertEqual(self.service.resolve_for_send("same")["status"], "AMBIGUOUS")
        self.service.disable_asset(second["asset_id"])
        self.assertEqual(self.service.resolve_for_send("same")["status"], "FOUND")
        self.assertEqual(self.service.list_assets(tag="same")["total"], 1)

    def test_settings_validate_and_persist(self):
        self.assertEqual(
            self.service.save_settings({"auto_send_enabled": False, "daily_limit": 7}),
            {"auto_send_enabled": False, "daily_limit": 7},
        )
        with self.assertRaises(ValueError):
            self.service.save_settings({"daily_limit": 0})
        with self.assertRaises(ValueError):
            self.service.save_settings({"daily_limit": 1001})

    def test_overlong_tags_are_rejected_instead_of_silently_truncated(self):
        with self.assertRaises(ValueError):
            self.service.upload(
                "long.png", "image/png", _image_bytes(), "长标签", ["x" * 121]
            )

    def test_product_matching_metadata_normalizes_and_resolves_exact_sources(self):
        result = self.service.upload(
            "g5.png",
            "image/png",
            _image_bytes(),
            "G5 图片",
            ["电视盒子"],
            "了解 {product_tag}",
            product_name="  G5  Ｐｌｕｓ ",
            sku=" G5-PLUS ",
            aliases=[" IPTV  Pro ", "高级版"],
        )
        self.assertEqual(result["product_name"], "G5 Plus")
        self.assertEqual(result["sku"], "G5-PLUS")
        self.assertEqual(result["aliases"], ["IPTV Pro", "高级版"])
        for query in ("g5 plus", "g5-plus", "iptv pro", "高级版", "电视盒子"):
            with self.subTest(query=query):
                resolved = self.service.resolve_for_send(query)
                self.assertEqual(resolved["status"], "FOUND", resolved)
                self.assertEqual(resolved["asset_id"], result["asset_id"])
        self.assertEqual(self.service.resolve_for_send("pro")["status"], "NOT_FOUND")

    def test_product_matching_uses_source_rank_then_priority_and_reports_ties(self):
        name_match = self.service.upload(
            "name.png", "image/png", _image_bytes(), "名称命中", ["other"],
            product_name="Shared Product",
        )
        tag_match = self.service.upload(
            "tag.png", "image/png", _image_bytes(color=(99, 1, 2, 255)), "标签命中", ["shared product"]
        )
        resolved = self.service.resolve_for_send(" shared  product ")
        self.assertEqual(resolved["status"], "FOUND", resolved)
        self.assertEqual(resolved["asset_id"], name_match["asset_id"])

        tie_a = self.service.upload(
            "tie-a.png", "image/png", _image_bytes(color=(4, 5, 6, 255)), "并列一", ["one"],
            product_name="Tie Product",
        )
        tie_b = self.service.upload(
            "tie-b.png", "image/png", _image_bytes(color=(7, 8, 9, 255)), "并列二", ["two"],
            product_name="Tie Product",
        )
        ambiguous = self.service.resolve_for_send("tie product")
        self.assertEqual(ambiguous["status"], "AMBIGUOUS", ambiguous)
        self.assertEqual(len(ambiguous["candidates"]), 2)
        self.assertIn(tie_a["asset_id"][:12], ambiguous["candidates"])
        self.assertIn(tie_b["asset_id"][:12], ambiguous["candidates"])

    def test_product_matching_rejects_overlong_metadata(self):
        cases = [
            {"product_name": "x" * 201},
            {"sku": "x" * 121},
            {"aliases": ["x" * 121]},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    self.service.upload(
                        "metadata.png", "image/png", _image_bytes(), "元数据", ["metadata"],
                        **metadata,
                    )

    def test_product_matching_variant_is_an_exact_secondary_key(self):
        asset = self.service.upload(
            "variant.png",
            "image/png",
            _image_bytes(),
            "变体图片",
            ["catalog", "blue"],
            product_name="Variant Product",
        )
        found = self.service.resolve_for_send("variant product", " blue ")
        self.assertEqual(found["status"], "FOUND", found)
        self.assertEqual(found["asset_id"], asset["asset_id"])
        self.assertEqual(
            self.service.resolve_for_send("variant product", "red")["status"],
            "NOT_FOUND",
        )


if __name__ == "__main__":
    unittest.main()
