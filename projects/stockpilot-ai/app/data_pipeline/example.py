"""Synthetic example: python -m app.data_pipeline.example --output-dir datasets/example"""
import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Product, SalesRecord, SalesTrackingDay
from . import generate
from .export import export_dataset


def main():
    parser = argparse.ArgumentParser(description="Export synthetic history from an isolated temporary database.")
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='stockpilot-synthetic-') as temp:
        engine = create_engine(f"sqlite:///{Path(temp) / 'synthetic.db'}")
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                db.add(Product(id=1, sku='SYNTHETIC-ONLY', name='Synthetic example',
                               unit_price=Decimal('1.25'), stock_quantity=0,
                               created_at=datetime(2026, 1, 1)))
                db.flush()
                for n in range(42):
                    day = date(2026, 1, 1) + timedelta(days=n)
                    if n != 10:  # one unknown day
                        db.add(SalesTrackingDay(product_id=1, sales_date=day))
                    if n % 7 and n != 10:
                        db.add(SalesRecord(product_id=1, sale_date=day,
                                           quantity_sold=n % 5 + 1, unit_selling_price=Decimal('1.25')))
                db.commit()
            result = generate(engine, date(2026, 1, 1), date(2026, 2, 11), date(2026, 2, 12))
            assert len(result[0]) == 42
            assert result[2]['completed_days'] == 41
            assert result[2]['unknown_days'] == 1
            assert result[1][39]['rolling_mean_28'] is not None
            path = export_dataset(result, args.output_dir)
            print(f"Verified synthetic export: {path}; 42 rows, 41 complete, 6 zero-sales, 1 unknown")
        finally:
            engine.dispose()


if __name__ == '__main__':
    main()
