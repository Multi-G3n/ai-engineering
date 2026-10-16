from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


PROJECT = Path(__file__).resolve().parents[1]


def test_migration_preserves_legacy_inventory_rows_and_unmatched_history(tmp_path, monkeypatch):
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, sku VARCHAR(64) NOT NULL UNIQUE, name VARCHAR(160) NOT NULL,
                category VARCHAR(100) NOT NULL, unit_price NUMERIC(10,2) NOT NULL,
                stock_quantity INTEGER NOT NULL, minimum_stock INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE inventory_movements (
                id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL, reason VARCHAR(240) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE sales_records (
                id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                sale_date DATE NOT NULL, quantity_sold INTEGER NOT NULL CHECK (quantity_sold > 0),
                unit_selling_price NUMERIC(10,2) NOT NULL CHECK (unit_selling_price >= 0),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(text("INSERT INTO products VALUES (1, 'OLD-1', 'Legacy item', 'Test', 4.25, 8, 2, CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO inventory_movements VALUES (1, 1, 8, 'Opening stock', CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO sales_records VALUES (77, 1, '2026-10-10', 1, 4.25, CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO inventory_movements VALUES (2, 1, -1, 'Sale recorded (sales record #77)', CURRENT_TIMESTAMP)"))
        # Same reason and amount is ambiguous, so neither movement may be linked by inference.
        connection.execute(text("INSERT INTO sales_records VALUES (78, 1, '2026-10-11', 1, 4.25, CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO inventory_movements VALUES (3, 1, -1, 'Sale recorded (sales record #78)', CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO inventory_movements VALUES (4, 1, -1, 'Sale recorded (sales record #78)', CURRENT_TIMESTAMP)"))
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    config = Config(str(PROJECT / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT sku, stock_quantity FROM products WHERE id=1")).one() == ("OLD-1", 8)
        assert connection.execute(text("SELECT COUNT(*) FROM inventory_movements")).scalar_one() == 4
        assert connection.execute(text("SELECT sales_record_id FROM inventory_movements WHERE id=2")).scalar_one() == 77
        assert connection.execute(text("SELECT sales_record_id FROM inventory_movements WHERE id IN (3,4) ORDER BY id")).all() == [(None,), (None,)]
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261016_3b"
    inspector = inspect(engine)
    assert {"sales_records", "sales_tracking_days", "users"}.issubset(inspector.get_table_names())
    assert inspector.get_indexes("users") == [{"name": "ix_users_username", "column_names": ["username"],
                                                 "unique": 1, "dialect_options": {}}]
    assert "sales_record_id" in {c["name"] for c in inspector.get_columns("inventory_movements")}
    engine.dispose()
