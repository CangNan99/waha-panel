import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from panel.app import init_db
from panel.chat_security import DataCipher, ReferenceCodec
from panel.aliyun_translation_service import AliyunTranslationService, is_pure_chinese


class FakeClient:
    def __init__(self):
        self.requests = []
        self.failure = False

    def translate_general_with_options(self, request, runtime):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("credential-secret source-private-text")
        return {"body": {"Code": 200, "Data": {"Translated": "你好", "DetectedLanguage": "en"}}}


class AliyunTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.cipher = DataCipher(Fernet.generate_key().decode("ascii"))
        self.client = FakeClient()
        self.logs = []
        self.service = AliyunTranslationService(
            self.database, self.cipher,
            sdk_client_factory=lambda _settings: self.client,
            logger=lambda level, message: self.logs.append((level, message)),
        )

    def tearDown(self):
        self.temp.cleanup()

    def configure(self):
        return self.service.save_settings({
            "access_key_id": "credential-id",
            "access_key_secret": "credential-secret",
        })

    def translate(self, text="Hello", message_ref="message-1"):
        return self.service.translate_batch(
            "default", "a" * 64, [{"message_ref": message_ref, "text": text}]
        )["items"][0]

    def test_missing_configuration_fails_without_sdk_call(self):
        self.assertFalse(self.service.settings_payload()["access_key_secret_configured"])
        result = self.translate()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "ALIYUN_CONFIG_MISSING")
        self.assertEqual(self.client.requests, [])

    def test_chinese_empty_and_nonlanguage_content_are_skipped(self):
        self.configure()
        for text in ("你好，价格 123 元 😊", "繁體中文", "", "😊 123 !!!"):
            self.assertEqual(self.translate(text)["status"], "SKIPPED")
        self.assertEqual(self.client.requests, [])
        self.assertTrue(is_pure_chinese("您好！😊"))
        self.assertFalse(is_pure_chinese("hello 你好"))

    def test_non_chinese_uses_general_auto_to_zh_plain_text(self):
        self.configure()
        result = self.translate()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["translation"], "你好")
        request = self.client.requests[0]
        self.assertEqual(request.source_language, "auto")
        self.assertEqual(request.target_language, "zh")
        self.assertEqual(request.format_type, "text")
        self.assertEqual(request.scene, "general")

    def test_mixed_text_is_translated(self):
        self.configure()
        self.assertEqual(self.translate("Hello 你好")["status"], "READY")
        self.assertEqual(len(self.client.requests), 1)

    def test_translation_cache_is_encrypted_and_source_scoped(self):
        self.configure()
        self.translate()
        self.translate()
        self.assertEqual(len(self.client.requests), 1)
        self.translate("Hello again")
        self.assertEqual(len(self.client.requests), 2)
        with closing(sqlite3.connect(self.database)) as connection:
            ciphertexts = connection.execute(
                "SELECT translation_ciphertext FROM machine_message_translations"
            ).fetchall()
        self.assertEqual(len(ciphertexts), 2)
        self.assertNotIn("你好", ciphertexts[0][0])
        self.assertEqual(self.cipher.decrypt_json(ciphertexts[0][0])["translation"], "你好")

    def test_cached_batch_reads_completed_translation_without_configuration_or_sdk_call(self):
        self.configure()
        self.assertEqual(self.translate("Hello", "cached-message")["status"], "READY")
        self.client.requests.clear()
        self.service.save_settings({"clear_credentials": True})

        result = self.service.get_cached_batch(
            "default",
            "a" * 64,
            [
                {"message_ref": "cached-message", "text": "Hello"},
                {"message_ref": "not-cached", "text": "New message"},
            ],
        )

        self.assertEqual(result["items"][0]["status"], "READY")
        self.assertEqual(result["items"][0]["translation"], "你好")
        self.assertEqual(result["items"][1]["status"], "MISS")
        self.assertEqual(self.client.requests, [])

    def test_old_cached_translation_survives_expired_message_reference(self):
        self.configure()
        codec = ReferenceCodec(self.cipher, clock=lambda: 1)
        value = json.dumps({"chat_id": "chat-1", "message_id": "m-1"})
        old_ref = codec.encode("message", "default", value, ttl=1)
        self.service.translate_batch("default", "a" * 64, [{"message_ref": old_ref, "text": "Hello"}])
        # Older releases used the encrypted message reference as the database key.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE machine_message_translations SET message_ref=?, message_id=''",
                (old_ref,),
            )
            connection.commit()
        new_ref = ReferenceCodec(self.cipher).encode("message", "default", value, ttl=60)
        self.service.save_settings({"clear_credentials": True})
        self.client.requests.clear()
        result = self.service.get_cached_batch("default", "a" * 64, [
            {"message_ref": new_ref, "message_id": "m-1", "text": "Hello"},
        ])["items"][0]
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["message_ref"], new_ref)
        self.assertEqual(result["translation"], "你好")
        self.assertEqual(self.client.requests, [])

    def test_oversized_text_is_rejected_before_sdk(self):
        self.configure()
        result = self.translate("a" * 4801)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "TEXT_TOO_LONG")
        self.assertEqual(self.client.requests, [])

    def test_errors_do_not_log_credentials_or_source(self):
        self.configure()
        self.client.failure = True
        self.assertEqual(self.translate("source-private-text")["status"], "FAILED")
        output = json.dumps(self.logs)
        self.assertNotIn("credential-secret", output)
        self.assertNotIn("source-private-text", output)

    def test_settings_hide_credentials_and_preserve_blank_secret(self):
        settings = self.configure()
        self.assertTrue(settings["access_key_id_configured"])
        self.assertTrue(settings["access_key_secret_configured"])
        settings = self.service.save_settings({"access_key_secret": ""})
        self.assertTrue(settings["access_key_secret_configured"])
        output = json.dumps(settings)
        self.assertNotIn("credential-id", output)
        self.assertNotIn("credential-secret", output)

    def test_connection_test_always_calls_aliyun_and_updates_status(self):
        self.configure()
        self.assertTrue(self.service.test_connection()["ok"])
        self.assertTrue(self.service.test_connection()["ok"])
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.service.settings_payload()["last_test_status"], "success")


if __name__ == "__main__":
    unittest.main()
