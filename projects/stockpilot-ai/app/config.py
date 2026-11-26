"""Shared configuration for the web app, Alembic, and command-line utilities."""

import os
import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_environment() -> None:
    """Read only the project .env, preserving explicitly supplied environment values."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


load_environment()


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "sqlite:///./stockpilot.db")
    url = make_url(value)
    if url.drivername.startswith("sqlite") and url.database not in (None, "", ":memory:"):
        # SQLite URI filenames carry their own semantics; do not rewrite them.
        if not (url.query.get("uri") == "true" and url.database.startswith("file:")):
            path = Path(url.database)
            if not path.is_absolute():
                url = url.set(database=(PROJECT_ROOT / path).resolve().as_posix())
                return url.render_as_string(hide_password=False)
    return value


def gemini_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY")


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


@dataclass(frozen=True)
class GeminiRequestSettings:
    timeout_seconds: float
    total_budget_seconds: float
    max_attempts: int


def gemini_request_settings() -> GeminiRequestSettings:
    """Validate without including supplied configuration values in errors."""
    def seconds(name: str, default: str, maximum: float) -> float:
        try:
            value = float(os.getenv(name, default))
        except ValueError:
            raise ValueError(f"{name} must be a finite number between 0.1 and {maximum} seconds.") from None
        if not math.isfinite(value) or not 0.1 <= value <= maximum:
            raise ValueError(f"{name} must be a finite number between 0.1 and {maximum} seconds.")
        return value

    timeout = seconds("GEMINI_REQUEST_TIMEOUT_SECONDS", "30", 300)
    budget = seconds("GEMINI_TOTAL_BUDGET_SECONDS", "60", 600)
    try:
        attempts = int(os.getenv("GEMINI_MAX_ATTEMPTS", "2"))
    except ValueError:
        raise ValueError("GEMINI_MAX_ATTEMPTS must be 1 or 2.") from None
    if attempts not in (1, 2):
        raise ValueError("GEMINI_MAX_ATTEMPTS must be 1 or 2.")
    return GeminiRequestSettings(timeout, budget, attempts)


def session_secret_key() -> str:
    key = os.getenv("STOCKPILOT_SECRET_KEY", "")
    if len(key) < 32:
        raise RuntimeError(
            "Set STOCKPILOT_SECRET_KEY to a securely generated value of at least 32 characters."
        )
    return key


def session_cookie_secure() -> bool:
    value = os.getenv("STOCKPILOT_COOKIE_SECURE", "true").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError("STOCKPILOT_COOKIE_SECURE must be true or false.")
    return value in {"true", "1", "yes"}
