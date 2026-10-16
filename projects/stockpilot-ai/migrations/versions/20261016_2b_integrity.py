"""Add sale idempotency and explicit stock movement linkage.

Revision ID: 20261016_2b
Revises: None
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261016_2b"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # create_all creates only absent tables; it never replaces existing business tables.
    from app.database import Base
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=bind, checkfirst=True)
    inspector = inspect(bind)
    movement_columns = {column["name"] for column in inspector.get_columns("inventory_movements")}
    if "sales_record_id" not in movement_columns:
        op.execute(sa.text(
            "ALTER TABLE inventory_movements ADD COLUMN sales_record_id INTEGER "
            "REFERENCES sales_records(id) ON DELETE RESTRICT"
        ))
    sales_columns = {column["name"] for column in inspect(bind).get_columns("sales_records")}
    if "idempotency_key" not in sales_columns:
        op.add_column("sales_records", sa.Column("idempotency_key", sa.String(36), nullable=True))
    indexes = {index["name"] for index in inspect(bind).get_indexes("sales_records")}
    if "uq_sales_idempotency_key" not in indexes:
        op.create_index("uq_sales_idempotency_key", "sales_records", ["idempotency_key"], unique=True)
    indexes = {index["name"] for index in inspect(bind).get_indexes("inventory_movements")}
    if "uq_inventory_movement_sales_record" not in indexes:
        op.create_index("uq_inventory_movement_sales_record", "inventory_movements", ["sales_record_id"], unique=True)

    # Link only exact, unique matches from the historical movement reason convention.
    # Ambiguous/unmatched rows remain NULL because historical evidence can be incomplete.
    if bind.dialect.name == "sqlite":
        op.execute(sa.text("""
            UPDATE inventory_movements
            SET sales_record_id = CAST(substr(reason, 30, length(reason) - 30) AS INTEGER)
            WHERE reason GLOB 'Sale recorded (sales record #[0-9]*)'
              AND (SELECT COUNT(*) FROM inventory_movements m2
                   WHERE m2.reason = inventory_movements.reason
                     AND m2.product_id = inventory_movements.product_id
                     AND m2.quantity = inventory_movements.quantity) = 1
              AND EXISTS (SELECT 1 FROM sales_records s
                          WHERE s.id = CAST(substr(inventory_movements.reason, 30, length(inventory_movements.reason) - 30) AS INTEGER)
                            AND s.product_id = inventory_movements.product_id
                            AND -s.quantity_sold = inventory_movements.quantity)
        """))


def downgrade():
    raise RuntimeError("Downgrade is disabled because removing linkage/idempotency schema could lose business data. Restore a verified pre-upgrade backup instead.")
