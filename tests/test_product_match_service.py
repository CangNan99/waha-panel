import unittest

from panel.glm_service import GLMError
from panel.product_match_service import ProductMatchService


class ProductMatchServiceTests(unittest.TestCase):
    def test_parse_sanitizes_model_asset_fields(self):
        result = ProductMatchService.parse_result({
            "match_status": "matched",
            "candidates": [{"product_name": "A", "confidence": 0.95, "asset_id": "secret", "url": "https://x"}],
            "intent": "identify",
        })
        self.assertEqual(result["candidates"][0], {"product_name": "A", "confidence": 0.95})

    def test_parse_rejects_bad_confidence_and_json(self):
        with self.assertRaises(GLMError):
            ProductMatchService.parse_result("not-json")
        with self.assertRaises(GLMError):
            ProductMatchService.parse_result({"candidates": [{"confidence": "high"}]})

    def test_evaluate_decisions(self):
        catalog = [{"product_id": "p1", "product_key": "a", "product_name": "A", "aliases": ["alpha"], "active": True}]
        result = ProductMatchService.evaluate([{"product_key": "a", "confidence": 0.95}], catalog, "identify")
        self.assertEqual(result["decision"], "AUTO_SEND")
        self.assertEqual(ProductMatchService.evaluate([], catalog, "unknown")["decision"], "SKIP")

    def test_reference_data_url_is_strict(self):
        self.assertTrue(ProductMatchService.valid_data_url("data:image/png;base64,YQ=="))
        self.assertFalse(ProductMatchService.valid_data_url("https://example.com/a.png"))

    def test_identity_fields_must_be_strings(self):
        with self.assertRaises(GLMError):
            ProductMatchService.parse_result({"match_status":"matched", "candidates":[{"product_name": [], "confidence": .9}]})

    def test_decision_reference_metadata_is_bounded(self):
        result = ProductMatchService.evaluate([{"product_key":"a", "confidence": .95}], [{"product_id":"p", "product_key":"a", "product_name":"A", "reference_assets":[{"title":"https://bad/path", "reference_role":"primary", "relative_path":"/secret"}]}], "identify")
        self.assertEqual(result["product"]["reference_assets"], [{"reference_role":"primary"}])


if __name__ == "__main__":
    unittest.main()
