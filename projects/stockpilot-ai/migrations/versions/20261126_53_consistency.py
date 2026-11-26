"""Normalize supported SQLite schemas without changing business fields.

Revision ID: 20261126_53
Revises: 20261016_3b
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_20261016 import metadata

revision = "20261126_53"
down_revision = "20261016_3b"
branch_labels = None
depends_on = None


def _normalized(value):
    return "".join(str(value if value is not None else "").lower().split()).strip("()'\"")


def _preflight(bind, schema):
    inspector = sa.inspect(bind)
    # Refuse to silently remove custom schema objects during canonical rebuilds.
    if inspector.get_view_names() or bind.execute(sa.text(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1"
    )).first():
        raise RuntimeError("Unsupported custom views/triggers; review the database before upgrading.")
    for table in schema.sorted_tables:
        name = table.name
        definition = bind.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
        ), {"name": name}).scalar_one().upper()
        if any(option in definition for option in ("AUTOINCREMENT", "WITHOUT ROWID", "DEFERRABLE")):
            raise RuntimeError(f"Unsupported table options in {name}.")
        columns = inspector.get_columns(name)
        if {column["name"] for column in columns} != set(table.c.keys()):
            raise RuntimeError(f"Unsupported columns in {name}; migration stopped without resetting data.")
        if inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"Unsupported primary key in {name}.")
        for column in columns:
            expected = table.c[column["name"]]
            if column.get("computed") or column.get("identity"):
                raise RuntimeError(f"Unsupported generated column in {name}.")
            if str(column["type"]).upper() != str(expected.type).upper():
                raise RuntimeError(f"Unsupported column type in {name}.")
            default = expected.server_default.arg if expected.server_default is not None else None
            if isinstance(expected.type, sa.DateTime):
                actual_default = str(column["default"] or "").strip().lower().strip("()")
                expected_default = str(default).lower()
            else:
                actual_default, expected_default = _normalized(column["default"]), _normalized(default)
            if actual_default != expected_default:
                raise RuntimeError(f"Unsupported server default in {name}.")
            if not expected.nullable and bind.execute(sa.text(
                f'SELECT 1 FROM "{name}" WHERE "{expected.name}" IS NULL LIMIT 1'
            )).first():
                raise RuntimeError(f"Invalid NULL data in {name}; correct data before upgrading.")
        allowed_indexes = {index.name: (tuple(c.name for c in index.columns), bool(index.unique))
                           for index in table.indexes}
        # Original 2b added redundant unique indexes, even on fresh databases.
        if name == "sales_records":
            allowed_indexes["uq_sales_idempotency_key"] = (("idempotency_key",), True)
        if name == "inventory_movements":
            allowed_indexes["uq_inventory_movement_sales_record"] = (("sales_record_id",), True)
        for index in inspector.get_indexes(name):
            if (index.get("dialect_options") or
                    allowed_indexes.get(index["name"]) != (tuple(index["column_names"]), bool(index["unique"]))):
                raise RuntimeError(f"Unsupported custom index in {name}.")
        uniques = {tuple(c.name for c in constraint.columns) for constraint in table.constraints
                   if isinstance(constraint, sa.UniqueConstraint)}
        uniques.update(tuple(c.name for c in index.columns) for index in table.indexes if index.unique)
        for constraint in inspector.get_unique_constraints(name):
            if tuple(constraint["column_names"]) not in uniques:
                raise RuntimeError(f"Unsupported unique constraint in {name}.")
        checks = {_normalized(constraint.sqltext) for constraint in table.constraints
                  if isinstance(constraint, sa.CheckConstraint)}
        for constraint in inspector.get_check_constraints(name):
            if _normalized(constraint["sqltext"]) not in checks:
                raise RuntimeError(f"Unsupported check constraint in {name}.")
        foreign_keys = {(tuple(fk.parent.name for fk in constraint.elements),
                         constraint.referred_table.name,
                         tuple(fk.column.name for fk in constraint.elements), constraint.ondelete)
                        for constraint in table.foreign_key_constraints}
        # PRAGMA is authoritative: SQLAlchemy's SQL parser can miss ON DELETE on
        # inline legacy references even though SQLite enforces those actions.
        for fk in bind.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")').mappings():
            actual = ((fk["from"],), fk["table"], (fk["to"],), fk["on_delete"])
            if actual not in foreign_keys or fk["on_update"] != "NO ACTION":
                raise RuntimeError(f"Unsupported foreign key in {name}.")
        for unique in uniques:
            fields = ", ".join(f'"{field}"' for field in unique)
            nonnull = " AND ".join(f'"{field}" IS NOT NULL' for field in unique)
            if bind.execute(sa.text(
                f'SELECT 1 FROM "{name}" WHERE {nonnull} GROUP BY {fields} HAVING COUNT(*) > 1 LIMIT 1'
            )).first():
                raise RuntimeError(f"Duplicate data in {name}; correct data before upgrading.")
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint) and bind.execute(sa.text(
                f'SELECT 1 FROM "{name}" WHERE NOT ({constraint.sqltext}) LIMIT 1'
            )).first():
                raise RuntimeError(f"Invalid data in {name}; correct data before upgrading.")
        for fk in table.foreign_keys:
            if bind.execute(sa.text(
                f'SELECT 1 FROM "{name}" child LEFT JOIN "{fk.column.table.name}" parent '
                f'ON child."{fk.parent.name}" = parent."{fk.column.name}" '
                f'WHERE child."{fk.parent.name}" IS NOT NULL AND parent."{fk.column.name}" IS NULL LIMIT 1'
            )).first():
                raise RuntimeError(f"Orphaned data in {name}; correct data before upgrading.")


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Phase 5.3 supports SQLite only; review other dialects separately.")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0 or not bind.in_transaction():
        raise RuntimeError("Use the project Alembic environment for safe transactional table rebuilds.")
    schema = metadata(include_users=True)
    _preflight(bind, schema)
    # The frozen target defines exactly the same columns. Batch-copy preserves IDs,
    # timestamps, NULL history tokens, links and credentials; no inferred data edits.
    for table in schema.sorted_tables:
        with op.batch_alter_table(table.name, recreate="always", copy_from=table):
            pass


def downgrade():
    raise RuntimeError("Downgrade is disabled. Preserve the newer database and restore a verified backup separately.")
