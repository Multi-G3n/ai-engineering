import os


def database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./stockpilot.db")


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
