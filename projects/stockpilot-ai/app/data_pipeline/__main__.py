"""python -m app.data_pipeline --help"""
import argparse
from datetime import date
from sqlalchemy.exc import SQLAlchemyError

from app.database import make_engine
from . import generate
from .export import export_dataset


def main():
    parser = argparse.ArgumentParser(description="Read-only retrospective demand dataset export (SQLite).")
    parser.add_argument('--start-date', required=True, type=date.fromisoformat)
    parser.add_argument('--end-date', required=True, type=date.fromisoformat)
    parser.add_argument('--as-of-date', required=True, type=date.fromisoformat)
    parser.add_argument('--sku')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--min-complete-days', type=int, default=28)
    args = parser.parse_args()
    engine = make_engine()
    try:
        result = generate(engine, args.start_date, args.end_date, args.as_of_date,
                          args.sku, args.min_complete_days)
        path = export_dataset(result, args.output_dir)
    except (ValueError, OSError, SQLAlchemyError):
        parser.exit(2, 'Export failed: invalid data, parameters, or output path; no existing export was overwritten.\n')
    finally:
        engine.dispose()
    print(f"Exported {len(result[0])} rows to {path}")


if __name__ == '__main__':
    main()
