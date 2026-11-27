"""Detached source snapshot. SQLite only: explicit read transaction, no ORM flush."""
from sqlalchemy import select
from pathlib import Path
from sqlalchemy.orm import Session

from app.models import (Product, SalesRecord, SalesTrackingDay, HistoricalSale,
                        HistoricalSalesCoverage, SalesImportBatch)


def extract(engine, sku=None):
    if engine.dialect.name != "sqlite":
        raise ValueError("Consistent extraction currently supports SQLite only.")
    database = engine.url.database
    if database not in (None, "", ":memory:") and not engine.url.query.get("uri") and not Path(database).is_file():
        raise ValueError("Source database must already exist.")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only=ON")
        try:
            connection.exec_driver_sql("BEGIN")
            with Session(bind=connection, autoflush=False) as db:
                tables = {}
                for model in (Product, SalesRecord, SalesTrackingDay, HistoricalSale,
                              HistoricalSalesCoverage, SalesImportBatch):
                    tables[model.__name__] = [
                        {c.name: getattr(row, c.name) for c in model.__table__.columns}
                        for row in db.scalars(select(model).order_by(model.id))
                    ]
            if sku is not None and not any(p["sku"] == sku for p in tables["Product"]):
                raise ValueError("Unknown product SKU.")
            return tables
        finally:
            connection.rollback()
            connection.exec_driver_sql("PRAGMA query_only=OFF")
            connection.rollback()
