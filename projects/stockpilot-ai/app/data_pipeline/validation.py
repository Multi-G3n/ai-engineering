"""Fail closed on source corruption; report ordinary missing coverage separately."""
from datetime import date, timedelta
from decimal import Decimal


def validate_sources(snapshot, as_of, sku=None):
    errors, warnings = [], []
    products = {p["id"]: p for p in snapshot["Product"]}
    selected = {pid for pid, p in products.items() if sku is None or p["sku"] == sku}
    batches = {b["id"]: b for b in snapshot["SalesImportBatch"]}
    owners, sources, covered = {}, {}, set()
    for table, owner, field in (("SalesRecord", "operational", "sale_date"),
                                ("HistoricalSale", "historical", "sale_date"),
                                ("SalesTrackingDay", "operational", "sales_date"),
                                ("HistoricalSalesCoverage", "historical", "sales_date")):
        for record in snapshot[table]:
            pid, day = record["product_id"], record[field]
            if pid not in products:
                errors.append(f"{table}: unknown product {pid}")
                continue
            if pid not in selected:
                continue
            if type(day) is not date:
                errors.append(f"{table}: invalid date")
                continue
            key = (pid, day)
            owners.setdefault(key, set()).add(owner)
            if day >= as_of:
                warnings.append(f"{table}: product {pid} date {day} excluded at/after as-of")
            if "quantity_sold" in record:
                q, price = record["quantity_sold"], record["unit_selling_price"]
                if type(q) is not int or q <= 0:
                    errors.append(f"{table}: invalid quantity for product {pid} date {day}")
                if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
                    errors.append(f"{table}: invalid monetary value for product {pid} date {day}")
                if owner == "historical":
                    batch = batches.get(record["import_batch_id"])
                    if batch is None:
                        errors.append("HistoricalSale: missing import batch")
                    else:
                        sources.setdefault(key, set()).add(batch["source"])
            else:
                marker = (table, key)
                if marker in covered:
                    errors.append(f"{table}: duplicate coverage {key}")
                covered.add(marker)
                if owner == "historical":
                    sources.setdefault(key, set()).add(record["source"])
    for key, values in owners.items():
        if len(values) > 1:
            errors.append(f"Conflicting source ownership: {key}")
    for key, values in sources.items():
        if len(values) > 1:
            errors.append(f"Inconsistent historical coverage/source: {key}")
    return sorted(set(errors)), sorted(set(warnings))


def quality_report(rows, features, products, errors=(), warnings=(), min_complete_days=28):
    if min_complete_days < 1:
        raise ValueError("Readiness minimum must be positive.")
    errors, warnings = list(errors), list(warnings)
    keys = [(r["product_id"], r["date"]) for r in rows]
    if len(keys) != len(set(keys)):
        errors.append("Duplicate product/date rows")
    readiness = []
    for product in products:
        subset = [r for r in rows if r["product_id"] == product["id"]]
        complete = sum(r["is_complete_day"] for r in subset)
        days = sorted(r["date"] for r in subset)
        if days and len(set(days)) != (days[-1] - days[0]).days + 1:
            errors.append(f"Missing dates for product {product['id']}")
        observed = [r["date"] for r in subset if r["observed_units_sold"] is not None]
        fs = [f for f in features if f["product_id"] == product["id"]]
        names = [f"lag_{n}" for n in (1, 7, 14, 28)] + [f"rolling_{stat}_{n}" for stat in ("mean", "std") for n in (7, 14, 28)]
        readiness.append(dict(product_id=product["id"], sku=product["sku"],
            complete_days=complete, missing_targets=len(subset)-complete,
            coverage_percentage=100 * complete / len(subset) if subset else 0,
            earliest_observed_date=min(observed) if observed else None,
            latest_observed_date=max(observed) if observed else None,
            available_feature_counts={name: sum(f[name] is not None for f in fs) for name in names},
            sufficient_observations=complete >= min_complete_days,
            ml_readiness="not assessed; model-specific evaluation required"))
    complete = sum(r["is_complete_day"] for r in rows)
    missing = len(rows)-complete
    if missing:
        warnings.append(f"{missing} days lack verified coverage; demand targets remain null")
    return dict(total_products=len(products), total_dataset_rows=len(rows), completed_days=complete,
                confirmed_zero_sales_days=sum(r["units_sold"] == 0 for r in rows),
                incomplete_days=sum(r["coverage_status"] == "incomplete" for r in rows),
                unknown_days=sum(r["coverage_status"] == "unknown" for r in rows),
                missing_demand_targets=missing, coverage_percentage=100*complete/len(rows) if rows else 0,
                validation_errors=sorted(set(errors)), validation_warnings=sorted(set(warnings)),
                readiness_min_complete_days=min_complete_days, product_readiness=readiness)
