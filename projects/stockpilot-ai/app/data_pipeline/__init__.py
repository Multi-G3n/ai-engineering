"""Read-only retrospective dataset generation, independent of forecasting formulas."""
from datetime import date, timedelta

from .extraction import extract
from .transformation import daily_rows
from .features import engineer, FEATURE_DEFINITIONS
from .validation import validate_sources, quality_report

LIMITATIONS = [
    "Retrospective dataset, not point-in-time-safe backtesting data. Creation/import/coverage timestamps do not supply a complete revision history or historical SKU mapping.",
    "SKU is current metadata; inventory, current price, names and administrator data are not exported.",
    "Known existence does not establish product availability; sales do not measure unconstrained demand or stockout losses.",
]


class DataQualityError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Source validation failed: " + "; ".join(errors))


def generate(engine, start, end, as_of, sku=None, min_complete_days=28):
    if any(type(d) is not date for d in (start, end, as_of)):
        raise ValueError("Dates must be calendar dates.")
    if start > end or start >= as_of or as_of > date.today() or (end-start).days >= 3660:
        raise ValueError("Invalid range: start <= end, start < as-of <= today; maximum 3660 days.")
    if min_complete_days < 1:
        raise ValueError("Readiness minimum must be positive.")
    snapshot = extract(engine, sku)
    errors, warnings = validate_sources(snapshot, as_of, sku)
    if errors:
        raise DataQualityError(errors)
    # Extract 28 preceding days for features, then trim to requested export range.
    history_start = start - timedelta(days=min(28, start.toordinal()-1))
    history = daily_rows(snapshot, history_start, end, as_of, sku)
    rows = [r for r in history if r["date"] >= start]
    features = [r for r in engineer(history) if r["date"] >= start]
    products = [p for p in snapshot["Product"] if sku is None or p["sku"] == sku]
    report = quality_report(rows, features, products, errors, warnings, min_complete_days)
    if report["validation_errors"]:
        raise DataQualityError(report["validation_errors"])
    metadata = dict(schema_version="1.0", dataset_kind="retrospective", requested_start=start,
                    requested_end=end, effective_start=min((r["date"] for r in rows), default=None),
                    effective_end=max((r["date"] for r in rows), default=None), as_of=as_of,
                    product_selection=sku, sku_semantics="current metadata",
                    feature_definitions=FEATURE_DEFINITIONS,
                    coverage_rules="Explicit matching-source coverage only; dates strictly before as-of; null incomplete targets",
                    row_counts=dict(daily=len(rows), features=len(features)), known_limitations=LIMITATIONS)
    return rows, features, report, metadata
