"""Add historical sales, import provenance and explicit coverage.

Revision ID: 20261126_6
Revises: 20261126_53
"""
from alembic import op
import sqlalchemy as sa

revision = "20261126_6"
down_revision = "20261126_53"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sales_import_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("confirmation_key", sa.String(36), nullable=False, unique=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("total_rows", sa.Integer, nullable=False),
        sa.Column("imported_rows", sa.Integer, nullable=False),
        sa.Column("duplicate_rows", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("total_rows >= 0 AND imported_rows >= 0 AND duplicate_rows >= 0", name="ck_import_counts_nonnegative"),
    )
    op.create_table(
        "historical_sales",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("import_batch_id", sa.Integer, sa.ForeignKey("sales_import_batches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("external_sale_id", sa.String(128), nullable=False),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sale_date", sa.Date, nullable=False),
        sa.Column("quantity_sold", sa.Integer, nullable=False),
        sa.Column("unit_selling_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("source_line", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("external_sale_id", name="uq_historical_external_sale_id"),
        sa.CheckConstraint("quantity_sold > 0", name="ck_historical_quantity_positive"),
        sa.CheckConstraint("unit_selling_price >= 0", name="ck_historical_price_nonnegative"),
        sa.CheckConstraint("source_line >= 2", name="ck_historical_source_line"),
    )
    op.create_index("ix_historical_sales_product_id", "historical_sales", ["product_id"])
    op.create_index("ix_historical_sales_sale_date", "historical_sales", ["sale_date"])
    op.create_table(
        "historical_sales_coverage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sales_date", sa.Date, nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reason", sa.String(240), nullable=False),
        sa.Column("marked_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("product_id", "sales_date", name="uq_historical_coverage_product_date"),
    )
    op.create_index("ix_historical_sales_coverage_product_id", "historical_sales_coverage", ["product_id"])
    op.create_index("ix_historical_sales_coverage_sales_date", "historical_sales_coverage", ["sales_date"])
    # Product/date ownership is deliberately conservative: operational records lack
    # external IDs, so transaction-level matching across the two stores is unsafe.
    for table, date_column in (("sales_records", "sale_date"), ("sales_tracking_days", "sales_date")):
        for event in ("INSERT", "UPDATE"):
            op.execute(sa.text(f"""CREATE TRIGGER guard_{table}_{event.lower()} BEFORE {event} ON {table}
                WHEN EXISTS(SELECT 1 FROM historical_sales WHERE product_id=NEW.product_id AND sale_date=NEW.{date_column})
                  OR EXISTS(SELECT 1 FROM historical_sales_coverage WHERE product_id=NEW.product_id AND sales_date=NEW.{date_column})
                BEGIN SELECT RAISE(ABORT, 'historical_operational_overlap'); END"""))
    for table, date_column in (("historical_sales", "sale_date"), ("historical_sales_coverage", "sales_date")):
        for event in ("INSERT", "UPDATE"):
            op.execute(sa.text(f"""CREATE TRIGGER guard_{table}_{event.lower()} BEFORE {event} ON {table}
                WHEN EXISTS(SELECT 1 FROM sales_records WHERE product_id=NEW.product_id AND sale_date=NEW.{date_column})
                  OR EXISTS(SELECT 1 FROM sales_tracking_days WHERE product_id=NEW.product_id AND sales_date=NEW.{date_column})
                BEGIN SELECT RAISE(ABORT, 'historical_operational_overlap'); END"""))
    op.execute(sa.text("""CREATE TRIGGER guard_historical_complete BEFORE INSERT ON historical_sales
        WHEN EXISTS(SELECT 1 FROM historical_sales_coverage WHERE product_id=NEW.product_id AND sales_date=NEW.sale_date)
        BEGIN SELECT RAISE(ABORT, 'historical_day_already_complete'); END"""))
    op.execute(sa.text("""CREATE TRIGGER guard_historical_source BEFORE INSERT ON historical_sales
        WHEN EXISTS(SELECT 1 FROM historical_sales h JOIN sales_import_batches old ON old.id=h.import_batch_id
                    JOIN sales_import_batches incoming ON incoming.id=NEW.import_batch_id
                    WHERE h.product_id=NEW.product_id AND h.sale_date=NEW.sale_date AND old.source!=incoming.source)
        BEGIN SELECT RAISE(ABORT, 'historical_source_overlap'); END"""))


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve imported history and provenance. Restore a verified backup separately.")
