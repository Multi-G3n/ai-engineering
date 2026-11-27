"""Canonical demand targets and separate partial-observation diagnostics."""
from datetime import timedelta
from decimal import Decimal


def daily_rows(snapshot, start, end, as_of, sku=None):
    products = sorted((p for p in snapshot["Product"] if sku is None or p["sku"] == sku),
                      key=lambda p: p["id"])
    rows = []
    end = min(end, as_of - timedelta(days=1))
    for product in products:
        pid = product["id"]
        sales, coverage = {}, {}
        for table, source in (("SalesRecord", "operational"), ("HistoricalSale", "historical")):
            for sale in snapshot[table]:
                if sale["product_id"] == pid:
                    sales.setdefault(sale["sale_date"], []).append((source, sale))
        for table, source in (("SalesTrackingDay", "operational"), ("HistoricalSalesCoverage", "historical")):
            for item in snapshot[table]:
                if item["product_id"] == pid:
                    coverage[item["sales_date"]] = source
        # Creation is known existence, not evidence of availability or complete demand.
        known = [product["created_at"].date(), *sales, *coverage]
        day = max(start, min(known))
        while day <= end:
            observed = sales.get(day, [])
            complete = day in coverage
            quantity = sum(s["quantity_sold"] for _, s in observed)
            value = sum((s["quantity_sold"] * s["unit_selling_price"] for _, s in observed), Decimal("0.00"))
            source = coverage.get(day) or (observed[0][0] if observed else "unknown")
            rows.append(dict(product_id=pid, sku=product["sku"], date=day,
                             units_sold=quantity if complete else None,
                             sales_value=value if complete else None,
                             transaction_count=len(observed) if complete else None,
                             observed_units_sold=quantity if observed or complete else None,
                             observed_sales_value=value if observed or complete else None,
                             observed_transaction_count=len(observed) if observed or complete else None,
                             coverage_status="complete" if complete else "incomplete" if observed else "unknown",
                             data_source=source, is_complete_day=complete))
            day += timedelta(days=1)
    return rows
