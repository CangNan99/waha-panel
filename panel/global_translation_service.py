"""Provider routing for the chat page's global message translation."""

import sqlite3
import time
from contextlib import closing

try:
    from .translation_service import TranslationError, normalize_translation_role
except ImportError:  # pragma: no cover - supports the container entrypoint
    from translation_service import TranslationError, normalize_translation_role


PROVIDERS = ("aliyun_professional", "ai_model")
SCENES = ("communication", "social")
MAX_BATCH_ITEMS = 30
MAX_BATCH_CHARS = 100_000


class GlobalTranslationError(TranslationError):
    pass


class GlobalTranslationService:
    def __init__(self, database_path, aliyun_service, ai_service, clock=None, logger=None):
        self.database_path = str(database_path)
        self.aliyun = aliyun_service
        self.ai = ai_service
        self.clock = clock or time.time
        self.logger = logger

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _preference(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT provider,scene,updated_at FROM global_translation_settings WHERE id=1"
            ).fetchone()
        if not row:
            return {"provider": "aliyun_professional", "scene": "communication", "updated_at": None}
        provider = str(row["provider"] or "aliyun_professional")
        scene = str(row["scene"] or "communication")
        if provider not in PROVIDERS:
            provider = "aliyun_professional"
        if scene not in SCENES:
            scene = "communication"
        return {"provider": provider, "scene": scene, "updated_at": row["updated_at"]}

    @staticmethod
    def _configured(settings, provider):
        if provider == "aliyun_professional":
            return bool(
                settings.get("access_key_id_configured")
                and settings.get("access_key_secret_configured")
            )
        return bool(
            settings.get("api_key_configured")
            and str(settings.get("base_url") or "").strip()
            and str(settings.get("model") or "").strip()
        )

    def settings_payload(self):
        preference = self._preference()
        aliyun = self.aliyun.settings_payload()
        ai = self.ai.settings_payload()
        return {
            "provider": preference["provider"],
            "scene": preference["scene"],
            "updated_at": preference["updated_at"],
            "aliyun_configured": self._configured(aliyun, "aliyun_professional"),
            "ai_configured": self._configured(ai, "ai_model"),
            "aliyun_last_test_status": aliyun.get("last_test_status", "never"),
            "aliyun_last_test_at": aliyun.get("last_test_at"),
            "ai_last_test_status": ai.get("last_test_status", "never"),
            "ai_last_test_at": ai.get("last_test_at"),
        }

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise GlobalTranslationError("INVALID_PROVIDER_SETTINGS", "翻译引擎设置格式不正确")
        provider = str(payload.get("provider") or self._preference()["provider"]).strip()
        scene = str(payload.get("scene") or self._preference()["scene"]).strip()
        if provider not in PROVIDERS:
            raise GlobalTranslationError("INVALID_PROVIDER", "翻译引擎选择无效")
        if scene not in SCENES:
            raise GlobalTranslationError("INVALID_SCENE", "翻译场景选择无效")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE global_translation_settings SET provider=?,scene=?,updated_at=? WHERE id=1",
                (provider, scene, int(self.clock())),
            )
            connection.commit()
        return self.settings_payload()

    @staticmethod
    def _validate_items(items):
        if not isinstance(items, list) or len(items) > MAX_BATCH_ITEMS:
            raise GlobalTranslationError("INVALID_BATCH", "每批最多翻译 30 条消息")
        if any(not isinstance(item, dict) or not str(item.get("message_ref") or "") for item in items):
            raise GlobalTranslationError("INVALID_BATCH", "翻译消息格式无效")
        if sum(len(str(item.get("text") or "")) for item in items) > MAX_BATCH_CHARS:
            raise GlobalTranslationError("BATCH_TOO_LONG", "本批翻译文本过长")

    @staticmethod
    def _with_provider(item, provider):
        result = dict(item)
        result["provider"] = provider
        return result

    @staticmethod
    def _normalize_ai_result(result):
        if not isinstance(result, dict):
            raise GlobalTranslationError("AI_TRANSLATION_INVALID", "AI 翻译结果格式无效")
        if "translation" in result and "status" in result:
            return result
        return {
            "status": "SKIPPED" if result.get("already_zh") else "READY",
            "translation": str(result.get("chinese_translation") or ""),
            "detected_language": str(result.get("source_language_code") or "") or None,
            "cached": bool(result.get("cached")),
            "error_code": None,
        }

    def translate_batch(self, session_name, chat_key, items):
        self._validate_items(items)
        preference = self._preference()
        provider = preference["provider"]
        if provider == "aliyun_professional":
            result = self.aliyun.translate_batch(session_name, chat_key, items)
            return {
                "provider": provider,
                "scene": preference["scene"],
                "items": [self._with_provider(item, provider) for item in result.get("items", [])],
            }

        results = []
        for item in items:
            reference = str(item.get("message_ref") or "")
            try:
                translated = self._normalize_ai_result(self.ai.translate_display(
                    str(item.get("text") or ""),
                    force=bool(item.get("force", False)),
                    role=normalize_translation_role(item.get("role")),
                ))
                results.append(self._with_provider({"message_ref": reference, **translated}, provider))
            except TranslationError as error:
                results.append(self._with_provider({
                    "message_ref": reference,
                    "status": "FAILED",
                    "translation": None,
                    "detected_language": None,
                    "error_code": error.code,
                }, provider))
            except Exception:
                results.append(self._with_provider({
                    "message_ref": reference,
                    "status": "FAILED",
                    "translation": None,
                    "detected_language": None,
                    "error_code": "AI_TRANSLATION_FAILED",
                }, provider))
        return {"provider": provider, "scene": preference["scene"], "items": results}

    def get_cached_batch(self, session_name, chat_key, items):
        self._validate_items(items)
        preference = self._preference()
        provider = preference["provider"]
        if provider == "aliyun_professional":
            result = self.aliyun.get_cached_batch(session_name, chat_key, items)
            return {
                "provider": provider,
                "scene": preference["scene"],
                "items": [self._with_provider(item, provider) for item in result.get("items", [])],
            }

        results = []
        for item in items:
            reference = str(item.get("message_ref") or "")
            cached = self.ai.get_cached_display(
                str(item.get("text") or ""),
                role=normalize_translation_role(item.get("role")),
            )
            if cached is None:
                cached = {"status": "MISS", "translation": None, "detected_language": None, "error_code": None}
            else:
                cached = self._normalize_ai_result(cached)
            results.append(self._with_provider({"message_ref": reference, **cached}, provider))
        return {"provider": provider, "scene": preference["scene"], "items": results}

    def test_connection(self):
        provider = self._preference()["provider"]
        service = self.aliyun if provider == "aliyun_professional" else self.ai
        result = dict(service.test_connection())
        result["provider"] = provider
        return result

    def clear_cache(self, provider=None):
        selected = provider or self._preference()["provider"]
        if selected not in PROVIDERS:
            raise GlobalTranslationError("INVALID_PROVIDER", "翻译引擎选择无效")
        service = self.aliyun if selected == "aliyun_professional" else self.ai
        result = dict(service.clear_cache())
        result["provider"] = selected
        return result
