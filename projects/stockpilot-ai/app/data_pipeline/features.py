"""Strictly preceding calendar-day features; full windows required for statistics."""
from datetime import timedelta
from statistics import mean, stdev

WINDOWS = (7, 14, 28)
LAGS = (1, 7, 14, 28)
FEATURE_DEFINITIONS = {
    "calendar": "day_of_week Monday=0; day_of_month; month; is_weekend Saturday/Sunday",
    "lags": "Exact preceding calendar dates: 1, 7, 14, 28; complete targets only",
    "rolling": "Preceding 7/14/28 calendar days, excluding target; all days complete required; sample standard deviation (ddof=1)",
    "coverage": "Number of completed targets in preceding 7/14/28 calendar days",
}


def engineer(rows):
    lookup = {(r["product_id"], r["date"]): r["units_sold"] for r in rows}
    result = []
    for row in rows:
        day, pid = row["date"], row["product_id"]
        item = dict(row, day_of_week=day.weekday(), day_of_month=day.day,
                    month=day.month, is_weekend=day.weekday() >= 5)
        for lag in LAGS:
            item[f"lag_{lag}"] = lookup.get((pid, day - timedelta(days=lag)))
        for window in WINDOWS:
            values = [lookup.get((pid, day - timedelta(days=i))) for i in range(1, window + 1)]
            count = sum(v is not None for v in values)
            item[f"completed_days_{window}"] = count
            item[f"rolling_mean_{window}"] = mean(values) if count == window else None
            item[f"rolling_std_{window}"] = stdev(values) if count == window else None
        result.append(item)
    return result
