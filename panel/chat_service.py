import base64
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import warnings
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from urllib.parse import quote, urlencode

from PIL import Image, UnidentifiedImageError

try:
    from .chat_security import ReferenceError as SecureReferenceError, chat_key_hmac
except ImportError:
    from chat_security import ReferenceError as SecureReferenceError, chat_key_hmac


SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_MEDIA_PROXY_BYTES = 15 * 1024 * 1024
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_CACHE_LIMIT = 256
AVATAR_TTL_SECONDS = 24 * 60 * 60
AVATAR_NEGATIVE_TTL_SECONDS = 30
CHAT_REF_TTL = 24 * 60 * 60
MESSAGE_REF_TTL = 60 * 60
IMAGE_TYPES = {
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "PNG": ("image/png", {".png"}),
    "WEBP": ("image/webp", {".webp"}),
}
MEDIA_LABELS = {"image": "图片", "video": "视频", "audio": "音频", "file": "文件"}
CONTEXT_FIELD_LIMIT = 512


def sanitize_context_content(value, limit=65535):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).strip()[:limit]


def format_media_context(direction, kind, filename="", mime="", caption="", size=None):
    safe_kind = kind if kind in MEDIA_LABELS else "file"
    actor = "客服" if direction == "outbound" else "客户"
    parts = [f"[{actor}发送{MEDIA_LABELS[safe_kind]}]"]
    if filename:
        parts.append("文件名=" + sanitize_context_content(filename, CONTEXT_FIELD_LIMIT))
    if mime:
        parts.append("MIME=" + sanitize_context_content(mime, 120))
    if isinstance(size, int) and 0 <= size <= 1024 * 1024 * 1024:
        parts.append(f"大小={size}")
    if caption:
        parts.append("说明=" + sanitize_context_content(caption, 4096))
    return " ".join(parts)


class ChatServiceError(RuntimeError):
    code = "CHAT_ERROR"

    def __init__(self, message, code=None):
        self.public_message = str(message)
        if code:
            self.code = str(code)
        super().__init__(self.public_message)


class ChatAccessError(ChatServiceError):
    code = "CHAT_ACCESS_DENIED"


class ChatSendError(ChatServiceError):
    code = "SEND_FAILED"

    def __init__(self, message, state="FAILED", code=None):
        self.state = state
        super().__init__(message, code)


class SendConflictError(ChatServiceError):
    code = "SEND_REQUEST_CONFLICT"


class ImageValidationError(ChatServiceError):
    code = "INVALID_IMAGE"


class ChatEventBroker:
    def __init__(self, clock=None, max_events=200):
        self.clock = clock or time.time
        self.max_events = max(10, int(max_events))
        self._condition = threading.Condition()
        self._sequences = defaultdict(int)
        self._events = defaultdict(lambda: deque(maxlen=self.max_events))

    def publish(self, session, event):
        name = _session_name(session)
        if not isinstance(event, dict):
            raise ValueError("实时事件必须是对象")
        with self._condition:
            self._sequences[name] += 1
            event_id = self._sequences[name]
            safe = dict(event)
            safe["event_id"] = event_id
            safe.setdefault("published_at", int(self.clock()))
            self._events[name].append(safe)
            self._condition.notify_all()
            return event_id

    def wait(self, session, after=0, timeout=25):
        name = _session_name(session)
        after_id = max(0, int(after or 0))
        wait_seconds = max(0.0, min(float(timeout or 0), 25.0))
        with self._condition:
            available = [item for item in self._events[name] if item["event_id"] > after_id]
            if not available and wait_seconds:
                self._condition.wait(wait_seconds)
                available = [item for item in self._events[name] if item["event_id"] > after_id]
            latest = self._sequences[name]
            return latest, [dict(item) for item in available]


def _session_name(value):
    name = str(value or "").strip()
    if not SESSION_RE.fullmatch(name):
        raise ChatAccessError("会话技术名称无效")
    return name


def _value_id(value):
    if isinstance(value, dict):
        return str(value.get("_serialized") or value.get("id") or "")
    return str(value or "")


def _limited_text(value, limit):
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def _avatar_url(value):
    """Return only a plain HTTP(S) avatar URL supplied by WAHA."""
    if isinstance(value, dict):
        value = value.get("url") or value.get("src") or value.get("profilePictureUrl")
    url = str(value or "").strip()
    if url.startswith(("https://", "http://")) and len(url) <= 2048:
        return url
    return ""


class ChatService:
    def __init__(self, database_path, client, codec, hmac_secret, logger=None,
                 clock=None, broker=None, outbound_recorder=None):
        self.database_path = Path(database_path)
        self.client = client
        self.codec = codec
        self.hmac_secret = bytes(hmac_secret)
        self.logger = logger
        self.clock = clock or time.time
        self.broker = broker or ChatEventBroker(clock=self.clock)
        self.outbound_recorder = outbound_recorder
        self._reference_lock = threading.Lock()
        self._chat_reference_cache = {}
        self._message_reference_cache = {}
        self._avatar_lock = threading.RLock()
        self._avatar_cache = OrderedDict()
        self._avatar_slots = threading.BoundedSemaphore(4)

    def _notify_outbound(self, session, chat_id, message_id, content, request_id):
        if not self.outbound_recorder:
            return
        try:
            self.outbound_recorder(
                session,
                chat_id,
                message_id,
                content,
                created_at=int(self.clock()),
                request_id=request_id,
            )
        except Exception as error:
            if self.logger:
                self.logger("ERROR", "出站消息归档失败：" + sanitize_context_content(error, 300))

    def _avatar_cache_get(self, key, now):
        with self._avatar_lock:
            cached = self._avatar_cache.get(key)
            if not cached:
                return None
            if cached[0] <= now:
                self._avatar_cache.pop(key, None)
                return None
            self._avatar_cache.move_to_end(key)
            return cached

    def _avatar_cache_put(self, key, value):
        with self._avatar_lock:
            self._avatar_cache[key] = value
            self._avatar_cache.move_to_end(key)
            while len(self._avatar_cache) > AVATAR_CACHE_LIMIT:
                self._avatar_cache.popitem(last=False)

    @staticmethod
    def _validate_avatar_response(response):
        if not isinstance(response, tuple) or len(response) != 3:
            raise ValueError("头像响应格式不正确")
        status, content_type, body = response
        try:
            status = int(status)
        except (TypeError, ValueError) as error:
            raise ValueError("头像响应状态不正确") from error
        if status < 200 or status >= 300:
            raise ValueError("头像响应状态不正确")
        if not isinstance(body, bytes) or not body or len(body) > MAX_AVATAR_BYTES:
            raise ValueError("头像内容超过面板读取限制")
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("头像不是受支持的图片格式")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(body)) as image:
                    image_format = str(image.format or "").upper()
                    if image_format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValueError("头像不是受支持的图片格式")
                    image.verify()
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError, ValueError) as error:
            raise ValueError("头像图片内容无法识别") from error
        return status, media_type, body

    def avatar(self, session, chat_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        key = (name, str(chat_id))
        now = int(self.clock())
        cached = self._avatar_cache_get(key, now)
        if cached:
            _expires_at, status, content_type, payload = cached
            if isinstance(payload, bytes):
                return status, content_type, payload
            raise ChatServiceError("客户头像暂不可用", "AVATAR_UNAVAILABLE")
        self._avatar_slots.acquire()
        try:
            # Another request may have filled the cache while this one waited.
            cached = self._avatar_cache_get(key, int(self.clock()))
            if cached:
                _expires_at, status, content_type, payload = cached
                if isinstance(payload, bytes):
                    return status, content_type, payload
                raise ChatServiceError("客户头像暂不可用", "AVATAR_UNAVAILABLE")
            try:
                response = self._validate_avatar_response(
                    self.client.get_chat_picture(name, chat_id)
                )
            except Exception as error:
                self._avatar_cache_put(
                    key,
                    (
                        int(self.clock()) + AVATAR_NEGATIVE_TTL_SECONDS,
                        None,
                        None,
                        "AVATAR_UNAVAILABLE",
                    ),
                )
                if isinstance(error, ChatServiceError):
                    raise
                raise ChatServiceError("客户头像暂不可用", "AVATAR_UNAVAILABLE") from error
            status, content_type, body = response
            self._avatar_cache_put(
                key,
                (int(self.clock()) + AVATAR_TTL_SECONDS, status, content_type, body),
            )
            return response
        finally:
            self._avatar_slots.release()

    def _chat_key(self, session, chat_id):
        return chat_key_hmac(self.hmac_secret, _session_name(session), str(chat_id))

    def _encode_chat(self, session, chat_id):
        name = _session_name(session)
        value = str(chat_id)
        key = (name, value)
        now = int(self.clock())
        with self._reference_lock:
            cached = self._chat_reference_cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
            token = self.codec.encode("chat", name, value, CHAT_REF_TTL)
            self._chat_reference_cache[key] = (token, now + CHAT_REF_TTL)
            if len(self._chat_reference_cache) > 10_000:
                self._chat_reference_cache = {
                    item_key: item for item_key, item in self._chat_reference_cache.items()
                    if item[1] > now
                }
            return token

    def _decode_chat(self, session, chat_ref):
        try:
            return self.codec.decode(chat_ref, "chat", _session_name(session))
        except SecureReferenceError as error:
            raise ChatAccessError("聊天引用无效或已过期") from error

    def _encode_message(self, session, chat_id, message_id):
        name = _session_name(session)
        chat_value = str(chat_id)
        message_value = str(message_id)
        key = (name, chat_value, message_value)
        now = int(self.clock())
        with self._reference_lock:
            cached = self._message_reference_cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
            value = json.dumps(
                {"chat_id": chat_value, "message_id": message_value},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            token = self.codec.encode("message", name, value, MESSAGE_REF_TTL)
            self._message_reference_cache[key] = (token, now + MESSAGE_REF_TTL)
            if len(self._message_reference_cache) > 50_000:
                self._message_reference_cache = {
                    item_key: item for item_key, item in self._message_reference_cache.items()
                    if item[1] > now
                }
            return token

    def _decode_message(self, session, message_ref):
        try:
            raw = self.codec.decode(message_ref, "message", _session_name(session))
            value = json.loads(raw)
        except (SecureReferenceError, ValueError, json.JSONDecodeError) as error:
            raise ChatAccessError("消息引用无效或已过期") from error
        if not isinstance(value, dict) or not value.get("chat_id") or not value.get("message_id"):
            raise ChatAccessError("消息引用内容无效")
        return str(value["chat_id"]), str(value["message_id"])

    def _takeover_state_by_id(self, session, chat_id):
        key = self._chat_key(session, chat_id)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT state, paused_at, resumed_at, updated_at, last_manual_sent_at, auto_resume_at FROM chat_takeovers "
                "WHERE session_name = ? AND chat_key_hmac = ?",
                (_session_name(session), key),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {"state": "AI_ELIGIBLE", "paused_at": None, "resumed_at": None,
                    "last_manual_sent_at": None, "auto_resume_at": None}
        return {
            "state": row[0],
            "paused_at": row[1],
            "resumed_at": row[2],
            "updated_at": row[3],
            "last_manual_sent_at": row[4],
            "auto_resume_at": row[5],
        }

    def chat_identity(self, session, chat_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        return {"chat_id": chat_id, "chat_key_hmac": self._chat_key(name, chat_id)}

    def history_for_id(self, session, chat_id, limit=20):
        name = _session_name(session)
        value = str(chat_id or "").strip()
        if not value:
            raise ChatAccessError("聊天编号无效")
        page_limit = max(1, min(int(limit), 20))
        raw_items = self.client.get_messages(name, value, page_limit, 0, None, download_media=False)
        items = []
        for raw in raw_items:
            message_id = _value_id(raw.get("id") or raw.get("messageId"))
            if not message_id:
                continue
            body = _limited_text(raw.get("body") or raw.get("text"), 100000)
            items.append({"timestamp": int(raw.get("timestamp") or 0),
                          "from_me": bool(raw.get("fromMe")), "body": body,
                          "caption": _limited_text(raw.get("caption") or (body if raw.get("hasMedia") else ""), 100000),
                          "message_id": message_id})
        items.sort(key=lambda item: (item["timestamp"], item["message_id"]))
        return items[-page_limit:]

    def has_inbound_since(self, session, chat_id, since_timestamp):
        name = _session_name(session); chat_id = str(chat_id)
        cutoff = int(since_timestamp or 0)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT last_incoming_at FROM conversation_activity WHERE session_name=? AND chat_id=?", (name, chat_id)).fetchone()
        finally:
            connection.close()
        if row and int(row[0]) > cutoff:
            return True
        offset = 0
        for _ in range(10):
            raw_items = self.client.get_messages(name, chat_id, 20, offset, None, download_media=False)
            if not raw_items:
                break
            found_older = False
            for raw in raw_items:
                timestamp = int(raw.get("timestamp") or 0)
                if timestamp <= cutoff:
                    found_older = True
                if timestamp > cutoff and not bool(raw.get("fromMe")):
                    return True
            if found_older or len(raw_items) < 20:
                break
            offset += len(raw_items)
        return False

    def _note_for_id(self, session, chat_id):
        name = _session_name(session)
        key = self._chat_key(name, chat_id)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT note, updated_at FROM chat_notes WHERE session_name = ? AND chat_key_hmac = ?",
                (name, key),
            ).fetchone()
        finally:
            connection.close()
        return {"note": row[0] if row else "", "updated_at": row[1] if row else None}

    def note(self, session, chat_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        return {"session": name, "chat_ref": chat_ref, **self._note_for_id(name, chat_id)}

    def save_note(self, session, chat_ref, note):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        value = _limited_text(note, 1000)
        key = self._chat_key(name, chat_id)
        now = int(self.clock())
        connection = sqlite3.connect(self.database_path)
        try:
            if value:
                connection.execute(
                    "INSERT INTO chat_notes(session_name, chat_key_hmac, note, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(session_name, chat_key_hmac) DO UPDATE SET note=excluded.note, updated_at=excluded.updated_at",
                    (name, key, value, now),
                )
            else:
                connection.execute(
                    "DELETE FROM chat_notes WHERE session_name = ? AND chat_key_hmac = ?",
                    (name, key),
                )
            connection.commit()
        finally:
            connection.close()
        return {"session": name, "chat_ref": chat_ref, "note": value, "updated_at": now}

    def is_human_takeover(self, session, chat_id):
        return self._takeover_state_by_id(session, chat_id)["state"] == "HUMAN_TAKEOVER"

    def pause(self, session, chat_id):
        name = _session_name(session)
        now = int(self.clock())
        key = self._chat_key(name, chat_id)
        prior = self._takeover_state_by_id(name, chat_id)
        clear_manual = prior["state"] != "HUMAN_TAKEOVER"
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO chat_takeovers(session_name, chat_key_hmac, state, paused_at, resumed_at, updated_at, last_manual_sent_at, auto_resume_at) "
                "VALUES (?, ?, 'HUMAN_TAKEOVER', ?, NULL, ?, NULL, ?) "
                "ON CONFLICT(session_name, chat_key_hmac) DO UPDATE SET "
                "state='HUMAN_TAKEOVER', paused_at=excluded.paused_at, resumed_at=NULL, updated_at=excluded.updated_at, "
                "auto_resume_at=excluded.auto_resume_at, last_manual_sent_at="
                + ("NULL" if clear_manual else "chat_takeovers.last_manual_sent_at"),
                (name, key, now, now, now + 18000),
            )
            connection.commit()
        finally:
            connection.close()
        return self._takeover_state_by_id(name, chat_id)

    def takeover_state(self, session, chat_ref):
        chat_id = self._decode_chat(session, chat_ref)
        return self._takeover_state_by_id(session, chat_id)

    def takeover(self, session, chat_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        return self.pause(name, chat_id)

    def resume_ai(self, session, chat_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        key = self._chat_key(name, chat_id)
        now = int(self.clock())
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO chat_takeovers(session_name, chat_key_hmac, state, paused_at, resumed_at, updated_at, last_manual_sent_at, auto_resume_at) "
                "VALUES (?, ?, 'AI_ELIGIBLE', NULL, ?, ?, NULL, NULL) "
                "ON CONFLICT(session_name, chat_key_hmac) DO UPDATE SET "
                "state='AI_ELIGIBLE', resumed_at=excluded.resumed_at, updated_at=excluded.updated_at, auto_resume_at=NULL",
                (name, key, now, now),
            )
            connection.commit()
        finally:
            connection.close()
        return self._takeover_state_by_id(name, chat_id)

    def _overview_metadata(self, session, chat_ids):
        name = _session_name(session)
        unique_ids = [str(chat_id) for chat_id in dict.fromkeys(chat_ids) if str(chat_id)]
        if not unique_ids:
            return {}
        keys = {chat_id: self._chat_key(name, chat_id) for chat_id in unique_ids}
        placeholders = ",".join("?" for _ in keys)
        values = [keys[chat_id] for chat_id in unique_ids]
        metadata = {
            chat_id: {"key": key, "note": "", "labels": []}
            for chat_id, key in keys.items()
        }
        connection = sqlite3.connect(self.database_path)
        try:
            note_rows = connection.execute(
                "SELECT chat_key_hmac,note FROM chat_notes "
                f"WHERE session_name=? AND chat_key_hmac IN ({placeholders})",
                [name, *values],
            ).fetchall()
            by_key = {key: chat_id for chat_id, key in keys.items()}
            for chat_key, note in note_rows:
                chat_id = by_key.get(chat_key)
                if chat_id is not None:
                    metadata[chat_id]["note"] = _limited_text(note, 1000)
            label_rows = connection.execute(
                "SELECT id,chat_key_hmac,source,label FROM customer_labels "
                f"WHERE session_name=? AND chat_key_hmac IN ({placeholders}) "
                "ORDER BY CASE source WHEN 'MANUAL' THEN 0 ELSE 1 END, id",
                [name, *values],
            ).fetchall()
            for label_id, chat_key, source, label in label_rows:
                chat_id = by_key.get(chat_key)
                if chat_id is None:
                    continue
                metadata[chat_id]["labels"].append({
                    "id": int(label_id),
                    "source": "manual" if source == "MANUAL" else "ai",
                    "label": _limited_text(label, 100),
                })
        finally:
            connection.close()
        return metadata

    def overview(self, session, limit=30, offset=0, search="", unread_only=False):
        name = _session_name(session)
        page_limit = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        raw_items = self.client.get_chats(name, page_limit, page_offset)
        needle = str(search or "").strip().casefold()
        normalized = []
        chat_ids = []
        for raw in raw_items:
            chat_id = _value_id(raw.get("id") or raw.get("chatId"))
            if not chat_id:
                continue
            contact_name = _limited_text(
                raw.get("name") or raw.get("title") or raw.get("pushName") or chat_id.split("@", 1)[0],
                120,
            )
            display_id = chat_id.split("@", 1)[0]
            last = raw.get("lastMessage") if isinstance(raw.get("lastMessage"), dict) else {}
            last_text = _limited_text(
                last.get("body") or last.get("text") or last.get("caption") or raw.get("lastMessageText"),
                300,
            )
            unread = max(0, int(raw.get("unreadCount") or raw.get("unread") or 0))
            if unread_only and unread == 0:
                continue
            if needle and needle not in f"{contact_name} {display_id} {last_text}".casefold():
                continue
            timestamp = int(
                last.get("timestamp") or raw.get("messageTimestamp") or raw.get("timestamp") or 0
            )
            state = self._takeover_state_by_id(name, chat_id)
            chat_ref = self._encode_chat(name, chat_id)
            normalized.append((chat_id, chat_ref, contact_name, display_id, last_text, unread, timestamp, state))
            chat_ids.append(chat_id)
        metadata = self._overview_metadata(name, chat_ids)
        items = []
        for chat_id, chat_ref, contact_name, display_id, last_text, unread, timestamp, state in normalized:
            chat_metadata = metadata.get(chat_id, {"note": "", "labels": []})
            labels = chat_metadata.get("labels", [])
            visible_labels = labels[:4]
            items.append({
                "chat_ref": chat_ref,
                "name": contact_name,
                "avatar_url": (
                    f"/api/chat/sessions/{quote(name, safe='')}/avatar?"
                    + urlencode({"chat_ref": chat_ref})
                ),
                "note": chat_metadata.get("note", ""),
                "labels": visible_labels,
                "label_overflow": max(0, len(labels) - len(visible_labels)),
                "display_id": display_id,
                "is_group": chat_id.endswith("@g.us"),
                "unread_count": unread,
                "timestamp": timestamp,
                "last_message": last_text,
                "takeover_state": state["state"],
            })
        return {
            "session": name,
            "items": items,
            "limit": page_limit,
            "offset": page_offset,
            "next_offset": page_offset + len(raw_items),
            "has_more": len(raw_items) >= page_limit,
        }

    def messages(self, session, chat_ref, limit=50, offset=0, before=None):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        page_limit = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        raw_items = self.client.get_messages(
            name, chat_id, page_limit, page_offset, before, download_media=False
        )
        items = []
        for raw in raw_items:
            message_id = _value_id(raw.get("id") or raw.get("messageId"))
            if not message_id:
                continue
            media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
            body = _limited_text(raw.get("body") or raw.get("text"), 100000)
            caption = _limited_text(raw.get("caption") or (body if raw.get("hasMedia") else ""), 100000)
            items.append({
                "message_ref": self._encode_message(name, chat_id, message_id),
                "timestamp": int(raw.get("timestamp") or 0),
                "from_me": bool(raw.get("fromMe")),
                "body": body,
                "caption": caption,
                "has_media": bool(raw.get("hasMedia") or media),
                "media_type": _limited_text(media.get("mimetype") or raw.get("type"), 80),
                "filename": _limited_text(media.get("filename"), 255),
                "ack": raw.get("ack"),
                "ack_name": _limited_text(raw.get("ackName"), 40),
            })
        items.sort(key=lambda item: (item["timestamp"], item["message_ref"]))
        return {
            "session": name,
            "chat_ref": chat_ref,
            "items": items,
            "limit": page_limit,
            "offset": page_offset,
            "next_offset": page_offset + len(raw_items),
            "has_more": len(raw_items) >= page_limit,
            "takeover": self._takeover_state_by_id(name, chat_id),
        }

    def media(self, session, chat_ref, message_ref):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        message_chat, message_id = self._decode_message(name, message_ref)
        if not __import__("hmac").compare_digest(chat_id, message_chat):
            raise ChatAccessError("消息不属于当前聊天")
        message = self.client.get_message(name, chat_id, message_id, download_media=True)
        media = message.get("media") if isinstance(message, dict) and isinstance(message.get("media"), dict) else {}
        media_url = media.get("url") or (message.get("mediaUrl") if isinstance(message, dict) else None)
        if not media_url:
            raise ChatAccessError("消息没有可读取的媒体")
        status, content_type, body = self.client.get_media_bytes(media_url)
        if len(body) > MAX_MEDIA_PROXY_BYTES:
            raise ImageValidationError("媒体文件超过面板读取限制")
        if not str(content_type).lower().startswith("image/"):
            raise ImageValidationError("当前媒体不是可显示图片")
        return status, content_type, body

    @staticmethod
    def _request_id(value):
        try:
            parsed = uuid.UUID(str(value or ""))
        except (ValueError, AttributeError) as error:
            raise ChatSendError("发送请求编号无效", "FAILED", "INVALID_REQUEST_ID") from error
        return str(parsed)

    @staticmethod
    def _payload_hash(kind, *values):
        digest = hashlib.sha256()
        digest.update(str(kind).encode("utf-8"))
        for value in values:
            digest.update(b"\x00")
            digest.update(value if isinstance(value, bytes) else str(value).encode("utf-8"))
        return digest.hexdigest()

    def _begin_send(self, session, chat_id, request_id, kind, payload_hash):
        name = _session_name(session)
        chat_key = self._chat_key(name, chat_id)
        now = int(self.clock())
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM manual_send_requests WHERE client_request_id = ?", (request_id,)
            ).fetchone()
            if existing:
                if (
                    existing["session_name"] != name
                    or existing["chat_key_hmac"] != chat_key
                    or existing["kind"] != kind
                    or existing["payload_hash"] != payload_hash
                ):
                    raise SendConflictError("发送请求编号已用于其他内容")
                connection.commit()
                return dict(existing)
            connection.execute(
                "INSERT INTO chat_takeovers(session_name, chat_key_hmac, state, paused_at, resumed_at, updated_at) "
                "VALUES (?, ?, 'HUMAN_TAKEOVER', ?, NULL, ?) "
                "ON CONFLICT(session_name, chat_key_hmac) DO UPDATE SET "
                "state='HUMAN_TAKEOVER', paused_at=excluded.paused_at, resumed_at=NULL, updated_at=excluded.updated_at",
                (name, chat_key, now, now),
            )
            connection.execute(
                "INSERT INTO manual_send_requests(client_request_id, session_name, chat_key_hmac, kind, "
                "payload_hash, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)",
                (request_id, name, chat_key, kind, payload_hash, now, now),
            )
            connection.commit()
            return None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_send(self, request_id, state, message_id=None, error_code=None, session=None, chat_id=None):
        now = int(self.clock())
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE manual_send_requests SET state = ?, waha_message_id = ?, error_code = ?, updated_at = ? "
                "WHERE client_request_id = ?",
                (state, message_id, error_code, now, request_id),
            )
            if state == "SENT" and session is not None and chat_id is not None:
                key = self._chat_key(session, chat_id)
                connection.execute(
                    "UPDATE chat_takeovers SET last_manual_sent_at=?, auto_resume_at=?, updated_at=? "
                    "WHERE session_name=? AND chat_key_hmac=?",
                    (now, now + 18000, now, _session_name(session), key),
                )
            connection.commit()
        finally:
            connection.close()

    def _stored_send_result(self, row, session, chat_id):
        result = {
            "request_id": row["client_request_id"],
            "state": row["state"],
            "kind": row["kind"],
        }
        if row.get("error_code"):
            result["error_code"] = row["error_code"]
        if row.get("waha_message_id"):
            result["message_ref"] = self._encode_message(session, chat_id, row["waha_message_id"])
        return result

    @staticmethod
    def _error_state(error):
        if isinstance(error, (TimeoutError, ConnectionError, OSError)) or getattr(error, "status", None) == 0:
            return "UNKNOWN", "WAHA_SEND_UNKNOWN"
        return "FAILED", "WAHA_SEND_FAILED"

    def send_text(self, session, chat_ref, text, client_request_id):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        message = str(text or "").strip()
        if not message:
            raise ChatSendError("请输入要发送的文字", "FAILED", "EMPTY_TEXT")
        if len(message) > 65535:
            raise ChatSendError("文字消息过长", "FAILED", "TEXT_TOO_LONG")
        request_id = self._request_id(client_request_id)
        payload_hash = self._payload_hash("text", message)
        existing = self._begin_send(name, chat_id, request_id, "text", payload_hash)
        if existing:
            return self._stored_send_result(existing, name, chat_id)
        try:
            response = self.client.send_text(name, chat_id, message)
            message_id = _value_id(response.get("id") if isinstance(response, dict) else response)
            self._finish_send(request_id, "SENT", message_id or None, session=name, chat_id=chat_id)
            self._notify_outbound(name, chat_id, message_id, message, request_id)
            result = {"request_id": request_id, "state": "SENT", "kind": "text"}
            if message_id:
                result["message_ref"] = self._encode_message(name, chat_id, message_id)
            return result
        except Exception as error:
            state, code = self._error_state(error)
            self._finish_send(request_id, state, error_code=code)
            raise ChatSendError("文字消息发送失败，请先刷新聊天记录确认状态", state, code) from error

    def send_automated_text(self, session, chat_id, text, client_request_id):
        name = _session_name(session)
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            raise ChatAccessError("聊天编号无效")
        message = str(text or "").strip()
        if not message:
            raise ChatSendError("请输入要发送的文字", "FAILED", "EMPTY_TEXT")
        if len(message) > 65535:
            raise ChatSendError("文字消息过长", "FAILED", "TEXT_TOO_LONG")
        request_id = self._request_id(client_request_id)
        payload_hash = self._payload_hash("automated_text", message)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM automated_send_requests WHERE client_request_id=?", (request_id,)
            ).fetchone()
            if row:
                if (row["session_name"], row["chat_id"], row["payload_hash"]) != (name, chat_id, payload_hash):
                    raise SendConflictError("发送请求编号已用于其他内容")
                connection.commit()
                if row["state"] == "SENT":
                    result = {"request_id": request_id, "state": "SENT", "kind": "text"}
                    if row["waha_message_id"]:
                        result["message_ref"] = self._encode_message(name, chat_id, row["waha_message_id"])
                    return result
                state = "UNKNOWN" if row["state"] == "PENDING" else row["state"]
                raise ChatSendError("文字消息发送失败，请先刷新聊天记录确认状态", state, row["error_code"] or "WAHA_SEND_UNKNOWN")
            connection.execute(
                "INSERT INTO automated_send_requests(client_request_id,session_name,chat_id,payload_hash,state,waha_message_id,error_code,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (request_id, name, chat_id, payload_hash, "PENDING", None, None, int(self.clock())),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally: connection.close()
        try:
            response = self.client.send_text(name, chat_id, message)
            message_id = _value_id(response.get("id") if isinstance(response, dict) else response)
            result = {"request_id": request_id, "state": "SENT", "kind": "text"}
            if message_id:
                result["message_ref"] = self._encode_message(name, chat_id, message_id)
            connection = sqlite3.connect(self.database_path)
            try:
                connection.execute("UPDATE automated_send_requests SET state='SENT',waha_message_id=?,updated_at=? WHERE client_request_id=?", (message_id or None,int(self.clock()),request_id)); connection.commit()
            finally: connection.close()
            self._notify_outbound(name, chat_id, message_id, message, request_id)
            return result
        except Exception as error:
            state, code = self._error_state(error)
            connection = sqlite3.connect(self.database_path)
            try:
                connection.execute("UPDATE automated_send_requests SET state=?,error_code=?,updated_at=? WHERE client_request_id=?", (state,code,int(self.clock()),request_id)); connection.commit()
            finally: connection.close()
            raise ChatSendError("文字消息发送失败，请先刷新聊天记录确认状态", state, code) from error

    @staticmethod
    def _label_value(value):
        value = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
        if not value or len(value) > 100:
            raise ChatServiceError("标签长度必须为 1-100 个字符", "INVALID_LABEL")
        return value

    def customer_labels(self, session, chat_ref):
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute("SELECT source,label FROM customer_labels WHERE session_name=? AND chat_key_hmac=? ORDER BY id", (name, key)).fetchall()
        finally: connection.close()
        result = {"manual": [], "ai": []}
        for source, label in rows: result["manual" if source == "MANUAL" else "ai"].append(label)
        return result

    def customer_label_records(self, session, chat_ref):
        """Return labels with stable numeric IDs for HTTP resource identity."""
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT id,source,label FROM customer_labels "
                "WHERE session_name=? AND chat_key_hmac=? ORDER BY id",
                (name, key),
            ).fetchall()
        finally:
            connection.close()
        result = {"manual": [], "ai": []}
        for label_id, source, label in rows:
            result["manual" if source == "MANUAL" else "ai"].append(
                {"id": int(label_id), "label": label}
            )
        return result

    def add_manual_label(self, session, chat_ref, label, source="MANUAL"):
        if str(source or "").upper() != "MANUAL":
            raise ChatServiceError("只能修改人工标签", "INVALID_LABEL_SOURCE")
        return self._label_mutation(session, chat_ref, label, "add")

    def update_manual_label(self, session, chat_ref, old_label, new_label=None, source="MANUAL"):
        if str(source or "").upper() != "MANUAL":
            raise ChatServiceError("只能修改人工标签", "INVALID_LABEL_SOURCE")
        return self._label_mutation(session, chat_ref, new_label, "update", old_label)

    def delete_manual_label(self, session, chat_ref, label, source="MANUAL"):
        if str(source or "").upper() != "MANUAL":
            raise ChatServiceError("只能修改人工标签", "INVALID_LABEL_SOURCE")
        return self._label_mutation(session, chat_ref, label, "delete")

    @staticmethod
    def _label_id(value):
        try:
            label_id = int(str(value).strip())
        except (TypeError, ValueError) as error:
            raise ChatServiceError("标签编号无效", "INVALID_LABEL_ID") from error
        if label_id <= 0:
            raise ChatServiceError("标签编号无效", "INVALID_LABEL_ID")
        return label_id

    def update_manual_label_by_id(self, session, chat_ref, label_id, new_label, source="MANUAL"):
        if str(source or "").upper() != "MANUAL":
            raise ChatServiceError("只能修改人工标签", "INVALID_LABEL_SOURCE")
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        value = self._label_value(new_label)
        label_id = self._label_id(label_id)
        connection = sqlite3.connect(self.database_path); now = int(self.clock())
        try:
            changed = connection.execute(
                "UPDATE customer_labels SET label=?,updated_at=? WHERE id=? AND session_name=? "
                "AND chat_key_hmac=? AND source='MANUAL'",
                (value, now, label_id, name, key),
            ).rowcount
            if not changed:
                raise ChatServiceError("标签不存在", "LABEL_NOT_FOUND")
            connection.commit()
        finally:
            connection.close()
        return self.customer_label_records(name, chat_ref)

    def delete_manual_label_by_id(self, session, chat_ref, label_id, source="MANUAL"):
        if str(source or "").upper() != "MANUAL":
            raise ChatServiceError("只能修改人工标签", "INVALID_LABEL_SOURCE")
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        label_id = self._label_id(label_id)
        connection = sqlite3.connect(self.database_path)
        try:
            changed = connection.execute(
                "DELETE FROM customer_labels WHERE id=? AND session_name=? AND chat_key_hmac=? "
                "AND source='MANUAL'",
                (label_id, name, key),
            ).rowcount
            if not changed:
                raise ChatServiceError("标签不存在", "LABEL_NOT_FOUND")
            connection.commit()
        finally:
            connection.close()
        return self.customer_label_records(name, chat_ref)

    def _label_mutation(self, session, chat_ref, label, action, old_label=None):
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        value = self._label_value(label)
        connection = sqlite3.connect(self.database_path); now = int(self.clock())
        try:
            if action == "add":
                connection.execute("INSERT OR IGNORE INTO customer_labels(session_name,chat_key_hmac,source,label,created_at,updated_at) VALUES (?,?, 'MANUAL',?,?,?)", (name,key,value,now,now))
            elif action == "delete":
                connection.execute("DELETE FROM customer_labels WHERE session_name=? AND chat_key_hmac=? AND source='MANUAL' AND label=?", (name,key,value))
            else:
                old = self._label_value(old_label)
                connection.execute("UPDATE customer_labels SET label=?,updated_at=? WHERE session_name=? AND chat_key_hmac=? AND source='MANUAL' AND label=?", (value,now,name,key,old))
            connection.commit()
        finally: connection.close()
        return self.customer_labels(name, chat_ref)

    def _summary_ciphertext(self, summary):
        cipher = getattr(self.codec, "cipher", None)
        if cipher is None or not hasattr(cipher, "encrypt_json"):
            raise ChatServiceError("摘要加密服务不可用", "SUMMARY_ENCRYPTION_UNAVAILABLE")
        return cipher.encrypt_json(summary)

    def _summary_plaintext(self, value):
        cipher = getattr(self.codec, "cipher", None)
        if cipher is None or not hasattr(cipher, "decrypt_json"):
            raise ChatServiceError("摘要加密服务不可用", "SUMMARY_ENCRYPTION_UNAVAILABLE")
        return cipher.decrypt_json(value)

    def current_summary(self, session, chat_ref):
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id)
        connection = sqlite3.connect(self.database_path)
        try: row = connection.execute("SELECT summary_ciphertext,updated_at FROM conversation_summaries WHERE session_name=? AND chat_key_hmac=?", (name,key)).fetchone()
        finally: connection.close()
        if not row:
            return None
        summary = self._summary_plaintext(row[0])
        if not isinstance(summary, dict):
            raise ChatServiceError("摘要格式无效", "INVALID_SUMMARY")
        result = dict(summary)
        result["updated_at"] = int(row[1])
        return result

    def save_summary_and_ai_labels(self, session, chat_ref, summary, ai_labels):
        if not isinstance(summary, dict): raise ChatServiceError("摘要格式无效", "INVALID_SUMMARY")
        name = _session_name(session); chat_id = self._decode_chat(name, chat_ref); key = self._chat_key(name, chat_id); now = int(self.clock())
        labels = [self._label_value(item) for item in (ai_labels or [])]
        encrypted = self._summary_ciphertext(summary)
        fingerprint = hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("BEGIN")
            connection.execute("INSERT INTO conversation_summaries(session_name,chat_key_hmac,summary_ciphertext,message_fingerprint,model_fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_name,chat_key_hmac) DO UPDATE SET summary_ciphertext=excluded.summary_ciphertext,message_fingerprint=excluded.message_fingerprint,updated_at=excluded.updated_at", (name,key,encrypted,fingerprint,"",now,now))
            connection.execute("DELETE FROM customer_labels WHERE session_name=? AND chat_key_hmac=? AND source='AI'", (name,key))
            connection.executemany("INSERT OR IGNORE INTO customer_labels(session_name,chat_key_hmac,source,label,created_at,updated_at) VALUES (?,?, 'AI',?,?,?)", [(name,key,label,now,now) for label in labels])
            connection.commit()
        except Exception:
            connection.rollback(); raise
        finally: connection.close()
        return self.current_summary(name, chat_ref)

    @staticmethod
    def _validated_image(filename, declared_type, data):
        if not isinstance(data, bytes) or not data:
            raise ImageValidationError("请选择图片文件")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageValidationError("图片不能超过 10MB", "IMAGE_TOO_LARGE")
        original_name = str(filename or "").replace("\x00", "")
        safe_name = Path(original_name).name
        if not safe_name or safe_name != original_name or len(safe_name) > 255:
            raise ImageValidationError("图片文件名无效")
        extension = Path(safe_name).suffix.lower()
        supplied_type = str(declared_type or "").lower().strip()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise ImageValidationError("图片像素尺寸超过限制", "IMAGE_DIMENSIONS_TOO_LARGE")
                    image.verify()
        except ImageValidationError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
                OSError, ValueError) as error:
            raise ImageValidationError("图片内容无法识别") from error
        expected = IMAGE_TYPES.get(image_format)
        if not expected or supplied_type != expected[0] or extension not in expected[1]:
            raise ImageValidationError("图片格式、扩展名与文件内容不一致")
        return safe_name, expected[0]

    def send_image(self, session, chat_ref, filename, declared_type, data, caption,
                   client_request_id):
        name = _session_name(session)
        chat_id = self._decode_chat(name, chat_ref)
        safe_name, mimetype = self._validated_image(filename, declared_type, data)
        safe_caption = str(caption or "").strip()
        if len(safe_caption) > 4096:
            raise ImageValidationError("图片说明文字过长")
        request_id = self._request_id(client_request_id)
        payload_hash = self._payload_hash("image", data, safe_caption)
        existing = self._begin_send(name, chat_id, request_id, "image", payload_hash)
        if existing:
            return self._stored_send_result(existing, name, chat_id)
        encoded = base64.b64encode(data).decode("ascii")
        try:
            response = self.client.send_image(
                name, chat_id, safe_name, mimetype, encoded, safe_caption
            )
            message_id = _value_id(response.get("id") if isinstance(response, dict) else response)
            self._finish_send(request_id, "SENT", message_id or None, session=name, chat_id=chat_id)
            self._notify_outbound(
                name,
                chat_id,
                message_id,
                format_media_context(
                    "outbound",
                    "image",
                    filename=safe_name,
                    mime=mimetype,
                    caption=safe_caption,
                ),
                request_id,
            )
            result = {"request_id": request_id, "state": "SENT", "kind": "image"}
            if message_id:
                result["message_ref"] = self._encode_message(name, chat_id, message_id)
            return result
        except Exception as error:
            state, code = self._error_state(error)
            self._finish_send(request_id, state, error_code=code)
            raise ChatSendError("图片发送失败，请先刷新聊天记录确认状态", state, code) from error
