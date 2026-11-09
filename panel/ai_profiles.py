"""Session-scoped AI capability profiles with encrypted API keys."""

import hashlib
import ipaddress
import re
import socket
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit

try:
    from .chat_security import DataCipherError
except ImportError:  # Supports the existing ``python app.py`` entrypoint.
    from chat_security import DataCipherError


CAPABILITIES = (
    "language",
    "vision_understanding",
    "vision_reasoning",
    "image_generation",
)
REASONING_MODES = ("disabled", "enabled", "low", "medium", "high", "max")
PROVIDERS = ("glm", "openai_compatible")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AIProfileError(RuntimeError):
    code = "AI_PROFILE_ERROR"

    def __init__(self, public_message, code=None):
        self.public_message = str(public_message)
        if code:
            self.code = str(code)
        super().__init__(self.public_message)


class AIProfileUnavailableError(AIProfileError):
    code = "AI_PROFILE_UNAVAILABLE"


class ProfileValidationError(AIProfileError):
    code = "INVALID_AI_PROFILE"


def _sha256(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _is_public_ip(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


class AIProfileStore:
    """Persist four independent AI capability profiles per managed session."""

    def __init__(self, database_path, cipher, resolver=socket.getaddrinfo, clock=time.time):
        self.database_path = str(database_path)
        self.cipher = cipher
        self.resolver = resolver
        self.clock = clock

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _session_name(value):
        name = str(value or "").strip()
        if not SESSION_RE.fullmatch(name):
            raise ProfileValidationError("会话技术名称无效", "INVALID_SESSION")
        return name

    def _require_cipher(self):
        if self.cipher is None:
            raise AIProfileUnavailableError("GLM 配置需要面板数据加密密钥")

    def _validate_base_url(self, value):
        address = str(value or "").strip().rstrip("/")
        try:
            parsed = urlsplit(address)
        except ValueError as error:
            raise ProfileValidationError("AI 服务地址无效", "INVALID_AI_URL") from error
        if parsed.scheme.lower() != "https":
            raise ProfileValidationError("AI 服务地址必须使用 HTTPS", "INVALID_AI_URL")
        if parsed.username is not None or parsed.password is not None:
            raise ProfileValidationError("AI 服务地址不能包含账号或密码", "INVALID_AI_URL")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise ProfileValidationError("AI 服务地址无效", "INVALID_AI_URL")
        try:
            port = parsed.port or 443
        except ValueError as error:
            raise ProfileValidationError("AI 服务端口无效", "INVALID_AI_URL") from error
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise ProfileValidationError("AI 服务地址不能指向内网或本机", "UNSAFE_AI_URL")
        try:
            records = self.resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as error:
            raise ProfileValidationError("AI 服务域名无法解析", "AI_HOST_UNAVAILABLE") from error
        addresses = []
        for record in records or []:
            try:
                addresses.append(record[4][0])
            except (IndexError, TypeError):
                continue
        if not addresses or any(not _is_public_ip(item) for item in addresses):
            raise ProfileValidationError("AI 服务地址不能指向内网或本机", "UNSAFE_AI_URL")
        return address

    def validate_profile(self, capability, provider, base_url, model, reasoning_mode):
        capability = str(capability or "").strip()
        provider = str(provider or "glm").strip().lower()
        base_url = str(base_url or "").strip()
        model = str(model or "").strip()
        reasoning_mode = str(reasoning_mode or "disabled").strip().lower()
        if capability not in CAPABILITIES:
            raise ProfileValidationError("AI 能力类型无效")
        if provider not in PROVIDERS:
            raise ProfileValidationError("AI 服务类型无效")
        if capability != "language" and provider != "glm":
            raise ProfileValidationError("视觉和图像生成能力只支持 GLM 服务")
        if reasoning_mode not in REASONING_MODES:
            raise ProfileValidationError("推理模式无效")
        if len(model) > 200:
            raise ProfileValidationError("模型名称不能超过 200 个字符")
        if bool(base_url) != bool(model):
            raise ProfileValidationError("服务地址和模型名称必须同时填写")
        if base_url:
            base_url = self._validate_base_url(base_url)
        return {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "reasoning_mode": reasoning_mode,
        }

    def _row_map(self, session):
        name = self._session_name(session)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT capability, provider, base_url, model, api_key_ciphertext, "
                "key_fingerprint, reasoning_mode, updated_at "
                "FROM session_ai_profiles WHERE session_name = ?",
                (name,),
            ).fetchall()
        return {row["capability"]: row for row in rows}

    def profile_payload(self, session):
        self._require_cipher()
        rows = self._row_map(session)
        profiles = {}
        for capability in CAPABILITIES:
            row = rows.get(capability)
            profiles[capability] = {
                "provider": str(row["provider"] if row else "glm"),
                "base_url": str(row["base_url"] if row else ""),
                "model": str(row["model"] if row else ""),
                "reasoning_mode": str(row["reasoning_mode"] if row else "disabled"),
                "api_key_configured": bool(row and row["api_key_ciphertext"]),
                "updated_at": row["updated_at"] if row else None,
            }
        return {"profiles": profiles}

    def save_profiles(self, session, payload):
        self._require_cipher()
        name = self._session_name(session)
        if not isinstance(payload, dict):
            raise ProfileValidationError("AI 配置格式不正确")
        raw_profiles = payload.get("profiles", payload)
        if not isinstance(raw_profiles, dict):
            raise ProfileValidationError("AI 配置格式不正确")
        old_rows = self._row_map(name)
        now = int(self.clock())
        updates = []
        for capability, raw in raw_profiles.items():
            if capability not in CAPABILITIES:
                raise ProfileValidationError("AI 能力类型无效")
            if not isinstance(raw, dict):
                raise ProfileValidationError("AI 能力配置格式不正确")
            normalized = self.validate_profile(
                capability,
                raw.get("provider", "glm"),
                raw.get("base_url", ""),
                raw.get("model", ""),
                raw.get("reasoning_mode", "disabled"),
            )
            previous = old_rows.get(capability)
            ciphertext = previous["api_key_ciphertext"] if previous else ""
            fingerprint = previous["key_fingerprint"] if previous else ""
            if raw.get("clear_api_key") is True:
                ciphertext = ""
                fingerprint = ""
            elif "api_key" in raw and str(raw.get("api_key") or "").strip():
                api_key = str(raw["api_key"]).strip()
                ciphertext = self.cipher.encrypt_json({"api_key": api_key})
                fingerprint = _sha256(api_key)
            updates.append((
                name,
                capability,
                normalized["provider"],
                normalized["base_url"],
                normalized["model"],
                ciphertext,
                fingerprint,
                normalized["reasoning_mode"],
                now,
            ))
        if updates:
            with closing(self._connect()) as connection:
                connection.executemany(
                    "INSERT INTO session_ai_profiles "
                    "(session_name, capability, provider, base_url, model, api_key_ciphertext, "
                    "key_fingerprint, reasoning_mode, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_name, capability) DO UPDATE SET "
                    "provider=excluded.provider, base_url=excluded.base_url, model=excluded.model, "
                    "api_key_ciphertext=excluded.api_key_ciphertext, key_fingerprint=excluded.key_fingerprint, "
                    "reasoning_mode=excluded.reasoning_mode, updated_at=excluded.updated_at",
                    updates,
                )
                connection.commit()
        return self.profile_payload(name)

    def effective_profile(self, session, capability, legacy_settings=None):
        name = self._session_name(session)
        capability = str(capability or "").strip()
        if capability not in CAPABILITIES:
            raise ProfileValidationError("AI 能力类型无效")
        legacy = legacy_settings if isinstance(legacy_settings, dict) else {}
        rows = self._row_map(session) if self.cipher is not None else {}
        row = rows.get(capability)
        if row and row["base_url"] and row["model"] and row["api_key_ciphertext"]:
            try:
                api_key = str(self.cipher.decrypt_json(row["api_key_ciphertext"]).get("api_key") or "")
            except DataCipherError as error:
                raise AIProfileError("AI 密钥无法读取", "AI_KEY_UNREADABLE") from error
            if api_key:
                return {
                    "session_name": name,
                    "provider": row["provider"],
                    "capability": capability,
                    "base_url": row["base_url"],
                    "model": row["model"],
                    "api_key": api_key,
                    "key_fingerprint": row["key_fingerprint"] or _sha256(api_key),
                    "reasoning_mode": row["reasoning_mode"],
                }
        if capability == "language":
            base_url = str(legacy.get("ai_base_url") or "").strip()
            model = str(legacy.get("ai_model") or "").strip()
            api_key = str(legacy.get("ai_api_key") or "").strip()
            if base_url and model and api_key:
                return {
                    "session_name": name,
                    "provider": "openai_compatible",
                    "capability": "language",
                    "base_url": base_url,
                    "model": model,
                    "api_key": api_key,
                    "key_fingerprint": _sha256(api_key),
                    "reasoning_mode": "disabled",
                }
        return None
