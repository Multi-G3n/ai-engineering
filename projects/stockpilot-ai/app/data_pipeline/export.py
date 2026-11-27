"""Deterministic exports into a newly created directory; never replace existing files."""
import csv
import hashlib
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

DAILY_FIELDS = ('product_id', 'sku', 'date', 'units_sold', 'sales_value', 'transaction_count',
                'observed_units_sold', 'observed_sales_value', 'observed_transaction_count',
                'coverage_status', 'data_source', 'is_complete_day')
FEATURE_FIELDS = (*DAILY_FIELDS, 'day_of_week', 'day_of_month', 'month', 'is_weekend',
                  *(f'lag_{n}' for n in (1, 7, 14, 28)),
                  *(name for n in (7, 14, 28) for name in
                    (f'completed_days_{n}', f'rolling_mean_{n}', f'rolling_std_{n}')))


def serialize(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, '.2f')
    raise TypeError('Unsupported export value')


def csv_bytes(rows, fields):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        # Neutralize spreadsheet formulas in text metadata. Canonical database values
        # remain untouched; this reversible prefix is documented in metadata.
        writer.writerow({key: ("'" + value if isinstance(value, str) and
                               value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n'))
                               else serialize(value) if isinstance(value, (date, Decimal))
                               else value) for key, value in row.items()})
    return buffer.getvalue().encode('utf-8')


def export_dataset(dataset, output):
    rows, features, report, original_metadata = dataset
    daily = csv_bytes(rows, DAILY_FIELDS)
    engineered = csv_bytes(features, FEATURE_FIELDS)
    metadata = dict(original_metadata, generated_at=datetime.now(timezone.utc).isoformat(),
                    csv_text_encoding="UTF-8; formula-like text prefixed with apostrophe; null is empty",
                    dataset_checksum=hashlib.sha256(daily + engineered).hexdigest(),
                    file_checksums={name: hashlib.sha256(data).hexdigest() for name, data in
                                    (("daily_demand.csv", daily), ("demand_features.csv", engineered))})
    files = {"daily_demand.csv": daily, "demand_features.csv": engineered,
             "data_quality_report.json": (json.dumps(report, default=serialize, sort_keys=True, indent=2)+'\n').encode(),
             "dataset_metadata.json": (json.dumps(metadata, default=serialize, sort_keys=True, indent=2)+'\n').encode()}
    target = Path(output).absolute()
    if '..' in target.parts:
        raise ValueError("Output path must not contain parent traversal.")
    if any(parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction())
           for parent in (target, *target.parents)):
        raise ValueError("Output path must not traverse links or junctions.")
    # Parent must already exist. Atomic mkdir is the reservation against concurrent exports.
    target.mkdir(exist_ok=False)
    for name, contents in files.items():
        with (target / name).open('xb') as stream:
            stream.write(contents)
    return target
