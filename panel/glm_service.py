"""Small, provider-specific GLM adapter used by the panel."""

import base64
import hmac
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    from .chat_service import ChatService, ImageValidationError
except ImportError:  # Supports the existing ``python app.py`` entrypoint.
    from chat_service import ChatService, ImageValidationError


MAX_PROMPT_CHARS = 12_000
MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024
GENERATED_IMAGE_TTL_SECONDS = 10 * 60
MAX_GENERATED_IMAGES = 32
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
TEST_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _msg, _headers, _newurl):
        return None


class GLMError(RuntimeError):
    def __init__(self, public_message, code="AI_ERROR"):
        self.code = str(code)
        self.public_message = str(public_message)
        super().__init__(self.public_message)


@dataclass(frozen=True)
class GLMResult:
    content: str
    reasoning_content: str = ""
    tool_calls: tuple = ()


@dataclass(frozen=True)
class GeneratedImage:
    mime_type: str
    data: bytes
    source_url: str = ""


class GeneratedImageStore:
    """Bounded in-memory cache for images waiting for an explicit send."""

    def __init__(self, clock=time.time, max_items=MAX_GENERATED_IMAGES, ttl=GENERATED_IMAGE_TTL_SECONDS):
        self.clock = clock
        self.max_items = max(1, int(max_items))
        self.ttl = max(1, int(ttl))
        self._items = {}
        self._lock = threading.RLock()

    def _purge(self):
        now = int(self.clock())
        expired = [key for key, item in self._items.items() if item["expires_at"] <= now]
        for key in expired:
            self._items.pop(key, None)

    @staticmethod
    def _owned(item, session, chat_key_hmac):
        return (
            hmac.compare_digest(str(item["session"]), str(session))
            and hmac.compare_digest(str(item["chat_key_hmac"]), str(chat_key_hmac))
        )

    def _item(self, session, chat_key_hmac, reference):
        self._purge()
        item = self._items.get(str(reference))
        if not item or not self._owned(item, session, chat_key_hmac):
            raise GLMError("生成图片不存在或已过期", "GENERATED_IMAGE_NOT_FOUND")
        return item

    def put(self, session, chat_key_hmac, mime_type, data):
        body = bytes(data or b"")
        if len(body) > MAX_GENERATED_IMAGE_BYTES:
            raise GLMError("生成图片超过 10MB 限制", "IMAGE_TOO_LARGE")
        with self._lock:
            self._purge()
            while len(self._items) >= self.max_items:
                oldest = min(self._items.items(), key=lambda pair: pair[1]["created_at"])[0]
                self._items.pop(oldest, None)
            reference = secrets.token_urlsafe(24)
            now = int(self.clock())
            self._items[reference] = {
                "session": str(session),
                "chat_key_hmac": str(chat_key_hmac),
                "mime_type": str(mime_type),
                "data": body,
                "created_at": now,
                "expires_at": now + self.ttl,
                "state": "AVAILABLE",
                "request_id": "",
            }
            return reference

    def get(self, session, chat_key_hmac, reference):
        with self._lock:
            item = self._item(session, chat_key_hmac, reference)
            if item["state"] == "CONSUMED":
                raise GLMError("生成图片已被消费", "GENERATED_IMAGE_CONSUMED")
            return item["mime_type"], item["data"]

    def reserve(self, session, chat_key_hmac, reference, request_id):
        with self._lock:
            item = self._item(session, chat_key_hmac, reference)
            request_id = str(request_id)
            if item["state"] == "CONSUMED":
                if item["request_id"] == request_id:
                    return item["mime_type"], item["data"]
                raise GLMError("生成图片已被消费", "GENERATED_IMAGE_CONSUMED")
            if item["state"] == "RESERVED" and item["request_id"] != request_id:
                raise GLMError("生成图片正在被其他发送请求使用", "GENERATED_IMAGE_BUSY")
            item["state"] = "RESERVED"
            item["request_id"] = request_id
            return item["mime_type"], item["data"]

    def finalize(self, session, chat_key_hmac, reference, request_id, outcome):
        with self._lock:
            item = self._item(session, chat_key_hmac, reference)
            if item["state"] == "CONSUMED" and item["request_id"] == str(request_id):
                return item["state"]
            if item["state"] != "RESERVED" or item["request_id"] != str(request_id):
                raise GLMError("生成图片发送请求无效", "GENERATED_IMAGE_REQUEST_INVALID")
            outcome = str(outcome or "").upper()
            if outcome == "FAILED":
                item["state"] = "AVAILABLE"
                item["request_id"] = ""
            elif outcome in {"SENT", "UNKNOWN"}:
                item["state"] = "CONSUMED"
                item["request_id"] = str(request_id)
            else:
                raise GLMError("生成图片发送结果无效", "GENERATED_IMAGE_OUTCOME_INVALID")
            return item["state"]


class GLMService:
    """HTTP adapter for GLM chat/vision and image generation endpoints."""

    def __init__(self, opener=urlopen, resolver=socket.getaddrinfo, logger=None):
        self.opener = opener
        self.resolver = resolver
        self.logger = logger

    @staticmethod
    def _profile_value(profile, name):
        value = str(profile.get(name) or "").strip() if isinstance(profile, dict) else ""
        if not value:
            raise GLMError("GLM 配置不完整", "AI_NOT_CONFIGURED")
        return value

    @staticmethod
    def _endpoint(profile, suffix):
        configured = GLMService._profile_value(profile, "base_url").rstrip("/")
        try:
            parsed = urlsplit(configured)
        except ValueError as error:
            raise GLMError("GLM 配置地址无效", "AI_INVALID_URL") from error
        if parsed.query or parsed.fragment:
            raise GLMError("GLM 配置地址无效", "AI_INVALID_URL")
        path = parsed.path.rstrip("/")
        for known_suffix in ("/chat/completions", "/images/generations"):
            if path.endswith(known_suffix):
                path = path[: -len(known_suffix)].rstrip("/")
                break
        endpoint_suffix = "/" + str(suffix or "").lstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path + endpoint_suffix, "", ""))

    @staticmethod
    def _prompt(value):
        prompt = str(value or "").strip()
        if not prompt:
            raise GLMError("请输入提示内容", "EMPTY_PROMPT")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise GLMError("提示内容不能超过 12000 个字符", "PROMPT_TOO_LONG")
        return prompt

    @staticmethod
    def _thinking_payload(profile, reasoning=False):
        mode = str(profile.get("reasoning_mode") or "disabled").lower() if isinstance(profile, dict) else "disabled"
        if not reasoning and mode == "disabled":
            return {}
        payload = {"thinking": {"type": "enabled"}}
        if mode in {"low", "medium", "high", "max"}:
            payload["reasoning_effort"] = mode
        return payload

    @staticmethod
    def _content_text(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "".join(parts).strip()
        return str(value or "").strip()

    @staticmethod
    def _result(envelope):
        try:
            message = envelope["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError) as error:
            raise GLMError("GLM 返回内容格式异常", "AI_RESPONSE_INVALID") from error
        content = GLMService._content_text(message.get("content"))
        if not content:
            raise GLMError("GLM 返回内容为空", "AI_RESPONSE_INVALID")
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            tool_calls = []
        return GLMResult(
            content=content,
            reasoning_content=GLMService._content_text(message.get("reasoning_content")),
            tool_calls=tuple(tool_calls),
        )

    def _request_json(
        self, endpoint, api_key, payload, timeout=30, accept="application/json", session_name=None
    ):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(endpoint, data=body, method="POST", headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": accept,
        })
        try:
            with self.opener(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
                if status < 200 or status >= 300:
                    raise GLMError("GLM 服务暂时不可用", "AI_UPSTREAM_ERROR")
        except GLMError:
            raise
        except HTTPError as error:
            self._safe_log(
                "error", f"glm HTTP error: {getattr(error, 'code', 'unknown')}", session_name
            )
            raise GLMError("GLM 服务请求失败", "AI_UPSTREAM_ERROR") from error
        except (URLError, TimeoutError, OSError) as error:
            self._safe_log("error", "glm connection error", session_name)
            raise GLMError("无法连接 GLM 服务", "AI_CONNECTION_ERROR") from error
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise GLMError("GLM 服务返回内容格式异常", "AI_RESPONSE_INVALID") from error

    def _safe_log(self, level, message, session_name=None):
        if not self.logger:
            return
        try:
            self.logger(level, message, session_name)
        except TypeError:
            try:
                self.logger(level, message)
            except TypeError:
                try:
                    self.logger(message)
                except Exception:
                    return
            except Exception:
                return
        except Exception:
            return

    def chat_text(self, profile, messages, tools=None):
        api_key = self._profile_value(profile, "api_key")
        model = self._profile_value(profile, "model")
        if not isinstance(messages, list) or not messages:
            raise GLMError("GLM 消息内容为空", "EMPTY_MESSAGES")
        payload = {"model": model, "messages": messages}
        payload.update(self._thinking_payload(profile))
        if tools:
            payload["tools"] = tools
        return self._result(self._request_json(
            self._endpoint(profile, "/chat/completions"),
            api_key,
            payload,
            session_name=profile.get("session_name") if isinstance(profile, dict) else None,
        ))

    def chat_vision(self, profile, prompt, image_bytes, mime_type, reasoning=False, context=None, tools=None):
        prompt = self._prompt(prompt)
        declared_mime = str(mime_type or "").lower().strip()
        extension = IMAGE_EXTENSIONS.get(declared_mime, ".png")
        try:
            _safe_name, normalized_mime = ChatService._validated_image(
                "vision-image" + extension, declared_mime, bytes(image_bytes or b"")
            )
        except ImageValidationError as error:
            raise GLMError(error.public_message, getattr(error, "code", "INVALID_IMAGE")) from error
        encoded = base64.b64encode(bytes(image_bytes)).decode("ascii")
        messages = []
        if isinstance(context, dict):
            system = str(context.get("system") or "").strip()
            if system:
                messages.append({"role": "system", "content": system})
            history = context.get("messages")
            if isinstance(history, list):
                messages.extend(item for item in history if isinstance(item, dict))
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{normalized_mime};base64,{encoded}"}},
            ],
        })
        api_key = self._profile_value(profile, "api_key")
        model = self._profile_value(profile, "model")
        payload = {"model": model, "messages": messages}
        payload.update(self._thinking_payload(profile, reasoning=reasoning))
        if tools:
            payload["tools"] = tools
        return self._result(self._request_json(
            self._endpoint(profile, "/chat/completions"),
            api_key,
            payload,
            session_name=profile.get("session_name") if isinstance(profile, dict) else None,
        ))

    def _fetch_generated_url(self, profile, url):
        parsed = urlsplit(str(url or ""))
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise GLMError("生成图片地址无效", "INVALID_GENERATED_IMAGE_URL")
        configured_host = urlsplit(self._profile_value(profile, "base_url")).hostname or ""
        host = parsed.hostname.lower()
        allowed = (
            host == configured_host.lower()
            or host.endswith(".bigmodel.cn")
            or host.endswith(".z.ai")
            or host == "chatglm.cn"
            or host.endswith(".chatglm.cn")
        )
        if not allowed:
            raise GLMError("生成图片地址不属于 GLM 服务", "UNSAFE_GENERATED_IMAGE_URL")
        request = Request(str(url), headers={
            "Accept": "image/png, image/jpeg, image/webp, application/octet-stream",
        })
        try:
            if self.opener is urlopen:
                opener = build_opener(_NoRedirectHandler())
                response_context = opener.open(request, timeout=15)
            else:
                response_context = self.opener(request, timeout=15)
            with response_context as response:
                body = response.read()
                content_type = getattr(response, "headers", {}).get("Content-Type", "image/png")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise GLMError("生成图片读取失败", "AI_IMAGE_FETCH_ERROR") from error
        try:
            _safe_name, mime_type = ChatService._validated_image("generated.png", content_type, body)
        except ImageValidationError as error:
            raise GLMError(error.public_message, getattr(error, "code", "INVALID_IMAGE")) from error
        return GeneratedImage(mime_type=mime_type, data=bytes(body), source_url=str(url))

    def generate_image(self, profile, prompt, size=None):
        prompt = self._prompt(prompt)
        api_key = self._profile_value(profile, "api_key")
        payload = {"model": self._profile_value(profile, "model"), "prompt": prompt}
        if size:
            payload["size"] = str(size)[:32]
        envelope = self._request_json(
            self._endpoint(profile, "/images/generations"),
            api_key,
            payload,
            timeout=30,
            session_name=profile.get("session_name") if isinstance(profile, dict) else None,
        )
        try:
            item = envelope["data"][0]
        except (KeyError, IndexError, TypeError) as error:
            raise GLMError("GLM 图像生成返回内容格式异常", "AI_RESPONSE_INVALID") from error
        if not isinstance(item, dict):
            raise GLMError("GLM 图像生成返回内容格式异常", "AI_RESPONSE_INVALID")
        if item.get("b64_json"):
            try:
                body = base64.b64decode(str(item["b64_json"]), validate=True)
            except (ValueError, TypeError) as error:
                raise GLMError("GLM 图像生成内容无效", "AI_RESPONSE_INVALID") from error
            try:
                _safe_name, mime_type = ChatService._validated_image("generated.png", "image/png", body)
            except ImageValidationError as error:
                raise GLMError(error.public_message, getattr(error, "code", "INVALID_IMAGE")) from error
            return GeneratedImage(mime_type=mime_type, data=body)
        if item.get("url"):
            return self._fetch_generated_url(profile, item["url"])
        raise GLMError("GLM 图像生成结果为空", "AI_RESPONSE_INVALID")

    def test_connection(self, profile, capability="language"):
        capability = str(capability or "language")
        if capability in {"vision_understanding", "vision_reasoning"}:
            result = self.chat_vision(
                profile,
                "只返回：连接正常",
                TEST_PNG_BYTES,
                "image/png",
                reasoning=capability == "vision_reasoning",
            )
        else:
            result = self.chat_text(profile, [{"role": "user", "content": "只返回：连接正常"}])
        return {"ok": True, "content": result.content}
