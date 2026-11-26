"""Migration/recovery contracts. Every database lives under pytest's tmp_path."""

from contextlib import closing
from pathlib import Path
import sqlite3
import os
import subprocess
import sys

from alembic import command
from alembic.config import Config
from alembic.operations.batch import ApplyBatchImpl
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from app import backup_db, init_db
from app.database import Base
from migrations.schema_20261016 import metadata as historical_metadata

PROJECT = Path(__file__).resolve().parents[1]
HEAD = "20261126_6"
BUSINESS = ("products", "sales_records", "sales_tracking_days", "inventory_movements", "users")


def configuration(monkeypatch, database):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    return Config(str(PROJECT / "alembic.ini"))


def normalize(value):
    return "".join(str(value if value is not None else "").lower().split()).strip("()'\"")


def signature(database):
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    try:
        inspector = sa.inspect(engine)
        result = {}
        for table in sorted(set(inspector.get_table_names()) - {"alembic_version"}):
            result[table] = {
                "columns": [(c["name"], str(c["type"]), c["nullable"], normalize(c["default"]))
                            for c in inspector.get_columns(table)],
                "pk": inspector.get_pk_constraint(table)["constrained_columns"],
                "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"],
                               tuple(f["referred_columns"]), f["options"].get("ondelete"))
                              for f in inspector.get_foreign_keys(table)),
                "unique": sorted((u["name"] or "", tuple(u["column_names"]))
                                 for u in inspector.get_unique_constraints(table)),
                "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                                  for i in inspector.get_indexes(table)),
                "checks": sorted((c["name"] or "", normalize(c["sqltext"]))
                                 for c in inspector.get_check_constraints(table)),
            }
        return result
    finally:
        engine.dispose()


def rows(database):
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: db.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                for table in BUSINESS if table in tables}


def revision(database):
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as db:
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def populate(database):
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO products VALUES (101,'KEPT','Preserved','Test',4.25,7,2,'2026-10-10 12:00:00')")
        db.execute("INSERT INTO sales_records VALUES (77,101,'2026-10-10',1,4.25,'2026-10-10 12:01:00',NULL)")
        db.execute("INSERT INTO inventory_movements VALUES (1,101,8,'Opening stock','2026-10-10 12:00:00',NULL)")
        db.execute("INSERT INTO inventory_movements VALUES (2,101,-1,'Sale recorded (sales record #77)','2026-10-10 12:01:00',77)")
        db.execute("INSERT INTO sales_tracking_days VALUES (5,101,'2026-10-10','2026-10-10 13:00:00')")
        db.execute("INSERT INTO users VALUES (9,'kept-admin','test-only-hash',1,1,'2026-10-10 11:00:00')")
        db.commit()


def old_head(monkeypatch, database):
    """Reproduce the original model-created 3b layout, including redundant indexes."""
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    historical_metadata(include_users=True).create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE UNIQUE INDEX uq_sales_idempotency_key ON sales_records(idempotency_key)")
        connection.exec_driver_sql("CREATE UNIQUE INDEX uq_inventory_movement_sales_record ON inventory_movements(sales_record_id)")
    engine.dispose()
    cfg = configuration(monkeypatch, database)
    command.stamp(cfg, "20261016_3b")
    populate(database)
    return cfg


def legacy(database, with_sales):
    with closing(sqlite3.connect(database)) as db:
        db.executescript('''
CREATE TABLE products (
 id INTEGER PRIMARY KEY, sku VARCHAR(64) NOT NULL UNIQUE, name VARCHAR(160) NOT NULL,
 category VARCHAR(100) NOT NULL, unit_price NUMERIC(10,2) NOT NULL,
 stock_quantity INTEGER NOT NULL, minimum_stock INTEGER NOT NULL,
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
CREATE TABLE inventory_movements (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 quantity INTEGER NOT NULL, reason VARCHAR(240) NOT NULL,
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
INSERT INTO products VALUES (101,'KEPT','Preserved','Test',4.25,7,2,'2026-10-10 12:00:00');
INSERT INTO inventory_movements VALUES (1,101,8,'Opening stock','2026-10-10 12:00:00');
''')
        if with_sales:
            db.executescript('''
CREATE TABLE sales_records (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 sale_date DATE NOT NULL, quantity_sold INTEGER NOT NULL CHECK(quantity_sold > 0),
 unit_selling_price NUMERIC(10,2) NOT NULL CHECK(unit_selling_price >= 0),
 created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
INSERT INTO sales_records VALUES (77,101,'2026-10-10',1,4.25,'2026-10-10 12:01:00');
INSERT INTO inventory_movements VALUES (2,101,-1,'Sale recorded (sales record #77)','2026-10-10 12:01:00');
''')


@pytest.fixture
def expected_schema(tmp_path):
    database = tmp_path / "model-expectations.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)  # Reference only; documented initialization uses Alembic.
    engine.dispose()
    return signature(database)


def test_revision_chain_has_one_head_and_expected_order():
    script = ScriptDirectory.from_config(Config(str(PROJECT / "alembic.ini")))
    assert script.get_heads() == [HEAD]
    assert [r.revision for r in reversed(list(script.walk_revisions()))] == [
        "20261016_2b", "20261016_3b", "20261126_53", HEAD,
    ]


def test_bootstrap_does_not_create_users_or_call_current_metadata(tmp_path, monkeypatch):
    database = tmp_path / "bootstrap.db"
    cfg = configuration(monkeypatch, database)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Historical migrations must not create current model metadata")

    monkeypatch.setattr(Base.metadata, "create_all", forbidden)
    command.upgrade(cfg, "20261016_2b")
    assert "users" not in signature(database)
    assert revision(database) == "20261016_2b"
    command.upgrade(cfg, "20261016_3b")
    assert "users" in signature(database)


def test_fresh_upgrade_matches_models_and_repeated_init(tmp_path, monkeypatch, expected_schema):
    database = tmp_path / "fresh.db"
    cfg = configuration(monkeypatch, database)
    command.upgrade(cfg, "head")
    assert signature(database) == expected_schema
    assert revision(database) == HEAD
    command.check(cfg)  # Autogeneration sees no structural drift either.
    populate(database)
    before = rows(database)
    init_db.main()
    command.upgrade(cfg, "head")
    assert rows(database) == before
    assert signature(database) == expected_schema


@pytest.mark.parametrize("with_sales", [False, True])
def test_supported_legacy_upgrade_preserves_rows_and_matches_models(tmp_path, monkeypatch, expected_schema, with_sales):
    database = tmp_path / "legacy.db"
    legacy(database, with_sales)
    before = rows(database)
    cfg = configuration(monkeypatch, database)
    command.upgrade(cfg, "head")
    after = rows(database)
    assert after["products"] == before["products"]
    assert [r[:-1] for r in after["inventory_movements"]] == before["inventory_movements"]
    if with_sales:
        assert [r[:-1] for r in after["sales_records"]] == before["sales_records"]
        assert after["inventory_movements"][1][-1] == 77
    assert signature(database) == expected_schema
    assert revision(database) == HEAD
    command.upgrade(cfg, "head")
    assert rows(database) == after


def test_original_applied_head_preserves_all_business_and_account_data(tmp_path, monkeypatch, expected_schema):
    database = tmp_path / "original-head.db"
    cfg = old_head(monkeypatch, database)
    before = rows(database)
    command.upgrade(cfg, "head")
    assert rows(database) == before
    assert signature(database) == expected_schema


def test_documented_initialization_matches_fresh_upgrade(tmp_path, monkeypatch, expected_schema):
    database = tmp_path / "documented.db"
    configuration(monkeypatch, database)
    init_db.main()
    assert revision(database) == HEAD
    assert signature(database) == expected_schema
    assert all(not records for records in rows(database).values())


def test_failed_batch_rebuild_rolls_back_schema_data_and_revision(tmp_path, monkeypatch):
    database = tmp_path / "failure.db"
    cfg = old_head(monkeypatch, database)
    before_rows, before_schema = rows(database), signature(database)
    original = ApplyBatchImpl._create
    rebuilt = []

    def fail_after_second_copy(self, impl):
        original(self, impl)
        rebuilt.append(self.table.name)
        if len(rebuilt) == 2:
            raise RuntimeError("injected migration failure after table replacement")

    with monkeypatch.context() as patch:
        patch.setattr(ApplyBatchImpl, "_create", fail_after_second_copy)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            command.upgrade(cfg, "head")
    assert len(rebuilt) == 2
    assert revision(database) == "20261016_3b"
    assert rows(database) == before_rows
    assert signature(database) == before_schema
    command.upgrade(cfg, "head")  # A corrected retry needs no reset or stamp.
    assert rows(database) == before_rows


@pytest.mark.parametrize("alter", [
    "ALTER TABLE products ADD COLUMN custom_data TEXT",
    "CREATE INDEX custom_index ON products(category)",
    "CREATE VIEW custom_view AS SELECT * FROM products",
    "UPDATE products SET stock_quantity = -1 WHERE id = 101",
    "INSERT INTO inventory_movements VALUES (99,999,1,'Orphan','2026-10-10',NULL)",
    "CREATE TRIGGER custom_trigger AFTER UPDATE ON products BEGIN SELECT 1; END",
])
def test_unsupported_drift_or_invalid_data_fails_without_reset(tmp_path, monkeypatch, alter):
    database = tmp_path / "drift.db"
    cfg = old_head(monkeypatch, database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(alter)
        db.commit()
    before_rows, before_schema = rows(database), signature(database)
    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "head")
    assert revision(database) == "20261016_3b"
    assert rows(database) == before_rows
    assert signature(database) == before_schema


def test_failed_legacy_upgrade_rolls_back_all_revisions(tmp_path, monkeypatch):
    database = tmp_path / "unsupported-legacy.db"
    legacy(database, True)
    with closing(sqlite3.connect(database)) as db:
        db.execute("ALTER TABLE products ADD COLUMN custom_column TEXT")
        db.commit()
    before_rows, before_schema = rows(database), signature(database)
    cfg = configuration(monkeypatch, database)
    with pytest.raises(RuntimeError, match="Unsupported columns"):
        command.upgrade(cfg, "head")
    assert rows(database) == before_rows
    assert signature(database) == before_schema
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchone() is None


def test_historical_upgrade_ignores_future_model_tables(tmp_path, monkeypatch):
    database = tmp_path / "frozen.db"
    cfg = configuration(monkeypatch, database)
    future = sa.Table("future_model_only", Base.metadata, sa.Column("id", sa.Integer, primary_key=True))
    try:
        command.upgrade(cfg, "head")
        assert "future_model_only" not in signature(database)
    finally:
        Base.metadata.remove(future)


def test_application_import_does_not_initialize_database(tmp_path):
    database = tmp_path / "not-created.db"
    env = os.environ.copy()
    env.update({"DATABASE_URL": f"sqlite:///{database.as_posix()}", "PYTHONPATH": str(PROJECT),
                "STOCKPILOT_SECRET_KEY": "test-only-startup-secret-at-least-32-characters",
                "GEMINI_API_KEY": "test-only-unused"})
    result = subprocess.run([sys.executable, "-B", "-c", "from app.main import app"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not database.exists()


def test_backup_in_wal_mode_excludes_uncommitted_records(tmp_path, monkeypatch):
    database = tmp_path / "wal-source.db"
    configuration(monkeypatch, database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("CREATE TABLE committed_records (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO committed_records VALUES (1,'committed')")
        db.commit()
        db.execute("INSERT INTO committed_records VALUES (2,'uncommitted')")
        backup = backup_db.main()
        with closing(sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)) as copy:
            assert copy.execute("SELECT * FROM committed_records").fetchall() == [(1, "committed")]
        db.rollback()
    assert backup_db._integrity_result(backup) == "ok"


def test_backup_preserves_committed_records_and_opens_independently(tmp_path, monkeypatch):
    database = tmp_path / "source.db"
    cfg = configuration(monkeypatch, database)
    command.upgrade(cfg, "head")
    populate(database)
    expected = rows(database)
    backup = backup_db.main()
    assert rows(backup) == expected
    assert backup_db._integrity_result(backup) == "ok"
    with closing(sqlite3.connect(database)) as db:
        db.execute("UPDATE products SET stock_quantity=99")
        db.commit()
    assert rows(backup) == expected
    assert rows(database) != expected
    # A recovery rehearsal makes another file, never replaces the source database.
    recovery = tmp_path / "recovery-rehearsal.db"
    with closing(sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(recovery)) as dest:
            src.backup(dest)
    assert rows(recovery) == expected
    assert backup_db._integrity_result(recovery) == "ok"
