import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from panel.ai_profiles import AIProfileStore
from panel.app import PanelState, init_db
from panel.chat_security import DataCipher


class LegacyMigrationRegressionTests(unittest.TestCase):
    def test_old_text_settings_survive_repeated_migrations(self):
        temp = tempfile.TemporaryDirectory()
        try:
            database = Path(temp.name) / "panel.sqlite3"
            init_db(database, seed_business=False)
            state = PanelState(database, object(), "waha-key")
            state.save_settings({
                "ai_base_url": "https://example.com/v1",
                "ai_model": "legacy-model",
                "ai_api_key": "legacy-secret",
            })
            init_db(database, seed_business=False)
            cipher = DataCipher(Fernet.generate_key().decode("ascii"))
            store = AIProfileStore(database, cipher, resolver=lambda host, port, type=None: [])
            fallback = store.effective_profile("default", "language", state._settings("default"))
            self.assertEqual(fallback["model"], "legacy-model")
            self.assertEqual(fallback["api_key"], "legacy-secret")
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM session_ai_profiles").fetchone()[0],
                    0,
                )
            finally:
                connection.close()
        finally:
            temp.cleanup()

    def test_user_docs_describe_glm_capabilities_and_image_auto_reply(self):
        readme = Path("README.zh-CN.md").read_text(encoding="utf-8")
        install = Path("INSTALL.zh-CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install
        for phrase in ("视觉理解", "视觉推理", "图像生成", "语言模型", "图片自动回复", "PANEL_DATA_ENCRYPTION_KEY"):
            self.assertIn(phrase, combined)


if __name__ == "__main__":
    unittest.main()
