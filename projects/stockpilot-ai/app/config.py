import os


def database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./stockpilot.db")
