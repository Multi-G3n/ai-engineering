from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import database_url
from app.database import Base
from app import models  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
url = database_url()
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        if connection.dialect.name == "sqlite":
            # SQLite table rebuilds must not cascade-delete referencing records.
            # PRAGMA changes must occur outside a transaction. Explicit BEGIN makes
            # DDL and alembic_version changes roll back together on any failure.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                context.configure(connection=connection, target_metadata=target_metadata,
                                  compare_type=True, render_as_batch=True, transactional_ddl=True)
                with context.begin_transaction():
                    context.run_migrations()
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("Migration aborted: foreign-key integrity check failed.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
        else:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
