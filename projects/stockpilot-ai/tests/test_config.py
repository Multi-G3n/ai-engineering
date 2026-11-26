"""Configuration contracts; all files and operational databases are disposable."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url

from app import config


VARIABLES = (
    "DATABASE_URL", "STOCKPILOT_SECRET_KEY", "STOCKPILOT_COOKIE_SECURE",
    "GEMINI_API_KEY", "GEMINI_MODEL",
    "GEMINI_REQUEST_TIMEOUT_SECONDS", "GEMINI_TOTAL_BUDGET_SECONDS", "GEMINI_MAX_ATTEMPTS",
)


def test_project_dotenv_is_loaded_outside_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "DATABASE_URL=sqlite:///./configured.db\n"
        "STOCKPILOT_SECRET_KEY=test-only-dotenv-session-secret-123456789\n"
        "STOCKPILOT_COOKIE_SECURE=false\n"
        "GEMINI_API_KEY=test-only-dotenv-key\n"
        "GEMINI_MODEL=test-only-model\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("GEMINI_MODEL=wrong-location\n", encoding="utf-8")
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.chdir(elsewhere)

    config.load_environment()

    assert make_url(config.database_url()).database == (project / "configured.db").as_posix()
    assert config.session_secret_key() == "test-only-dotenv-session-secret-123456789"
    assert config.session_cookie_secure() is False
    assert config.gemini_api_key() == "test-only-dotenv-key"
    assert config.gemini_model() == "test-only-model"
    assert not (project / "configured.db").exists()


def test_process_environment_overrides_dotenv_including_empty_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}=dotenv-value" for name in VARIABLES), encoding="utf-8"
    )
    values = {
        "DATABASE_URL": "sqlite:///:memory:",
        "STOCKPILOT_SECRET_KEY": "test-only-process-session-secret-123456789",
        "STOCKPILOT_COOKIE_SECURE": "true",
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "process-model",
        "GEMINI_REQUEST_TIMEOUT_SECONDS": "15",
        "GEMINI_TOTAL_BUDGET_SECONDS": "25",
        "GEMINI_MAX_ATTEMPTS": "1",
    }
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config.load_environment()
    config.load_environment()

    assert all(os.environ[name] == value for name, value in values.items())
    assert config.gemini_api_key() == ""
    assert config.gemini_model() == "process-model"
    assert config.gemini_request_settings() == config.GeminiRequestSettings(15, 25, 1)


def test_defaults_without_dotenv_and_required_session_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    config.load_environment()

    assert make_url(config.database_url()).database == (tmp_path / "stockpilot.db").as_posix()
    assert config.session_cookie_secure() is True
    assert config.gemini_api_key() is None
    assert config.gemini_model() == "gemini-3.1-flash-lite"
    assert config.gemini_request_settings() == config.GeminiRequestSettings(30, 60, 2)
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        config.session_secret_key()


@pytest.mark.parametrize("value", ["sqlite://", "sqlite:///:memory:", "sqlite:///file:test?mode=memory&cache=shared&uri=true"])
def test_memory_and_uri_database_urls_are_preserved(monkeypatch, value):
    monkeypatch.setenv("DATABASE_URL", value)
    assert config.database_url() == value


def test_absolute_database_url_and_query_are_preserved(tmp_path, monkeypatch):
    value = f"sqlite:///{(tmp_path / 'absolute.db').as_posix()}?timeout=12"
    monkeypatch.setenv("DATABASE_URL", value)
    assert config.database_url() == value


def test_relative_database_url_preserves_query_and_environment(tmp_path, monkeypatch):
    value = "sqlite:///./data/test.db?timeout=12"
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_URL", value)
    url = make_url(config.database_url())
    assert url.database == (tmp_path / "data" / "test.db").as_posix()
    assert url.query == {"timeout": "12"}
    assert os.environ["DATABASE_URL"] == value


def test_non_sqlite_database_url_is_preserved_without_redaction(monkeypatch):
    value = "postgresql://test_user:test_only_password@localhost/test_database"
    monkeypatch.setenv("DATABASE_URL", value)
    assert config.database_url() == value


@pytest.mark.parametrize("value", ["invalid", ""])
def test_invalid_cookie_setting_is_rejected(monkeypatch, value):
    monkeypatch.setenv("STOCKPILOT_COOKIE_SECURE", value)
    with pytest.raises(RuntimeError, match="must be true or false"):
        config.session_cookie_secure()


@pytest.mark.parametrize("outside_project", [False, True])
def test_web_and_cli_share_configuration_in_fresh_process(tmp_path, outside_project):
    """Exercise init/admin/seed/backup only against an explicit temporary database."""
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "entrypoints.db"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(project),
        "DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "STOCKPILOT_SECRET_KEY": "test-only-subprocess-session-secret-123456789",
        "STOCKPILOT_COOKIE_SECURE": "true",
        "GEMINI_API_KEY": "test-only-not-a-live-key",
        "GEMINI_MODEL": "test-only-model",
    })
    script = '''
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from app import backup_db, config, create_admin, database, init_db, main, seed
from app.models import Product, User

assert create_admin.SessionLocal is database.SessionLocal
assert seed.SessionLocal is database.SessionLocal
assert backup_db.database_url() == config.database_url()
assert Path(database.engine.url.database).resolve() == Path(make_url(config.database_url()).database).resolve()
assert main.session_secret_key() == config.session_secret_key()
with patch("google.genai.Client", side_effect=AssertionError("live AI forbidden")):
    init_db.main()
    with patch("builtins.input", return_value="config-test-admin"), patch(
        "app.create_admin.getpass", return_value="test-only-admin-password-123"
    ):
        create_admin.main()
    seed.main()
    seed.main()
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count(Product.id))) == 5
        assert db.scalar(select(func.count(User.id))) == 1
    target = backup_db.main()
    assert target.parent == Path(database.engine.url.database).parent / "backups"
    assert backup_db._integrity_result(target) == "ok"
database.engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=tmp_path if outside_project else project,
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert database.is_file()
