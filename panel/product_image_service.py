"""Storage and validation for the administrator-managed product image library."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_TYPES = {
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}, ".jpg"),
    "PNG": ("image/png", {".png"}, ".png"),
    "WEBP": ("image/webp", {".webp"}, ".webp"),
}
TAG_LIMIT = 120
TITLE_LIMIT = 200
CAPTION_LIMIT = 4096
PRODUCT_NAME_LIMIT = 200
SKU_LIMIT = 120
ALIAS_LIMIT = 120


class ProductImageError(RuntimeError):
    code = "PRODUCT_IMAGE_ERROR"

    def __init__(self, message, code=None):
        self.public_message = str(message)
        if code:
            self.code = str(code)
        super().__init__(self.public_message)


class ProductImageValidationError(ValueError, ProductImageError):
    code = "INVALID_PRODUCT_IMAGE"


class ProductImageNotFoundError(ProductImageError):
    code = "PRODUCT_IMAGE_NOT_FOUND"


def _truthy(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ProductImageValidationError("自动发送开关参数无效", "INVALID_SETTINGS")


def normalize_tag(value):
    """Normalize a tag for exact, case-insensitive matching."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    if len(text) > TAG_LIMIT:
        raise ProductImageValidationError("产品标签过长", "TAG_TOO_LONG")
    return text


def display_tag(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


class ProductImageService:
    def __init__(self, database_path, data_root, clock=None, logger=None):
        self.database_path = Path(database_path)
        self.data_root = Path(data_root)
        # Callers may pass either the panel data root or its product-images
        # child.  Keep the relative database path stable in both cases.
        if self.data_root.name == "product-images":
            self.storage_root = self.data_root.parent
            self.image_root = self.data_root
        else:
            self.storage_root = self.data_root
            self.image_root = self.data_root / "product-images"
        self.clock = clock or time.time
        self.logger = logger
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.image_root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self):
        migration_root = Path(__file__).with_name("migrations")
        with self._db() as connection:
            connection.executescript(
                (migration_root / "014_product_image_library.sql").read_text(encoding="utf-8")
            )
            connection.executescript(
                (migration_root / "019_product_image_matching.sql").read_text(encoding="utf-8")
            )
            connection.executescript(
                (migration_root / "021_product_catalog.sql").read_text(encoding="utf-8")
            )
            connection.executescript((migration_root / "022_product_image_matching.sql").read_text(encoding="utf-8"))
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(product_image_assets)")
            }
            for definition in (
                "product_name TEXT NOT NULL DEFAULT ''",
                "sku TEXT NOT NULL DEFAULT ''",
                "aliases_json TEXT NOT NULL DEFAULT '[]'",
                "product_id TEXT REFERENCES product_catalog(product_id) ON DELETE SET NULL",
                "variant TEXT NOT NULL DEFAULT ''",
                "reference_role TEXT NOT NULL DEFAULT 'primary'",
                "allow_visual_match INTEGER NOT NULL DEFAULT 1",
                "allow_auto_send INTEGER NOT NULL DEFAULT 1",
                "legacy_import_status TEXT NOT NULL DEFAULT 'pending'",
            ):
                name = definition.split(" ", 1)[0]
                if name not in columns:
                    connection.execute("ALTER TABLE product_image_assets ADD COLUMN " + definition)

    def _log(self, level, message):
        if not self.logger:
            return
        try:
            self.logger(level, message)
        except TypeError:
            try:
                self.logger(message)
            except Exception:
                pass
        except Exception:
            pass

    def _now(self):
        return int(self.clock())

    @staticmethod
    def _safe_filename(filename):
        original = str(filename or "").replace("\x00", "")
        safe = Path(original).name
        if not safe or safe != original or "/" in original or "\\" in original or len(safe) > 255:
            raise ProductImageValidationError("图片文件名无效", "INVALID_FILENAME")
        return safe

    @classmethod
    def _validate_bytes(cls, filename, declared_type, data):
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ProductImageValidationError("请选择图片文件", "EMPTY_IMAGE")
        raw = bytes(data)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ProductImageValidationError("图片不能超过 10MB", "IMAGE_TOO_LARGE")
        safe_name = cls._safe_filename(filename)
        extension = Path(safe_name).suffix.lower()
        supplied_type = str(declared_type or "").strip().lower()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    image_format = str(image.format or "").upper()
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise ProductImageValidationError(
                            "图片像素尺寸超过限制", "IMAGE_DIMENSIONS_TOO_LARGE"
                        )
                    image.verify()
        except ProductImageValidationError:
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as error:
            raise ProductImageValidationError("图片内容无法识别", "INVALID_IMAGE_CONTENT") from error
        expected = IMAGE_TYPES.get(image_format)
        if not expected or supplied_type != expected[0] or extension not in expected[1]:
            raise ProductImageValidationError(
                "图片格式、扩展名与文件内容不一致", "IMAGE_TYPE_MISMATCH"
            )
        return raw, safe_name, expected[0], expected[2], int(width), int(height)

    @staticmethod
    def _clean_title(value, fallback=""):
        title = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not title:
            title = fallback
        if not title:
            raise ProductImageValidationError("图片标题不能为空", "TITLE_REQUIRED")
        if len(title) > TITLE_LIMIT:
            raise ProductImageValidationError("图片标题过长", "TITLE_TOO_LONG")
        return title

    @staticmethod
    def _clean_caption(value):
        caption = str(value or "").replace("\x00", "").strip()
        if len(caption) > CAPTION_LIMIT:
            raise ProductImageValidationError("图片说明过长", "CAPTION_TOO_LONG")
        return caption

    @staticmethod
    def _clean_match_text(value, field, limit):
        text = display_tag(value)
        if len(text) > limit:
            raise ProductImageValidationError(f"{field}过长", "MATCH_METADATA_TOO_LONG")
        return text

    @classmethod
    def _clean_match_metadata(cls, product_name="", sku="", aliases=()):
        cleaned_name = cls._clean_match_text(product_name, "产品名称", PRODUCT_NAME_LIMIT)
        cleaned_sku = cls._clean_match_text(sku, "SKU", SKU_LIMIT)
        if aliases is None:
            aliases = []
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, (list, tuple, set)):
            raise ProductImageValidationError("产品别名格式无效", "INVALID_ALIASES")
        cleaned_aliases = []
        seen = set()
        for alias in aliases:
            label = cls._clean_match_text(alias, "产品别名", ALIAS_LIMIT)
            key = normalize_tag(label)
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned_aliases.append(label)
        return cleaned_name, cleaned_sku, cleaned_aliases

    @classmethod
    def _decode_aliases(cls, value):
        try:
            aliases = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            aliases = []
        try:
            _, _, cleaned = cls._clean_match_metadata(aliases=aliases)
        except ProductImageValidationError:
            cleaned = []
        return cleaned

    @classmethod
    def _clean_tags(cls, values):
        if isinstance(values, str):
            values = [values]
        if values is None:
            values = []
        if not isinstance(values, (list, tuple, set)):
            raise ProductImageValidationError("产品标签格式无效", "INVALID_TAGS")
        cleaned = []
        seen = set()
        for value in values:
            label = display_tag(value)
            key = normalize_tag(label)
            if not key:
                continue
            if key not in seen:
                cleaned.append((key, label))
                seen.add(key)
        if not cleaned:
            raise ProductImageValidationError("至少需要一个产品标签", "TAGS_REQUIRED")
        return cleaned

    @staticmethod
    def _asset_dict(row, tags=None, reused=False):
        if row is None:
            return None
        item = {
            "asset_id": str(row["id"]),
            "title": str(row["title"]),
            "caption_template": str(row["caption_template"] or ""),
            "relative_path": str(row["relative_path"]),
            "original_filename": str(row["original_filename"]),
            "mime_type": str(row["mime_type"]),
            "byte_size": int(row["byte_size"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "sha256": str(row["sha256"]),
            "active": bool(row["active"]),
            "is_active": bool(row["active"]),
            "sort_order": int(row["sort_order"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "tags": list(tags or []),
            "product_name": str(row["product_name"] or ""),
            "sku": str(row["sku"] or ""),
            "aliases": ProductImageService._decode_aliases(row["aliases_json"]),
            "product_id": str(row["product_id"] or "") if "product_id" in row.keys() else "",
            "variant": str(row["variant"] or "") if "variant" in row.keys() else "",
            "reference_role": str(row["reference_role"] or "primary") if "reference_role" in row.keys() else "primary",
            "allow_visual_match": bool(row["allow_visual_match"]) if "allow_visual_match" in row.keys() else True,
            "allow_auto_send": bool(row["allow_auto_send"]) if "allow_auto_send" in row.keys() else True,
        }
        if reused:
            item["reused"] = True
        return item

    def _load_asset(self, connection, asset_id, include_inactive=False):
        row = connection.execute(
            "SELECT * FROM product_image_assets WHERE id=?" + ("" if include_inactive else " AND active=1"),
            (str(asset_id),),
        ).fetchone()
        if not row:
            raise ProductImageNotFoundError("图片不存在或已停用")
        tags = [
            {
                "tag_key": str(item[0]),
                "display_name": str(item[1]),
                "priority": int(item[2]),
                "is_primary": bool(item[3]),
            }
            for item in connection.execute(
                "SELECT t.tag_key,t.display_name,at.priority,at.is_primary "
                "FROM product_image_asset_tags at JOIN product_image_tags t ON t.id=at.tag_id "
                "WHERE at.asset_id=? ORDER BY at.priority,t.tag_key",
                (str(asset_id),),
            )
        ]
        return self._asset_dict(row, tags=tags)

    def _ensure_tag(self, connection, key, label, now):
        connection.execute(
            "INSERT INTO product_image_tags(tag_key,display_name,created_at,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(tag_key) DO UPDATE SET display_name=excluded.display_name,updated_at=excluded.updated_at",
            (key, label, now, now),
        )
        return connection.execute(
            "SELECT id FROM product_image_tags WHERE tag_key=?", (key,)
        ).fetchone()[0]

    def settings_payload(self):
        with self._db() as connection:
            row = connection.execute(
                "SELECT auto_send_enabled,daily_limit FROM product_image_settings WHERE id=1"
            ).fetchone()
            if not row:
                now = self._now()
                connection.execute(
                    "INSERT INTO product_image_settings(id,auto_send_enabled,daily_limit,updated_at) VALUES (1,1,20,?)",
                    (now,),
                )
                return {"auto_send_enabled": True, "daily_limit": 20}
            return {"auto_send_enabled": bool(row[0]), "daily_limit": int(row[1])}

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise ProductImageValidationError("图片库设置格式无效", "INVALID_SETTINGS")
        current = self.settings_payload()
        enabled = _truthy(payload.get("auto_send_enabled"), current["auto_send_enabled"])
        raw_limit = payload.get("daily_limit", current["daily_limit"])
        try:
            daily_limit = int(raw_limit)
        except (TypeError, ValueError) as error:
            raise ProductImageValidationError("每日上限必须是整数", "INVALID_DAILY_LIMIT") from error
        if daily_limit < 1 or daily_limit > 1000:
            raise ProductImageValidationError("每日上限必须在 1 至 1000 之间", "INVALID_DAILY_LIMIT")
        now = self._now()
        with self._db() as connection:
            connection.execute(
                "INSERT INTO product_image_settings(id,auto_send_enabled,daily_limit,updated_at) VALUES (1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET auto_send_enabled=excluded.auto_send_enabled,daily_limit=excluded.daily_limit,updated_at=excluded.updated_at",
                (1 if enabled else 0, daily_limit, now),
            )
        return {"auto_send_enabled": enabled, "daily_limit": daily_limit}

    def list_tags(self):
        with self._db() as connection:
            rows = connection.execute(
                "SELECT t.id,t.tag_key,t.display_name,t.created_at,t.updated_at,COUNT(at.asset_id) AS asset_count "
                "FROM product_image_tags t LEFT JOIN product_image_asset_tags at ON at.tag_id=t.id "
                "GROUP BY t.id ORDER BY t.tag_key"
            ).fetchall()
        return {
            "items": [
                {
                    "id": int(row["id"]),
                    "tag_key": str(row["tag_key"]),
                    "display_name": str(row["display_name"]),
                    "asset_count": int(row["asset_count"]),
                    "created_at": int(row["created_at"]),
                    "updated_at": int(row["updated_at"]),
                }
                for row in rows
            ]
        }

    def create_tag(self, payload):
        if not isinstance(payload, dict):
            raise ProductImageValidationError("标签数据格式无效", "INVALID_TAG")
        label = display_tag(payload.get("display_name") or payload.get("tag") or payload.get("tag_key"))
        key = normalize_tag(payload.get("tag_key") or label)
        if not key or not label:
            raise ProductImageValidationError("产品标签不能为空", "TAGS_REQUIRED")
        now = self._now()
        with self._db() as connection:
            try:
                connection.execute(
                    "INSERT INTO product_image_tags(tag_key,display_name,created_at,updated_at) VALUES (?,?,?,?)",
                    (key, label, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise ProductImageValidationError("产品标签已存在", "TAG_EXISTS") from error
            row = connection.execute(
                "SELECT id,tag_key,display_name,created_at,updated_at FROM product_image_tags WHERE tag_key=?",
                (key,),
            ).fetchone()
        return {
            "id": int(row["id"]),
            "tag_key": str(row["tag_key"]),
            "display_name": str(row["display_name"]),
            "asset_count": 0,
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def update_tag(self, tag_id, payload):
        if not isinstance(payload, dict):
            raise ProductImageValidationError("标签数据格式无效", "INVALID_TAG")
        now = self._now()
        with self._db() as connection:
            current = connection.execute(
                "SELECT * FROM product_image_tags WHERE id=?", (int(tag_id),)
            ).fetchone()
            if not current:
                raise ProductImageNotFoundError("标签不存在", "TAG_NOT_FOUND")
            label = display_tag(payload.get("display_name", current["display_name"]))
            key = normalize_tag(payload.get("tag_key", current["tag_key"]))
            if not key or not label:
                raise ProductImageValidationError("产品标签不能为空", "TAGS_REQUIRED")
            try:
                connection.execute(
                    "UPDATE product_image_tags SET tag_key=?,display_name=?,updated_at=? WHERE id=?",
                    (key, label, now, int(tag_id)),
                )
            except sqlite3.IntegrityError as error:
                raise ProductImageValidationError("产品标签已存在", "TAG_EXISTS") from error
            row = connection.execute(
                "SELECT t.id,t.tag_key,t.display_name,t.created_at,t.updated_at,COUNT(at.asset_id) AS asset_count "
                "FROM product_image_tags t LEFT JOIN product_image_asset_tags at ON at.tag_id=t.id "
                "WHERE t.id=? GROUP BY t.id",
                (int(tag_id),),
            ).fetchone()
        return {
            "id": int(row["id"]),
            "tag_key": str(row["tag_key"]),
            "display_name": str(row["display_name"]),
            "asset_count": int(row["asset_count"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def delete_tag(self, tag_id):
        with self._db() as connection:
            row = connection.execute(
                "SELECT id FROM product_image_tags WHERE id=?", (int(tag_id),)
            ).fetchone()
            if not row:
                raise ProductImageNotFoundError("标签不存在", "TAG_NOT_FOUND")
            connection.execute("DELETE FROM product_image_tags WHERE id=?", (int(tag_id),))
        return {"deleted": True, "id": int(tag_id)}

    def upload(
        self,
        filename,
        declared_type,
        data,
        title,
        tags,
        caption_template="",
        product_name="",
        sku="",
        aliases=(),
        product_id="", variant="", reference_role="primary", allow_visual_match=True, allow_auto_send=True,
    ):
        raw, safe_name, mime_type, extension, width, height = self._validate_bytes(
            filename, declared_type, data
        )
        cleaned_title = self._clean_title(title, Path(safe_name).stem)
        cleaned_caption = self._clean_caption(caption_template)
        cleaned_tags = self._clean_tags(tags)
        cleaned_name, cleaned_sku, cleaned_aliases = self._clean_match_metadata(
            product_name, sku, aliases
        )
        product_id = str(product_id or "").strip()
        variant = str(variant or "").strip()
        reference_role = str(reference_role or "primary").strip().casefold()
        if reference_role not in {"primary", "reference", "marketing"}:
            raise ProductImageValidationError("图片角色无效", "INVALID_REFERENCE_ROLE")
        allow_visual_match = _truthy(allow_visual_match, True)
        allow_auto_send = _truthy(allow_auto_send, True)
        sha256 = hashlib.sha256(raw).hexdigest()
        now = self._now()
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM product_image_assets WHERE sha256=?", (sha256,)
            ).fetchone()
            if existing:
                asset = self._load_asset(connection, existing["id"], include_inactive=True)
                incoming_id = str(product_id or "").strip()
                current_id = str(asset.get("product_id") or "").strip()
                if current_id and incoming_id and current_id != incoming_id:
                    raise ProductImageValidationError(
                        "相同图片已绑定其他产品，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                    )
                for field, incoming, current in (
                    ("product_name", cleaned_name, asset.get("product_name", "")),
                    ("sku", cleaned_sku, asset.get("sku", "")),
                ):
                    if incoming and current and normalize_tag(incoming) != normalize_tag(current):
                        raise ProductImageValidationError(
                            f"相同图片的{field}元数据不一致，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                        )
                if cleaned_title and str(asset.get("title") or "").strip() and cleaned_title != str(asset.get("title") or "").strip():
                    raise ProductImageValidationError(
                        "相同图片标题不一致，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                    )
                if cleaned_caption and str(asset.get("caption_template") or "").strip() and cleaned_caption != str(asset.get("caption_template") or "").strip():
                    raise ProductImageValidationError(
                        "相同图片说明不一致，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                    )
                incoming_variant = str(variant or "").strip()
                current_variant = str(asset.get("variant") or "").strip()
                if current_variant and incoming_variant and current_variant != incoming_variant:
                    raise ProductImageValidationError(
                        "相同图片已绑定其他变体，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                    )
                incoming_role = str(reference_role or "primary").strip().casefold()
                current_role = str(asset.get("reference_role") or "primary").strip().casefold()
                if current_role != "primary" and incoming_role != "primary" and current_role != incoming_role:
                    raise ProductImageValidationError(
                        "相同图片已绑定其他图片角色，无法安全关联", "DUPLICATE_IMAGE_CONFLICT"
                    )
                merged_name = str(asset.get("product_name") or cleaned_name)
                merged_sku = str(asset.get("sku") or cleaned_sku)
                merged_aliases = list(asset.get("aliases") or [])
                seen_aliases = {normalize_tag(value) for value in merged_aliases}
                for alias in cleaned_aliases:
                    key = normalize_tag(alias)
                    if key and key not in seen_aliases:
                        merged_aliases.append(alias)
                        seen_aliases.add(key)
                merged_id = current_id or incoming_id or None
                merged_variant = current_variant or incoming_variant
                merged_role = current_role if current_role != "primary" else incoming_role
                merged_visual = bool(asset.get("allow_visual_match", True)) and bool(allow_visual_match)
                merged_auto = bool(asset.get("allow_auto_send", True)) and bool(allow_auto_send)
                merged_title = str(asset.get("title") or cleaned_title)
                merged_caption = str(asset.get("caption_template") or cleaned_caption)
                connection.execute(
                    "UPDATE product_image_assets SET title=?,caption_template=?,product_name=?,sku=?,aliases_json=?,product_id=?,variant=?,reference_role=?,allow_visual_match=?,allow_auto_send=?,updated_at=? WHERE id=?",
                    (
                        merged_title,
                        merged_caption,
                        merged_name,
                        merged_sku,
                        json.dumps(merged_aliases, ensure_ascii=False, separators=(",", ":")),
                        merged_id,
                        merged_variant,
                        merged_role,
                        int(merged_visual),
                        int(merged_auto),
                        now,
                        existing["id"],
                    ),
                )
                existing_tag_keys = {str(tag["tag_key"]) for tag in asset.get("tags", [])}
                for index, (key, label) in enumerate(cleaned_tags):
                    tag_id = self._ensure_tag(connection, key, label, now)
                    if key not in existing_tag_keys:
                        connection.execute(
                            "INSERT INTO product_image_asset_tags(asset_id,tag_id,priority,is_primary,created_at) VALUES (?,?,?,?,?)",
                            (existing["id"], tag_id, (len(existing_tag_keys) + index) * 10 + 100, 0, now),
                        )
                asset = self._load_asset(connection, existing["id"], include_inactive=True)
                asset["reused"] = True
                return asset
            asset_id = str(uuid.uuid4())
            relative_path = str(Path("product-images") / sha256[:2] / f"{asset_id}{extension}")
            target = self.storage_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=".upload-", suffix=".tmp", delete=False
                ) as handle:
                    temp_path = Path(handle.name)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, target)
                connection.execute(
                    "INSERT INTO product_image_assets(id,title,caption_template,relative_path,original_filename,mime_type,byte_size,width,height,sha256,active,sort_order,created_at,updated_at,product_name,sku,aliases_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        asset_id,
                        cleaned_title,
                        cleaned_caption,
                        relative_path,
                        safe_name,
                        mime_type,
                        len(raw),
                        width,
                        height,
                        sha256,
                        1,
                        0,
                        now,
                        now,
                        cleaned_name,
                        cleaned_sku,
                        json.dumps(cleaned_aliases, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                connection.execute("UPDATE product_image_assets SET product_id=?,variant=?,reference_role=?,allow_visual_match=?,allow_auto_send=? WHERE id=?", (str(product_id or "") or None, variant, reference_role, int(_truthy(allow_visual_match, True)), int(_truthy(allow_auto_send, True)), asset_id))
                for index, (key, label) in enumerate(cleaned_tags):
                    tag_id = self._ensure_tag(connection, key, label, now)
                    connection.execute(
                        "INSERT INTO product_image_asset_tags(asset_id,tag_id,priority,is_primary,created_at) VALUES (?,?,?,?,?)",
                        (asset_id, tag_id, index * 10 + 100, 1, now),
                    )
                asset = self._load_asset(connection, asset_id, include_inactive=True)
            except Exception:
                if temp_path and temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                if target.exists():
                    target.unlink(missing_ok=True)
                raise
        self._log("INFO", f"product_image.uploaded asset={asset_id[:12]}")
        return asset

    def list_assets(self, tag="", include_inactive=False):
        normalized = normalize_tag(tag) if str(tag or "").strip() else ""
        query = (
            "SELECT DISTINCT a.* FROM product_image_assets a "
            "LEFT JOIN product_image_asset_tags at ON at.asset_id=a.id "
            "LEFT JOIN product_image_tags t ON t.id=at.tag_id WHERE 1=1"
        )
        params = []
        if not include_inactive:
            query += " AND a.active=1"
        if normalized:
            query += " AND t.tag_key=?"
            params.append(normalized)
        query += " ORDER BY a.sort_order ASC,a.created_at DESC,a.id"
        with self._db() as connection:
            rows = connection.execute(query, params).fetchall()
            items = [self._load_asset(connection, row["id"], include_inactive=True) for row in rows]
        return {"items": items, "total": len(items)}

    def get_asset(self, asset_id, include_inactive=False):
        with self._db() as connection:
            return self._load_asset(connection, asset_id, include_inactive=include_inactive)

    def _safe_path(self, relative_path):
        root = self.storage_root.resolve()
        path = (self.storage_root / str(relative_path)).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ProductImageError("图片路径无效", "INVALID_IMAGE_PATH") from error
        return path

    def _read_verified(self, asset):
        path = self._safe_path(asset["relative_path"])
        if not path.is_file():
            raise ProductImageNotFoundError("图片文件不存在")
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ProductImageNotFoundError("图片文件无法读取") from error
        if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
            raise ProductImageError("图片校验失败", "IMAGE_HASH_MISMATCH")
        self._validate_bytes(asset["original_filename"], asset["mime_type"], raw)
        return raw

    def preview(self, asset_id):
        asset = self.get_asset(asset_id, include_inactive=True)
        return asset["mime_type"], self._read_verified(asset)

    def update_asset(self, asset_id, payload):
        if not isinstance(payload, dict):
            raise ProductImageValidationError("图片更新数据格式无效", "INVALID_ASSET")
        now = self._now()
        with self._db() as connection:
            current = self._load_asset(connection, asset_id, include_inactive=True)
            title = self._clean_title(payload.get("title", current["title"]), current["title"])
            caption = self._clean_caption(payload.get("caption_template", current["caption_template"]))
            product_name, sku, aliases = self._clean_match_metadata(
                payload.get("product_name", current.get("product_name", "")),
                payload.get("sku", current.get("sku", "")),
                payload.get("aliases", current.get("aliases", [])),
            )
            active = current["active"] if "active" not in payload else _truthy(payload["active"])
            product_id = str(payload.get("product_id", current.get("product_id", "")) or "") or None
            variant = str(payload.get("variant", current.get("variant", "")) or "").strip()
            reference_role = str(payload.get("reference_role", current.get("reference_role", "primary")) or "primary").strip().casefold()
            allow_visual_match = _truthy(payload.get("allow_visual_match", current.get("allow_visual_match", True)), True)
            allow_auto_send = _truthy(payload.get("allow_auto_send", current.get("allow_auto_send", True)), True)
            if reference_role not in {"primary", "reference", "marketing"}: raise ProductImageValidationError("图片角色无效", "INVALID_REFERENCE_ROLE")
            try:
                sort_order = int(payload.get("sort_order", current["sort_order"]))
            except (TypeError, ValueError) as error:
                raise ProductImageValidationError("排序值无效", "INVALID_SORT_ORDER") from error
            connection.execute(
                "UPDATE product_image_assets SET title=?,caption_template=?,active=?,sort_order=?,product_name=?,sku=?,aliases_json=?,product_id=?,variant=?,reference_role=?,allow_visual_match=?,allow_auto_send=?,updated_at=? WHERE id=?",
                (
                    title,
                    caption,
                    1 if active else 0,
                    sort_order,
                    product_name,
                    sku,
                    json.dumps(aliases, ensure_ascii=False, separators=(",", ":")),
                    product_id, variant, reference_role, int(allow_visual_match), int(allow_auto_send),
                    now,
                    str(asset_id),
                ),
            )
            if "tags" in payload:
                cleaned_tags = self._clean_tags(payload.get("tags"))
                connection.execute("DELETE FROM product_image_asset_tags WHERE asset_id=?", (str(asset_id),))
                connection.execute("UPDATE product_image_assets SET product_id=?,variant=?,reference_role=?,allow_visual_match=?,allow_auto_send=? WHERE id=?", (str(product_id or "") or None, variant, reference_role, int(_truthy(allow_visual_match, True)), int(_truthy(allow_auto_send, True)), asset_id))
                for index, (key, label) in enumerate(cleaned_tags):
                    tag_id = self._ensure_tag(connection, key, label, now)
                    connection.execute(
                        "INSERT INTO product_image_asset_tags(asset_id,tag_id,priority,is_primary,created_at) VALUES (?,?,?,?,?)",
                        (str(asset_id), tag_id, index * 10 + 100, 1, now),
                    )
            if "tag_updates" in payload:
                updates = payload.get("tag_updates")
                if not isinstance(updates, list):
                    raise ProductImageValidationError("标签更新格式无效", "INVALID_TAGS")
                for entry in updates:
                    if not isinstance(entry, dict):
                        raise ProductImageValidationError("标签更新格式无效", "INVALID_TAGS")
                    key = normalize_tag(entry.get("tag_key"))
                    if not key:
                        raise ProductImageValidationError("产品标签不能为空", "TAGS_REQUIRED")
                    row = connection.execute(
                        "SELECT id FROM product_image_tags WHERE tag_key=?", (key,)
                    ).fetchone()
                    if not row:
                        raise ProductImageValidationError("标签不存在", "TAG_NOT_FOUND")
                    fields = []
                    values = []
                    if "priority" in entry:
                        try:
                            priority = int(entry["priority"])
                        except (TypeError, ValueError) as error:
                            raise ProductImageValidationError("标签优先级无效", "INVALID_PRIORITY") from error
                        if priority < 0 or priority > 1000000:
                            raise ProductImageValidationError("标签优先级无效", "INVALID_PRIORITY")
                        fields.append("priority=?")
                        values.append(priority)
                    if "is_primary" in entry:
                        fields.append("is_primary=?")
                        values.append(1 if _truthy(entry["is_primary"]) else 0)
                    if fields:
                        values.extend([str(asset_id), row[0]])
                        connection.execute(
                            "UPDATE product_image_asset_tags SET " + ",".join(fields) + " WHERE asset_id=? AND tag_id=?",
                            values,
                        )
            result = self._load_asset(connection, asset_id, include_inactive=True)
        self._log("INFO", f"product_image.updated asset={str(asset_id)[:12]}")
        return result

    def disable_asset(self, asset_id):
        now = self._now()
        with self._db() as connection:
            connection.execute(
                "UPDATE product_image_assets SET active=0,updated_at=? WHERE id=?",
                (now, str(asset_id)),
            )
            if connection.total_changes <= 0:
                raise ProductImageNotFoundError("图片不存在")
            result = self._load_asset(connection, asset_id, include_inactive=True)
        self._log("INFO", f"product_image.disabled asset={str(asset_id)[:12]}")
        return result

    def _chat_key_hmac(self, session_name, chat_id):
        # Product-image storage does not receive the panel's chat cipher key.
        # A one-way digest keeps raw customer identifiers out of this ledger;
        # the chat service remains the owner of encrypted references.
        value = f"product-image-chat-v1\x00{session_name}\x00{chat_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _caption_hash(caption):
        return hashlib.sha256(str(caption or "").encode("utf-8")).hexdigest()

    def reserve_send(self, session_name, chat_id, message_key, asset_id, tag_key, caption, daily_limit=None):
        session = str(session_name or "").strip()
        customer = str(chat_id or "").strip()
        message = str(message_key or "").strip()
        asset = str(asset_id or "").strip()
        tag = normalize_tag(tag_key)
        safe_caption = self._clean_caption(caption)
        if not session or not customer or not message or len(message) > 512:
            raise ProductImageValidationError("图片发送请求参数无效", "INVALID_SEND_REQUEST")
        if not asset or not tag:
            raise ProductImageValidationError("图片发送目标无效", "INVALID_SEND_REQUEST")
        chat_key = self._chat_key_hmac(session, customer)
        caption_hash = self._caption_hash(safe_caption)
        payload_hash = hashlib.sha256(
            f"{asset}\x00{tag}\x00{caption_hash}".encode("utf-8")
        ).hexdigest()
        now = self._now()
        with self._db() as connection:
            # Serialize duplicate lookup, quota accounting, and reservation
            # insertion so concurrent workers cannot both pass the cap.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_id,state,waha_message_id,error_code,asset_id,tag_key,caption_hash,payload_hash "
                "FROM product_image_send_requests WHERE session_name=? AND chat_key_hmac=? AND message_key=?",
                (session, chat_key, message),
            ).fetchone()
            if existing:
                return {
                    "request_id": str(existing["request_id"]),
                    "state": str(existing["state"]),
                    "waha_message_id": existing["waha_message_id"],
                    "error_code": existing["error_code"],
                    "asset_id": str(existing["asset_id"]),
                    "tag_key": str(existing["tag_key"]),
                    "caption_hash": str(existing["caption_hash"]),
                    "payload_hash": str(existing["payload_hash"]),
                    "reserved": False,
                    "recovery_required": str(existing["state"]) in {"PENDING", "UNKNOWN"},
                    "reason_code": "RECOVERY_REQUIRED" if str(existing["state"]) in {"PENDING", "UNKNOWN"} else "ALREADY_PROCESSED",
                }
            if daily_limit is not None:
                try:
                    limit = int(daily_limit)
                except (TypeError, ValueError):
                    limit = 0
                if limit > 0:
                    day_start = now - (now % 86400)
                    sent = connection.execute(
                        "SELECT COUNT(*) FROM product_image_send_requests WHERE state IN ('SENT','PENDING','UNKNOWN') AND updated_at>=?",
                        (day_start,),
                    ).fetchone()[0]
                    if int(sent) >= limit:
                        return {"reserved": False, "state": "DAILY_LIMIT", "reason_code": "DAILY_LIMIT"}
            request_id = str(uuid.uuid4())
            connection.execute(
                "INSERT OR IGNORE INTO product_image_send_requests(request_id,session_name,chat_key_hmac,message_key,asset_id,tag_key,caption_hash,payload_hash,state,waha_message_id,error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    session,
                    chat_key,
                    message,
                    asset,
                    tag,
                    caption_hash,
                    payload_hash,
                    "PENDING",
                    None,
                    None,
                    now,
                    now,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] == 0:
                existing = connection.execute(
                    "SELECT request_id,state,waha_message_id,error_code,asset_id,tag_key,caption_hash,payload_hash "
                    "FROM product_image_send_requests WHERE session_name=? AND chat_key_hmac=? AND message_key=?",
                    (session, chat_key, message),
                ).fetchone()
                return {
                    "request_id": str(existing["request_id"]),
                    "state": str(existing["state"]),
                    "waha_message_id": existing["waha_message_id"],
                    "error_code": existing["error_code"],
                    "asset_id": str(existing["asset_id"]),
                    "tag_key": str(existing["tag_key"]),
                    "caption_hash": str(existing["caption_hash"]),
                    "payload_hash": str(existing["payload_hash"]),
                    "reserved": False,
                    "recovery_required": str(existing["state"]) in {"PENDING", "UNKNOWN"},
                    "reason_code": "RECOVERY_REQUIRED" if str(existing["state"]) in {"PENDING", "UNKNOWN"} else "ALREADY_PROCESSED",
                }
        return {
            "request_id": request_id,
            "state": "PENDING",
            "waha_message_id": None,
            "error_code": None,
            "asset_id": asset,
            "tag_key": tag,
            "caption_hash": caption_hash,
            "payload_hash": payload_hash,
            "reserved": True,
        }

    def finish_send(self, request_id, state, message_id=None, error_code=None):
        final_state = str(state or "").upper()
        if final_state not in {"PENDING", "SENT", "FAILED", "UNKNOWN"}:
            raise ProductImageValidationError("图片发送状态无效", "INVALID_SEND_STATE")
        with self._db() as connection:
            connection.execute(
                "UPDATE product_image_send_requests SET state=?,waha_message_id=?,error_code=?,updated_at=? WHERE request_id=?",
                (
                    final_state,
                    str(message_id or "") or None,
                    str(error_code or "") or None,
                    self._now(),
                    str(request_id),
                ),
            )
        return {
            "request_id": str(request_id),
            "state": final_state,
            "waha_message_id": str(message_id or "") or None,
            "error_code": str(error_code or "") or None,
        }

    def reserve_auto_send(self, session_name, chat_id, message_key, product_id, asset_id, source, caption):
        """Reserve a product-level automatic image action.

        The legacy ledger is asset keyed and intentionally remains the source
        of idempotency.  This adapter validates the product/asset relationship
        and records the action through that ledger without exposing raw chat
        identifiers or image bytes.
        """
        product_key = str(product_id or "").strip()
        asset_key = str(asset_id or "").strip()
        if not product_key or not asset_key:
            raise ProductImageValidationError("产品图片发送目标无效", "INVALID_SEND_REQUEST")
        with self._db() as connection:
            asset = self._load_asset(connection, asset_key, include_inactive=False)
        linked_product = str(asset.get("product_id") or "").strip()
        if linked_product and linked_product != product_key:
            raise ProductImageValidationError("产品图片与产品不匹配", "PRODUCT_ASSET_MISMATCH")
        tag_key = linked_product or str(asset.get("product_name") or asset.get("sku") or asset_key)
        # Reserve under an IMMEDIATE transaction and count every non-terminal
        # request. This prevents concurrent customer-image workers from
        # crossing the global daily limit while one request is still in flight.
        settings = self.settings_payload()
        daily_limit = int(settings.get("daily_limit", 20))
        session = str(session_name or "").strip()
        customer = str(chat_id or "").strip()
        message = str(message_key or "").strip()
        safe_caption = self._clean_caption(caption)
        if not session or not customer or not message or len(message) > 512:
            raise ProductImageValidationError("图片发送请求参数无效", "INVALID_SEND_REQUEST")
        chat_key = self._chat_key_hmac(session, customer)
        caption_hash = self._caption_hash(safe_caption)
        payload_hash = hashlib.sha256(f"{asset_key}\x00{tag_key}\x00{caption_hash}".encode("utf-8")).hexdigest()
        now = self._now()
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_id,state,waha_message_id,error_code,asset_id,tag_key,caption_hash,payload_hash FROM product_image_send_requests WHERE session_name=? AND chat_key_hmac=? AND message_key=?",
                (session, chat_key, message),
            ).fetchone()
            if existing:
                result = {"request_id": str(existing["request_id"]), "state": str(existing["state"]), "waha_message_id": existing["waha_message_id"], "error_code": existing["error_code"], "asset_id": str(existing["asset_id"]), "tag_key": str(existing["tag_key"]), "caption_hash": str(existing["caption_hash"]), "payload_hash": str(existing["payload_hash"]), "reserved": False, "recovery_required": str(existing["state"]) in {"PENDING", "UNKNOWN"}, "reason_code": "RECOVERY_REQUIRED" if str(existing["state"]) in {"PENDING", "UNKNOWN"} else "ALREADY_PROCESSED"}
            else:
                used = connection.execute("SELECT COUNT(*) FROM product_image_send_requests WHERE state IN ('SENT','PENDING','UNKNOWN') AND updated_at>=?", (now - (now % 86400),)).fetchone()[0]
                if int(used) >= daily_limit:
                    result = {"request_id": None, "state": "LIMIT", "asset_id": asset_key, "tag_key": tag_key, "reserved": False, "reason_code": "DAILY_LIMIT"}
                else:
                    request_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO product_image_send_requests(request_id,session_name,chat_key_hmac,message_key,asset_id,tag_key,caption_hash,payload_hash,state,waha_message_id,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (request_id, session, chat_key, message, asset_key, tag_key, caption_hash, payload_hash, "PENDING", None, None, now, now),
                    )
                    result = {"request_id": request_id, "state": "PENDING", "waha_message_id": None, "error_code": None, "asset_id": asset_key, "tag_key": tag_key, "caption_hash": caption_hash, "payload_hash": payload_hash, "reserved": True}
        result["product_id"] = product_key
        result["source"] = str(source or "customer_image").strip()[:64] or "customer_image"
        return result

    def finish_auto_send(self, request_id, state, message_id=None, error_code=None):
        """Finalize an automatic product-image reservation."""
        return self.finish_send(request_id, state, message_id=message_id, error_code=error_code)

    def daily_sent_count(self):
        start = self._now() - (self._now() % 86400)
        with self._db() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM product_image_send_requests WHERE state='SENT' AND updated_at>=?",
                    (start,),
                ).fetchone()[0]
            )


    def resolve_for_send(self, product_tag, variant=""):
        tag_key = normalize_tag(product_tag)
        variant_key = normalize_tag(variant) if str(variant or "").strip() else ""
        if not tag_key:
            return {"status": "NOT_FOUND", "code": "NOT_FOUND", "tag_key": ""}
        with self._db() as connection:
            rows = connection.execute(
                "SELECT a.*,t.tag_key,at.priority,at.is_primary "
                "FROM product_image_assets a "
                "LEFT JOIN product_image_asset_tags at ON at.asset_id=a.id "
                "LEFT JOIN product_image_tags t ON t.id=at.tag_id "
                "WHERE a.active=1 ORDER BY a.id,at.priority,t.tag_key"
            ).fetchall()
            assets = {}
            for row in rows:
                asset_id = str(row["id"])
                entry = assets.setdefault(
                    asset_id,
                    {
                        "row": row,
                        "fields": {
                            "product_name": normalize_tag(row["product_name"]),
                            "sku": normalize_tag(row["sku"]),
                            "aliases": {
                                normalize_tag(alias)
                                for alias in self._decode_aliases(row["aliases_json"])
                                if normalize_tag(alias)
                            },
                        },
                        "tags": [],
                    },
                )
                if row["tag_key"]:
                    entry["tags"].append(
                        {
                            "key": str(row["tag_key"]),
                            "priority": int(row["priority"]),
                            "is_primary": bool(row["is_primary"]),
                        }
                    )

            candidates = []
            for asset_id, entry in assets.items():
                fields = entry["fields"]
                tag_matches = [
                    item for item in entry["tags"]
                    if item["is_primary"] and item["key"] == tag_key
                ]
                scores = []
                if fields["product_name"] == tag_key and fields["product_name"]:
                    scores.append((0, 0))
                if fields["sku"] == tag_key and fields["sku"]:
                    scores.append((1, 0))
                if tag_key in fields["aliases"]:
                    scores.append((2, 0))
                scores.extend((3, item["priority"]) for item in tag_matches)
                if not scores:
                    continue
                if variant_key:
                    variant_values = {
                        fields["product_name"],
                        fields["sku"],
                        *fields["aliases"],
                        *(item["key"] for item in entry["tags"]),
                    }
                    if variant_key not in variant_values:
                        continue
                candidates.append((min(scores), asset_id))

            if not candidates:
                self._log("INFO", f"product_image.not_found tag={tag_key[:80]}")
                return {"status": "NOT_FOUND", "code": "NOT_FOUND", "tag_key": tag_key}
            best_score = min(score for score, _ in candidates)
            best_ids = sorted(asset_id for score, asset_id in candidates if score == best_score)
            if len(best_ids) != 1:
                self._log("INFO", f"product_image.ambiguous tag={tag_key[:80]}")
                return {
                    "status": "AMBIGUOUS",
                    "code": "AMBIGUOUS",
                    "tag_key": tag_key,
                    "candidates": [asset_id[:12] for asset_id in best_ids],
                }
            asset = self._load_asset(connection, best_ids[0], include_inactive=False)
            asset["status"] = "FOUND"
            asset["code"] = "FOUND"
            asset["tag_key"] = tag_key
            asset["variant"] = variant_key
            return asset


    def list_reference_images(self, product_id: str, variant: str = "") -> list[dict]:
        with self._db() as c:
            q="SELECT * FROM product_image_assets WHERE product_id=? AND active=1 AND allow_visual_match=1"; args=[str(product_id)]
            if variant: q += " AND variant=?"; args.append(str(variant))
            rows=c.execute(q+" ORDER BY sort_order,created_at,id",args).fetchall()
            return [self._load_asset(c,r["id"],include_inactive=True) for r in rows]

    def resolve_product_asset(self, product_id: str, variant: str = "", role: str = "primary") -> dict:
        role=str(role or "primary").strip().casefold()
        if role not in {"primary","reference","marketing"}: return {"status":"INVALID","code":"INVALID_REFERENCE_ROLE","candidates":[]}
        with self._db() as c:
            q="SELECT * FROM product_image_assets WHERE product_id=? AND active=1 AND allow_auto_send=1 AND reference_role=?"; args=[str(product_id),role]
            if variant: q += " AND variant=?"; args.append(str(variant))
            rows=c.execute(q+" ORDER BY sort_order,created_at,id",args).fetchall()
            if not rows:return {"status":"NOT_FOUND","code":"NOT_FOUND","candidates":[]}
            if len(rows)>1: return {"status":"AMBIGUOUS","code":"AMBIGUOUS","candidates":[str(r["id"]) for r in rows]}
            item=self._load_asset(c,rows[0]["id"]); item.update(status="FOUND",code="FOUND",role=role); return item

    def record_match_audit(self, payload: dict) -> dict:
        digest=str(payload.get("image_sha256") or "").strip().lower()
        if len(digest)!=64: raise ProductImageValidationError("图片校验值无效","INVALID_AUDIT")
        aid=str(uuid.uuid4()); now=self._now(); result=payload.get("result",{})
        with self._db() as c: c.execute("INSERT INTO product_image_match_audits(audit_id,candidate_product_id,variant,role,mime_type,image_sha256,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",(aid,str(payload.get("candidate_product_id") or "") or None,str(payload.get("variant") or ""),str(payload.get("role") or "primary"),str(payload.get("mime_type") or ""),digest,json.dumps(result,ensure_ascii=False),now))
        return {"audit_id":aid,"result":result,"image_sha256":digest,"created_at":now}

    def match_test(self, image_bytes: bytes, mime_type: str, candidate_product_id: str | None = None) -> dict:
        raw,_,actual,_,_,_=self._validate_bytes("match-test",mime_type,image_bytes); digest=hashlib.sha256(raw).hexdigest()
        with self._db() as c:
            q="SELECT * FROM product_image_assets WHERE active=1 AND allow_visual_match=1 AND sha256=?"; args=[digest]
            if candidate_product_id:q += " AND product_id=?"; args.append(str(candidate_product_id))
            rows=c.execute(q,args).fetchall()
        candidates=[{"asset_id":str(r["id"]),"product_id":str(r["product_id"] or ""),"variant":str(r["variant"] or ""),"reference_role":str(r["reference_role"] or "primary")} for r in rows]
        result={"status":"MATCHED" if candidates else "NO_MATCH","candidates":candidates,"mime_type":actual}; self.record_match_audit({"candidate_product_id":candidate_product_id,"mime_type":actual,"image_sha256":digest,"result":result}); return result
__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "ProductImageError",
    "ProductImageNotFoundError",
    "ProductImageService",
    "ProductImageValidationError",
    "normalize_tag",
]










