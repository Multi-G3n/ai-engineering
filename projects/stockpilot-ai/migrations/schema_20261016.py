"""Frozen historical schema. Never import app models or update this snapshot.

Used to repair the bootstrap revision's model dependency and normalize known
pre-5.3 SQLite installations. Future schema changes belong in new revisions.
"""

import sqlalchemy as sa


def metadata(include_users=False):
    schema = sa.MetaData()
    sa.Table(
        "products", schema,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("stock_quantity", sa.Integer, nullable=False),
        sa.Column("minimum_stock", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("unit_price >= 0", name="ck_product_price_nonnegative"),
        sa.CheckConstraint("stock_quantity >= 0", name="ck_product_stock_nonnegative"),
        sa.CheckConstraint("minimum_stock >= 0", name="ck_product_minimum_nonnegative"),
        sa.Index("ix_products_sku", "sku", unique=True),
        sa.Index("ix_products_name", "name"),
    )
    sa.Table(
        "sales_records", schema,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sale_date", sa.Date, nullable=False),
        sa.Column("quantity_sold", sa.Integer, nullable=False),
        sa.Column("unit_selling_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("idempotency_key", sa.String(36)),
        sa.CheckConstraint("quantity_sold > 0", name="ck_sales_quantity_positive"),
        sa.CheckConstraint("unit_selling_price >= 0", name="ck_sales_price_nonnegative"),
        sa.UniqueConstraint("idempotency_key", name="uq_sales_idempotency_key"),
        sa.Index("ix_sales_records_product_id", "product_id"),
        sa.Index("ix_sales_records_sale_date", "sale_date"),
    )
    sa.Table(
        "inventory_movements", schema,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(240), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("sales_record_id", sa.Integer, sa.ForeignKey("sales_records.id", ondelete="RESTRICT"), unique=True),
        sa.CheckConstraint("quantity != 0", name="ck_movement_nonzero"),
        sa.Index("ix_inventory_movements_product_id", "product_id"),
    )
    sa.Table(
        "sales_tracking_days", schema,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sales_date", sa.Date, nullable=False),
        sa.Column("marked_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("product_id", "sales_date", name="uq_tracking_product_date"),
        sa.Index("ix_sales_tracking_days_product_id", "product_id"),
        sa.Index("ix_sales_tracking_days_sales_date", "sales_date"),
    )
    if include_users:
        sa.Table(
            "users", schema,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("username", sa.String(64), nullable=False),
            sa.Column("password_hash", sa.String(256), nullable=False),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
            sa.Column("is_admin", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Index("ix_users_username", "username", unique=True),
        )
    return schema
