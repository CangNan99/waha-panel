import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken


class DataCipherError(ValueError):
    pass


class ReferenceError(ValueError):
    pass


class DataCipher:
    def __init__(self, key):
        value = str(key or "").strip()
        if not value:
            raise DataCipherError("面板数据加密密钥未配置")
        try:
            self._fernet = Fernet(value.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise DataCipherError("面板数据加密密钥格式无效") from error

    def encrypt_json(self, value):
        if not isinstance(value, dict):
            raise DataCipherError("只能加密 JSON 对象")
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decrypt_json(self, token):
        try:
            raw = self._fernet.decrypt(str(token or "").encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise DataCipherError("加密数据无法读取") from error
        if not isinstance(value, dict):
            raise DataCipherError("加密数据格式无效")
        return value


class ReferenceCodec:
    def __init__(self, cipher, clock=None):
        self.cipher = cipher
        self.clock = clock or time.time

    def encode(self, kind, session, value, ttl):
        payload = {
            "kind": str(kind),
            "session": str(session),
            "value": str(value),
            "expires_at": int(self.clock()) + int(ttl),
        }
        return self.cipher.encrypt_json(payload)

    def decode(self, token, kind, session):
        try:
            payload = self.cipher.decrypt_json(token)
            actual_kind = str(payload.get("kind", ""))
            actual_session = str(payload.get("session", ""))
            value = str(payload.get("value", ""))
            expires_at = int(payload.get("expires_at", 0))
        except (DataCipherError, TypeError, ValueError) as error:
            raise ReferenceError("安全引用无效") from error
        if not hmac.compare_digest(actual_kind, str(kind)):
            raise ReferenceError("安全引用用途不匹配")
        if not hmac.compare_digest(actual_session, str(session)):
            raise ReferenceError("安全引用不属于当前会话")
        if expires_at < int(self.clock()):
            raise ReferenceError("安全引用已过期")
        if not value:
            raise ReferenceError("安全引用内容无效")
        return value


def chat_key_hmac(secret, session, chat_id):
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise ValueError("HMAC 密钥长度不足")
    message = f"chat\x00{session}\x00{chat_id}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def derive_csrf_token(secret):
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise ValueError("CSRF 密钥长度不足")
    digest = hmac.new(secret, b"waha-panel-csrf-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
