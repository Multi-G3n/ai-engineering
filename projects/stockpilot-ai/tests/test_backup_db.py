from datetime import datetime as RealDateTime
import sqlite3

import pytest

from app import backup_db


def create_source(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE)")
        db.executemany("INSERT INTO products (sku) VALUES (?)", [("BK-1",), ("BK-2",)])


def configure_relative_source(monkeypatch, tmp_path):
    source = tmp_path / "source.db"
    create_source(source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./source.db")
    return source


def test_backup_creates_verified_copy_and_reports_absolute_paths(monkeypatch, tmp_path, capsys):
    source = configure_relative_source(monkeypatch, tmp_path)
    original_check = backup_db._integrity_result
    checks = []

    def record_check(target):
        checks.append(target)
        return original_check(target)

    monkeypatch.setattr(backup_db, "_integrity_result", record_check)
    target = backup_db.main()

    assert target.is_absolute()
    assert target.parent == tmp_path / "backups"
    assert checks == [target]
    assert "Source database: " + str(source.resolve()) in capsys.readouterr().out
    with sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT sku FROM products ORDER BY sku").fetchall() == [("BK-1",), ("BK-2",)]


def test_filename_collision_preserves_existing_backup(monkeypatch, tmp_path):
    configure_relative_source(monkeypatch, tmp_path)
    fixed_time = RealDateTime(2026, 10, 16, 12, 0, 0)

    class FixedDateTime:
        @classmethod
        def now(cls):
            return fixed_time

    monkeypatch.setattr(backup_db, "datetime", FixedDateTime)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    existing = backup_dir / "source-pre-migration-20261016-120000.db"
    existing.write_bytes(b"pre-existing backup must remain byte-for-byte unchanged")
    before = existing.read_bytes()

    with pytest.raises(FileExistsError):
        backup_db.main()

    assert existing.read_bytes() == before
    assert list(backup_dir.iterdir()) == [existing]


def test_copy_failure_removes_only_the_reserved_destination(monkeypatch, tmp_path):
    configure_relative_source(monkeypatch, tmp_path)

    def fail_copy(_source, _target):
        raise OSError("simulated SQLite backup failure")

    monkeypatch.setattr(backup_db, "_copy_database", fail_copy)
    with pytest.raises(OSError, match="simulated SQLite backup failure"):
        backup_db.main()

    assert list((tmp_path / "backups").iterdir()) == []


def test_integrity_failure_removes_only_the_reserved_destination(monkeypatch, tmp_path):
    configure_relative_source(monkeypatch, tmp_path)
    monkeypatch.setattr(backup_db, "_integrity_result", lambda _target: "simulated corruption")

    with pytest.raises(RuntimeError, match="integrity check failed: simulated corruption"):
        backup_db.main()

    assert list((tmp_path / "backups").iterdir()) == []
