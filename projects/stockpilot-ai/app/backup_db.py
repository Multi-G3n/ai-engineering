"""Create a consistent, non-overwriting SQLite backup before migrations."""
from datetime import datetime
from pathlib import Path
import sqlite3

from sqlalchemy.engine import make_url

from app.config import database_url


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
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {target}")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError(f"SQLite backup integrity check failed: {result}")
    print(f"Verified SQLite backup created: {target}")
    return target


if __name__ == "__main__":
    main()
