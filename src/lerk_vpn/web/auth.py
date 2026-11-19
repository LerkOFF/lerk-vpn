"""Вход: хеш пароля, подписанная cookie, CSRF, лимит неверных попыток."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .db import DB
from .settings import Settings

COOKIE = "lvs"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, login: str, password_hash: str, db: DB, settings: Settings):
        self.login = (login or "admin").strip().lower()
        self.hash = password_hash or ""
        self.db = db
        self.settings = settings
        # подпись зависит от хеша пароля: смена пароля завершает все входы
        self._key = hashlib.sha256(b"lerk-vpn-session|" + self.hash.encode()).digest()

    @property
    def enabled(self) -> bool:
        return bool(self.hash)

    # --- cookie ---
    def make_cookie(self) -> str:
        exp = int(time.time()) + int(self.settings.get("security.session_days")) * 86400
        nonce = secrets.token_hex(8)
        body = f"{exp}.{nonce}"
        return f"{body}.{self._sig(body)}"

    def _sig(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def check_cookie(self, value: str | None) -> str | None:
        """Возвращает идентификатор сессии (для CSRF) или None."""
        if not value or not self.enabled:
            return None
        try:
            exp, nonce, sig = value.split(".")
            if int(exp) < time.time() or not hmac.compare_digest(sig, self._sig(f"{exp}.{nonce}")):
                return None
        except ValueError:
            return None
        return value

    def csrf(self, session: str) -> str:
        return hmac.new(self._key, b"csrf|" + session.encode(), hashlib.sha256).hexdigest()[:32]

    def csrf_ok(self, session: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf(session), token or "")

    # --- лимит попыток ---
    def blocked_for(self, ip: str) -> int:
        row = self.db.one("SELECT until FROM attempts WHERE ip=?", (ip,))
        if row and row["until"] > time.time():
            return int(row["until"] - time.time())
        return 0

    def fail(self, ip: str) -> None:
        now = int(time.time())
        row = self.db.one("SELECT fails,last FROM attempts WHERE ip=?", (ip,))
        fails = (row["fails"] if row and now - row["last"] < 3600 else 0) + 1
        until = 0
        if fails >= int(self.settings.get("security.max_attempts")):
            until = now + int(self.settings.get("security.pause_min")) * 60
            fails = 0
        self.db.x(
            "INSERT INTO attempts(ip,fails,until,last) VALUES(?,?,?,?) ON CONFLICT(ip) DO UPDATE SET fails=excluded.fails, until=excluded.until, last=excluded.last",
            (ip, fails, until, now),
        )

    def success(self, ip: str) -> None:
        self.db.x("DELETE FROM attempts WHERE ip=?", (ip,))

    def try_login(self, login: str, password: str) -> bool:
        # одинаковое время и один и тот же ответ на любой промах
        ok_login = hmac.compare_digest((login or "").strip().lower(), self.login)
        ok_pass = verify_password(password or "", self.hash)
        return ok_login and ok_pass
