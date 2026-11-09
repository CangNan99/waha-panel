import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from panel.ai_profiles import AIProfileStore, AIProfileUnavailableError, ProfileValidationError
from panel.app import PanelState, init_db
from panel.chat_security import DataCipher


PUBLIC_RESOLVER = lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))]


def profile_payload(api_key="profile-secret"):
    return {
        "language": {
            "provider": "glm",
            "base_url": "https://example.com/api/paas/v4",
            "model": "glm-5.3",
            "reasoning_mode": "disabled",
            "api_key": api_key,
        },
        "vision_understanding": {
            "provider": "glm",
            "base_url": "https://example.com/api/paas/v4",
            "model": "glm-5.3-flash",
            "reasoning_mode": "disabled",
            "api_key": api_key,
        },
        "vision_reasoning": {
            "provider": "glm",
            "base_url": "https://example.com/api/paas/v4",
            "model": "glm-5.3-flash",
            "reasoning_mode": "high",
            "api_key": api_key,
        },
        "image_generation": {
            "provider": "glm",
            "base_url": "https://example.com/api/paas/v4",
            "model": "glm-image",
            "reasoning_mode": "disabled",
            "api_key": api_key,
        },
    }


class ProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        init_db(self.database, seed_business=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_idempotent_and_adds_profile_columns(self):
        connection = sqlite3.connect(self.database)
        try:
            profile_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(session_ai_profiles)")
            }
            translation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(translation_settings)")
            }
        finally:
            connection.close()
        self.assertEqual(
            profile_columns,
            {
                "session_name", "capability", "provider", "base_url", "model",
                "api_key_ciphertext", "key_fingerprint", "reasoning_mode", "updated_at",
            },
        )
        self.assertIn("provider", translation_columns)
        self.assertIn("reasoning_mode", translation_columns)


class AIProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.cipher = DataCipher(Fernet.generate_key().decode("ascii"))
        self.store = AIProfileStore(
            self.database,
            self.cipher,
            resolver=PUBLIC_RESOLVER,
            clock=lambda: 100,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_profiles_are_encrypted_and_session_scoped(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO managed_sessions(session_name, display_name, created_at, updated_at) "
                "VALUES ('sales', '销售', 1, 1)"
            )
            connection.commit()
        finally:
            connection.close()

        saved = self.store.save_profiles("default", profile_payload())
        self.assertEqual(set(saved["profiles"]), {
            "language", "vision_understanding", "vision_reasoning", "image_generation",
        })
        self.assertTrue(saved["profiles"]["language"]["api_key_configured"])
        self.assertNotIn("profile-secret", repr(saved))

        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT session_name, api_key_ciphertext FROM session_ai_profiles"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual({row[0] for row in rows}, {"default"})
        self.assertTrue(rows[0][1])
        self.assertNotIn("profile-secret", rows[0][1])

        effective = self.store.effective_profile(
            "default", "vision_reasoning", {"ai_base_url": "", "ai_model": "", "ai_api_key": ""}
        )
        self.assertEqual(effective["model"], "glm-5.3-flash")
        self.assertEqual(effective["api_key"], "profile-secret")

    def test_non_language_openai_profile_is_rejected(self):
        invalid = profile_payload()
        invalid["image_generation"]["provider"] = "openai_compatible"
        with self.assertRaises(ProfileValidationError):
            self.store.save_profiles("default", invalid)

    def test_legacy_language_settings_are_used_when_profile_is_missing(self):
        effective = self.store.effective_profile(
            "default",
            "language",
            {
                "ai_base_url": "https://example.com/v1",
                "ai_model": "legacy-model",
                "ai_api_key": "legacy-secret",
            },
        )
        self.assertEqual(effective["provider"], "openai_compatible")
        self.assertEqual(effective["model"], "legacy-model")
        self.assertEqual(effective["api_key"], "legacy-secret")


class PanelStateProfileAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.state = PanelState(self.database, object(), "waha-key")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_encryption_key_does_not_break_legacy_settings(self):
        settings = self.state.settings_payload("default")
        self.assertFalse(settings["ai_api_key_configured"])
        with self.assertRaises(AIProfileUnavailableError):
            self.state.ai_profiles_payload("default")
        with self.assertRaises(AIProfileUnavailableError):
            self.state.save_ai_profiles("default", profile_payload())

    def test_empty_profile_payload_does_not_block_legacy_save_without_key(self):
        saved = self.state.save_settings({
            "default_reply_text": "旧文本仍可保存",
            "ai_profiles": {"profiles": {
                "language": {"provider": "glm", "base_url": "", "model": "", "api_key": ""},
                "vision_understanding": {"provider": "glm", "base_url": "", "model": "", "api_key": ""},
                "vision_reasoning": {"provider": "glm", "base_url": "", "model": "", "api_key": ""},
                "image_generation": {"provider": "glm", "base_url": "", "model": "", "api_key": ""},
            }},
        }, "default")
        self.assertEqual(saved["default_reply_text"], "旧文本仍可保存")


if __name__ == "__main__":
    unittest.main()
