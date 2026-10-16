"""Session authentication, password hashing, CSRF checks, and login throttling."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import threading
import time

from fastapi import Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

PASSWORD_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def normalize_username(username: str) -> str:
    value = username.strip().casefold()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("Username must be 3-64 characters using letters, numbers, dot, dash, or underscore.")
    return value


def hash_password(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_hex, digest_hex = encoded_hash.split("$", 3)
        iterations = int(iteration_text)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass
class _Attempt:
    count: int
    window_started: float
    locked_until: float


class LoginRateLimiter:
    """Per-process IP throttling; use a shared edge limiter for multi-worker deployments."""

    def __init__(self, limit: int = 5, window_seconds: int = 900, lock_seconds: int = 900):
        self.limit = limit
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._attempts: OrderedDict[str, _Attempt] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(client_address: str) -> str:
        return hashlib.sha256((client_address or "unknown").encode("utf-8")).hexdigest()

    def is_blocked(self, client_address: str) -> bool:
        now = time.monotonic()
        key = self._key(client_address)
        with self._lock:
            attempt = self._attempts.get(key)
            if attempt is None:
                return False
            if now >= attempt.locked_until and now - attempt.window_started >= self.window_seconds:
                del self._attempts[key]
                return False
            self._attempts.move_to_end(key)
            return now < attempt.locked_until

    def failed(self, client_address: str) -> None:
        now = time.monotonic()
        key = self._key(client_address)
        with self._lock:
            attempt = self._attempts.get(key)
            if attempt is None or now - attempt.window_started >= self.window_seconds:
                attempt = _Attempt(count=0, window_started=now, locked_until=0)
            attempt.count += 1
            if attempt.count >= self.limit:
                attempt.locked_until = now + self.lock_seconds
            self._attempts[key] = attempt
            self._attempts.move_to_end(key)
            while len(self._attempts) > 4096:
                self._attempts.popitem(last=False)

    def succeeded(self, client_address: str) -> None:
        with self._lock:
            self._attempts.pop(self._key(client_address), None)

    def clear(self) -> None:
        with self._lock:
            self._attempts.clear()


login_rate_limiter = LoginRateLimiter()
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def authenticate_user(db: Session, username: str, password: str, client_address: str) -> User | None:
    if login_rate_limiter.is_blocked(client_address):
        return None
    normalized = username.strip().casefold()
    user = db.scalar(select(User).where(User.username == normalized))
    candidate_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    if len(password) > MAX_PASSWORD_LENGTH:
        verify_password(password[:MAX_PASSWORD_LENGTH], candidate_hash)
        password_matches = False
    else:
        password_matches = verify_password(password, candidate_hash)
    if user is not None and user.is_active and password_matches:
        login_rate_limiter.succeeded(client_address)
        return user
    login_rate_limiter.failed(client_address)
    return None


def require_authenticated_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if isinstance(user_id, int) else None
    if user is None or not user.is_active or not user.is_admin:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_csrf(request: Request, csrf_token: str | None = Form(default=None)) -> None:
    expected = request.session.get("csrf_token", "")
    submitted = request.headers.get("x-csrf-token") or csrf_token or ""
    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        raise HTTPException(status_code=403, detail="The form security token is missing or invalid. Refresh the page and try again.")
