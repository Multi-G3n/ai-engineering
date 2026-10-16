"""Create a consistent, non-overwriting SQLite backup before migrations."""
from datetime import datetime
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from sqlalchemy.engine import make_url

from app.config import database_url


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _copy_database(source: Path, target: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=0)) as src:
        with closing(sqlite3.connect(target, timeout=0)) as dst:
            src.backup(dst)


def _integrity_result(target: Path) -> str:
    target_uri = f"file:{target.as_posix()}?mode=ro"
    with closing(sqlite3.connect(target_uri, uri=True, timeout=0)) as db:
        row = db.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "no result returned"


def _remove_owned_file(target: Path, identity: tuple[int, int] | None) -> None:
    """Remove only the same filesystem object exclusively created by this call."""
    if identity is not None and _file_identity(target) == identity:
        target.unlink()


def main() -> Path:
    url = make_url(database_url())
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise RuntimeError("The automatic backup command supports file-based SQLite databases only.")
    source = Path(url.database).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database file does not exist: {source}")
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    target = backup_dir / f"{source.stem}-pre-migration-{datetime.now():%Y%m%d-%H%M%S}.db"
    print(f"Source database: {source}")
    print(f"Backup destination: {target}")
    identity = None
    descriptor = None
    try:
        # O_EXCL reserves the path atomically: an existing file is never opened.
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        file_stat = os.fstat(descriptor)
        identity = (file_stat.st_dev, file_stat.st_ino)
        try:
            os.close(descriptor)
        finally:
            descriptor = None
        _copy_database(source, target)
        result = _integrity_result(target)
        if result != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {result}")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        _remove_owned_file(target, identity)
        raise
    print(f"Verified SQLite backup created: {target}")
    return target


if __name__ == "__main__":
    main()
