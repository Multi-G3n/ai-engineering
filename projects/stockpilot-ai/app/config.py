"""Shared configuration for the web app, Alembic, and command-line utilities."""

import os
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
