"""Persistent administrator accounts for the portable panel release.

The service deliberately owns only administrator identities.  It never returns
password hashes and it accepts a one-time bootstrap file so installation
scripts do not need to put a plaintext password in the image or database.
"""

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.exceptions import InvalidKey


USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_PASSWORD_LENGTH = 12
ARGON2_MEMORY_COST = 32 * 1024
ARGON2_ITERATIONS = 2
ARGON2_LANES = 2
ARGON2_LENGTH = 32
ARGON2_GATE_LIMIT = 2
MAX_FAILED_ATTEMPTS = 5
FAILED_ATTEMPT_WINDOW = 300
LOCKOUT_SECONDS = 60
_ARGON2_GATE = threading.BoundedSemaphore(ARGON2_GATE_LIMIT)


class AdminServiceError(ValueError):
    code = "ADMIN_ERROR"


class AdminAuthUnavailableError(AdminServiceError):
    code = "ADMIN_AUTH_UNAVAILABLE"


class AdminValidationError(AdminServiceError):
    code = "ADMIN_VALIDATION_ERROR"


class AdminConflictError(AdminServiceError):
    code = "ADMIN_CONFLICT"


class AdminNotFoundError(AdminServiceError):
    code = "ADMIN_NOT_FOUND"


class LastActiveAdministratorError(AdminServiceError):
    code = "LAST_ACTIVE_ADMIN"


def apply_migration(connection):
    """Create the administrator tables without changing existing data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            identity_hash TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            first_failed_at INTEGER NOT NULL,
            locked_until INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        """
    )


def validate_username(value):
    username = str(value or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise AdminValidationError(
            "管理员账号只能使用字母、数字、点、下划线或短横线，且长度为 1-64 个字符"
        )
    return username


def validate_password(value):
    password = str(value or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AdminValidationError(f"管理员密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > 256:
        raise AdminValidationError("管理员密码不能超过 256 个字符")
    if any(ord(char) < 32 for char in password):
        raise AdminValidationError("管理员密码不能包含控制字符")
    return password


def _password_hash(password):
    try:
        with _ARGON2_GATE:
            salt = os.urandom(16)
            kdf = Argon2id(
                salt,
                ARGON2_LENGTH,
                ARGON2_ITERATIONS,
                ARGON2_LANES,
                ARGON2_MEMORY_COST,
            )
            return kdf.derive_phc_encoded(password.encode("utf-8"))
    except MemoryError as error:
        raise AdminAuthUnavailableError("管理员认证资源暂时不足，请稍后重试") from error


def _verify_password(password, encoded):
    try:
        with _ARGON2_GATE:
            Argon2id.verify_phc_encoded(password.encode("utf-8"), encoded)
            return True
    except MemoryError as error:
        raise AdminAuthUnavailableError("管理员认证资源暂时不足，请稍后重试") from error
    except (InvalidKey, TypeError, ValueError):
        return False


def _argon2_parameters(encoded):
    match = re.match(
        r"^\$argon2id\$v=(?P<version>\d+)\$m=(?P<memory>\d+),t=(?P<iterations>\d+),p=(?P<lanes>\d+)\$",
        str(encoded or ""),
    )
    if not match:
        return None
    return tuple(
        int(match.group(name))
        for name in ("version", "memory", "iterations", "lanes")
    )


def _needs_rehash(encoded):
    parameters = _argon2_parameters(encoded)
    if parameters is None:
        return False
    version, memory, iterations, lanes = parameters
    return (version, memory, iterations, lanes) != (19, ARGON2_MEMORY_COST, ARGON2_ITERATIONS, ARGON2_LANES)


class AdminService:
    """CRUD and authentication operations for persistent panel administrators."""

    def __init__(self, database_path, clock=None):
        self.database_path = Path(database_path)
        self.clock = clock or time.time
        self._dummy_hash = _password_hash("invalid-password-for-timing-only")

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _public(row):
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }

    def has_users(self):
        connection = self._connect()
        try:
            return connection.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None
        finally:
            connection.close()

    def active_user_count(self):
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM admin_users WHERE is_active = 1").fetchone()[0])
        finally:
            connection.close()

    def list_users(self):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, username, is_active, created_at, updated_at, last_login_at "
                "FROM admin_users ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return [self._public(row) for row in rows]

    def create_user(self, username, password, is_active=True):
        name = validate_username(username)
        secret = validate_password(password)
        now = int(self.clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM admin_users WHERE username = ?", (name,)
            ).fetchone()
            if existing:
                raise AdminConflictError("管理员账号已经存在")
            cursor = connection.execute(
                "INSERT INTO admin_users(username, password_hash, is_active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, _password_hash(secret), 1 if is_active else 0, now, now),
            )
            row = connection.execute(
                "SELECT id, username, is_active, created_at, updated_at, last_login_at "
                "FROM admin_users WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            connection.commit()
            return self._public(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_user(self, user_id, username=None, is_active=None):
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError) as error:
            raise AdminValidationError("管理员编号无效") from error
        updates = {}
        if username is not None:
            updates["username"] = validate_username(username)
        if is_active is not None:
            updates["is_active"] = 1 if bool(is_active) else 0
        if not updates:
            row = self._get_user(numeric_id)
            if row is None:
                raise AdminNotFoundError("管理员不存在")
            return self._public(row)
        now = int(self.clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM admin_users WHERE id = ?", (numeric_id,)).fetchone()
            if row is None:
                raise AdminNotFoundError("管理员不存在")
            if "username" in updates:
                duplicate = connection.execute(
                    "SELECT 1 FROM admin_users WHERE username = ? AND id != ?",
                    (updates["username"], numeric_id),
                ).fetchone()
                if duplicate:
                    raise AdminConflictError("管理员账号已经存在")
            if updates.get("is_active") == 0 and row["is_active"]:
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM admin_users WHERE is_active = 1"
                ).fetchone()[0]
                if active_count <= 1:
                    raise LastActiveAdministratorError("不能停用最后一个有效管理员")
            assignments = ", ".join(f"{key} = ?" for key in updates)
            values = [updates[key] for key in updates] + [now, numeric_id]
            connection.execute(
                f"UPDATE admin_users SET {assignments}, updated_at = ? WHERE id = ?", values
            )
            updated = connection.execute(
                "SELECT id, username, is_active, created_at, updated_at, last_login_at "
                "FROM admin_users WHERE id = ?", (numeric_id,)
            ).fetchone()
            connection.commit()
            return self._public(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def change_password(self, user_id, password):
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError) as error:
            raise AdminValidationError("管理员编号无效") from error
        secret = validate_password(password)
        now = int(self.clock())
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE admin_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (_password_hash(secret), now, numeric_id),
            )
            if cursor.rowcount == 0:
                raise AdminNotFoundError("管理员不存在")
            connection.commit()
            row = connection.execute(
                "SELECT id, username, is_active, created_at, updated_at, last_login_at "
                "FROM admin_users WHERE id = ?", (numeric_id,)
            ).fetchone()
            return self._public(row)
        finally:
            connection.close()

    def _get_user(self, user_id):
        connection = self._connect()
        try:
            return connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        finally:
            connection.close()

    @staticmethod
    def _identity_hash(username, client_key):
        raw = f"{str(username or '').casefold()}\x00{str(client_key or '')}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _is_locked(self, connection, identity_hash, now):
        row = connection.execute(
            "SELECT failed_count, first_failed_at, locked_until FROM admin_login_attempts "
            "WHERE identity_hash = ?", (identity_hash,)
        ).fetchone()
        if not row:
            return False
        if row["locked_until"] > now:
            return True
        if now - row["first_failed_at"] > FAILED_ATTEMPT_WINDOW:
            connection.execute("DELETE FROM admin_login_attempts WHERE identity_hash = ?", (identity_hash,))
        return False

    def _record_failure(self, connection, identity_hash, now):
        row = connection.execute(
            "SELECT failed_count, first_failed_at FROM admin_login_attempts WHERE identity_hash = ?",
            (identity_hash,),
        ).fetchone()
        if not row or now - row["first_failed_at"] > FAILED_ATTEMPT_WINDOW:
            count = 1
            first = now
        else:
            count = int(row["failed_count"]) + 1
            first = row["first_failed_at"]
        locked_until = now + LOCKOUT_SECONDS if count >= MAX_FAILED_ATTEMPTS else 0
        connection.execute(
            "INSERT INTO admin_login_attempts(identity_hash, failed_count, first_failed_at, locked_until, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(identity_hash) DO UPDATE SET "
            "failed_count=excluded.failed_count, first_failed_at=excluded.first_failed_at, "
            "locked_until=excluded.locked_until, updated_at=excluded.updated_at",
            (identity_hash, count, first, locked_until, now),
        )

    def authenticate(self, username, password, client_key=""):
        """Return True only for an active account with a valid password."""
        name = str(username or "").strip()
        secret = str(password or "")
        now = int(self.clock())
        identity_hash = self._identity_hash(name, client_key)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._is_locked(connection, identity_hash, now):
                connection.commit()
                _verify_password(secret, self._dummy_hash)
                return False
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? AND is_active = 1", (name,)
            ).fetchone()
            valid = _verify_password(secret, row["password_hash"] if row else self._dummy_hash)
            if not valid:
                self._record_failure(connection, identity_hash, now)
                connection.commit()
                return False
            password_hash = row["password_hash"]
            replacement_hash = password_hash
            if _needs_rehash(password_hash):
                try:
                    replacement_hash = _password_hash(secret)
                except AdminAuthUnavailableError:
                    replacement_hash = password_hash
            connection.execute(
                "DELETE FROM admin_login_attempts WHERE identity_hash = ?", (identity_hash,)
            )
            connection.execute(
                "UPDATE admin_users SET password_hash = ?, last_login_at = ? WHERE id = ?",
                (replacement_hash, now, row["id"]),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def bootstrap_from_file(self, path):
        """Create the first administrator from a one-time key/value file."""
        bootstrap_path = Path(path)
        if not bootstrap_path.exists():
            return False
        if self.has_users():
            try:
                bootstrap_path.unlink()
            except OSError:
                pass
            return False
        values = {}
        for raw_line in bootstrap_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        username = values.get("PANEL_ADMIN_USERNAME")
        password = values.get("PANEL_ADMIN_PASSWORD")
        if username is None or password is None:
            raise AdminValidationError("管理员初始化文件缺少必要字段")
        result = self.create_user(username, password, True)
        try:
            bootstrap_path.unlink()
        except OSError as error:
            raise AdminServiceError("管理员初始化文件无法删除") from error
        return bool(result)
