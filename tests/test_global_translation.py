import sqlite3
import tempfile
import unittest
from pathlib import Path

from panel.app import init_db
from panel.global_translation_service import GlobalTranslationError, GlobalTranslationService
from panel.translation_service import TranslationService


class FakeAliyun:
    def __init__(self):
        self.calls = []
        self.cache_calls = []
        self.test_calls = 0

    def settings_payload(self):
        return {
            "access_key_id_configured": True,
            "access_key_secret_configured": True,
            "last_test_status": "success",
            "last_test_at": 10,
        }

    def translate_batch(self, session_name, chat_key, items):
        self.calls.append((session_name, chat_key, items))
        return {"items": [
            {
                "message_ref": item["message_ref"],
                "status": "READY",
                "translation": "阿里译文",
                "detected_language": "en",
                "error_code": None,
            }
            for item in items
        ]}

    def get_cached_batch(self, session_name, chat_key, items):
        self.cache_calls.append((session_name, chat_key, items))
        return {"items": [{"message_ref": items[0]["message_ref"], "status": "READY", "translation": "缓存阿里译文"}]}

    def test_connection(self):
        self.test_calls += 1
        return {"ok": True, "provider": "aliyun_professional"}

    def clear_cache(self):
        return {"cleared": True, "deleted": 1}


class FakeAI:
    def __init__(self):
        self.calls = []
        self.cache_calls = []
        self.test_calls = 0

    def settings_payload(self):
        return {
            "api_key_configured": True,
            "base_url": "https://ai.example/v1",
            "last_test_status": "success",
            "last_test_at": 11,
            "model": "test-model",
        }

    def translate_display(self, text, force=False, role="customer"):
        self.calls.append((text, force, role))
        return {
            "chinese_translation": "AI译文",
            "source_language_code": "en",
            "already_zh": False,
            "cached": False,
        }

    def get_cached_display(self, text, role="customer"):
        self.cache_calls.append((text, role))
        return {
            "chinese_translation": "缓存AI译文",
            "source_language_code": "en",
            "already_zh": False,
            "cached": True,
        }

    def test_connection(self):
        self.test_calls += 1
        return {"ok": True, "provider": "ai_model"}

    def clear_cache(self):
        return {"cleared": True, "deleted": 2}


class GlobalTranslationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.aliyun = FakeAliyun()
        self.ai = FakeAI()
        self.service = GlobalTranslationService(self.database, self.aliyun, self.ai, clock=lambda: 100)

    def tearDown(self):
        self.temp.cleanup()

    def items(self):
        return [{"message_ref": "m-1", "text": "Hello", "role": "customer"}]

    def test_defaults_to_aliyun_professional(self):
        payload = self.service.settings_payload()
        self.assertEqual(payload["provider"], "aliyun_professional")
        self.assertEqual(payload["scene"], "communication")
        self.assertTrue(payload["aliyun_configured"])
        self.assertTrue(payload["ai_configured"])

    def test_provider_switch_is_persisted_and_invalid_value_does_not_overwrite(self):
        saved = self.service.save_settings({"provider": "ai_model"})
        self.assertEqual(saved["provider"], "ai_model")
        with self.assertRaises(GlobalTranslationError) as context:
            self.service.save_settings({"provider": "invalid"})
        self.assertEqual(context.exception.code, "INVALID_PROVIDER")
        self.assertEqual(self.service.settings_payload()["provider"], "ai_model")

    def test_only_selected_provider_is_called_and_results_are_tagged(self):
        aliyun_result = self.service.translate_batch("default", "k" * 64, self.items())
        self.assertEqual(aliyun_result["provider"], "aliyun_professional")
        self.assertEqual(aliyun_result["items"][0]["translation"], "阿里译文")
        self.assertEqual(len(self.aliyun.calls), 1)
        self.assertEqual(self.ai.calls, [])

        self.service.save_settings({"provider": "ai_model"})
        ai_result = self.service.translate_batch("default", "k" * 64, self.items())
        self.assertEqual(ai_result["provider"], "ai_model")
        self.assertEqual(ai_result["items"][0]["translation"], "AI译文")
        self.assertEqual(len(self.ai.calls), 1)

    def test_cache_reads_use_only_selected_provider_without_network_translation(self):
        self.service.save_settings({"provider": "ai_model"})
        result = self.service.get_cached_batch("default", "k" * 64, self.items())
        self.assertEqual(result["provider"], "ai_model")
        self.assertEqual(result["items"][0]["translation"], "缓存AI译文")
        self.assertEqual(self.aliyun.cache_calls, [])
        self.assertEqual(len(self.ai.cache_calls), 1)

    def test_test_connection_uses_current_provider_only(self):
        self.service.save_settings({"provider": "ai_model"})
        result = self.service.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(self.ai.test_calls, 1)
        self.assertEqual(self.aliyun.test_calls, 0)

    def test_translation_service_display_adapter_maps_structured_translation(self):
        service = object.__new__(TranslationService)
        service.translate = lambda text, force=False, role="customer": {
            "chinese_translation": "AI译文",
            "source_language_code": "en",
            "already_zh": False,
            "cached": True,
        }
        result = service.translate_display("Hello", role="agent")
        self.assertEqual(result["translation"], "AI译文")
        self.assertEqual(result["detected_language"], "en")
        self.assertTrue(result["cached"])


if __name__ == "__main__":
    unittest.main()
