"""Add database-backed administrator accounts.

Revision ID: 20261016_3b
Revises: 20261016_2b
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261016_3b"
down_revision = "20261016_2b"
branch_labels = None
depends_on = None


def upgrade():
    # The preceding revision calls current metadata.create_all(), which may already
    # create this additive table when upgrading a database from before Milestone 2B.
    if "users" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.String(length=256), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade():
    raise RuntimeError("Downgrade is disabled because dropping user accounts could remove authentication data.")
