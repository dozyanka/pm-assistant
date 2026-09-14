"""Local account and session management using only Python's standard library."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import required_string, utc_now

ITERATIONS = 240_000
SESSION_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex()


class AuthManager:
    def __init__(self, store):
        self.store = store

    def has_users(self) -> bool:
        with closing(self.store.connect()) as con:
            return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    @staticmethod
    def _validate_username(username: str) -> str:
        required_string(username, "username", 64)
        username = username.strip().lower()
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", username):
            raise ValueError("Логин: 3–64 символа, латинские буквы, цифры, точка, дефис или подчёркивание.")
        return username

    @staticmethod
    def _validate_password(password: str) -> str:
        if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
            raise ValueError("Пароль должен содержать от 8 до 200 символов.")
        if "\x00" in password:
            raise ValueError("Некорректный пароль.")
        return password

    def setup_first_admin(self, username: str, display_name: str, password: str) -> dict:
        if self.has_users():
            raise ValueError("Первый администратор уже создан.")
        return self._create_user(username, display_name, password, "admin")

    def _create_user(self, username: str, display_name: str, password: str, role: str) -> dict:
        username = self._validate_username(username)
        display_name = required_string(display_name, "display name", 100).strip()
        password = self._validate_password(password)
        if role not in {"admin", "pm"}:
            raise ValueError("Некорректная роль.")
        salt = secrets.token_bytes(16)
        ph = _password_hash(password, salt)
        with closing(self.store.connect()) as con, con:
            try:
                cur = con.execute("INSERT INTO users(username,display_name,password_salt,password_hash,role,active,created_at) VALUES (?,?,?,?,?,1,?)",
                                  (username, display_name, salt.hex(), ph, role, utc_now()))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError("Пользователь с таким логином уже существует.") from exc
                raise
            uid = cur.lastrowid
        return {"id": uid, "username": username, "display_name": display_name, "role": role, "active": True}

    def create_user(self, actor: dict, username: str, display_name: str, password: str, role: str = "pm") -> dict:
        if not actor or actor.get("role") != "admin":
            raise ValueError("Только администратор может создавать пользователей.")
        return self._create_user(username, display_name, password, role)

    def list_users(self, actor: dict) -> list[dict]:
        if not actor or actor.get("role") != "admin":
            raise ValueError("Только администратор может просматривать пользователей.")
        with closing(self.store.connect()) as con:
            return [{"id": r["id"], "username": r["username"], "display_name": r["display_name"],
                     "role": r["role"], "active": bool(r["active"]), "created_at": r["created_at"]}
                    for r in con.execute("SELECT id,username,display_name,role,active,created_at FROM users ORDER BY id")]

    def login(self, username: str, password: str) -> tuple[dict, str]:
        username = self._validate_username(username)
        self._validate_password(password)
        with closing(self.store.connect()) as con, con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row is None or not row["active"]:
                raise ValueError("Неверный логин или пароль.")
            salt = bytes.fromhex(row["password_salt"])
            actual = _password_hash(password, salt)
            if not hmac.compare_digest(actual, row["password_hash"]):
                raise ValueError("Неверный логин или пароль.")
            token = secrets.token_urlsafe(36)
            created = _now()
            expires = created + timedelta(days=SESSION_DAYS)
            con.execute("DELETE FROM sessions WHERE expires_at<=?", (created.isoformat(),))
            con.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                        (_token_hash(token), row["id"], created.isoformat(), expires.isoformat()))
            user = {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                    "role": row["role"], "active": True}
            return user, token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token or not isinstance(token, str) or len(token) > 200:
            return None
        try:
            hashed = _token_hash(token)
        except UnicodeEncodeError:
            return None
        with closing(self.store.connect()) as con, con:
            row = con.execute("""SELECT u.id,u.username,u.display_name,u.role,u.active,s.expires_at
                FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (hashed,)).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= _now().isoformat() or not row["active"]:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (hashed,))
                return None
            return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                    "role": row["role"], "active": True}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        try:
            hashed = _token_hash(token)
        except UnicodeEncodeError:
            return
        with closing(self.store.connect()) as con, con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hashed,))
