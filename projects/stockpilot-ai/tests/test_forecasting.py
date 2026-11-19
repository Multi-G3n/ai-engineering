
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.forecasting import (
    calculate_recommendation,
    round_up,
)


def product(stock=20, minimum=5):
    return SimpleNamespace(
        id=1,
        sku="TEST-001",
        name="Test Product",
        stock_quantity=stock,
        minimum_stock=minimum,
    )


def test_round_up():
    assert round_up(Decimal("3.1")) == 4
    assert round_up(Decimal("3.0")) == 3


def test_seven_and_thirty_day_demand():
    result = calculate_recommendation(
        product(),
        daily_demand=Decimal("2.5"),
        tracked_days=10,
    )

    assert result.demand_7_days == 18
    assert result.demand_30_days == 75
    assert result.tracked_days == 10


def test_high_stockout_risk():
    result = calculate_recommendation(
        product(stock=10),
        daily_demand=Decimal("2"),
    )

    assert result.risk == "High risk"
    assert result.days_of_stock == Decimal("5")


def test_reorder_quantity():
    result = calculate_recommendation(
        product(stock=10),
        daily_demand=Decimal("2"),
        lead_time_days=7,
        safety_stock_days=7,
    )

    assert result.reorder_quantity == 18


def test_no_sales_history():
    result = calculate_recommendation(
        product(stock=2),
        daily_demand=None,
    )

    assert result.risk == "Insufficient data"
    assert result.reorder_quantity is None
    assert result.demand_7_days is None


def test_zero_demand():
    result = calculate_recommendation(
        product(stock=20),
        daily_demand=Decimal("0"),
    )

    assert result.risk == "Healthy"
    assert result.days_of_stock is None
    assert result.reorder_quantity == 0


def test_out_of_stock():
    result = calculate_recommendation(
        product(stock=0),
        daily_demand=Decimal("2"),
    )

    assert result.risk == "Out of stock"


def test_negative_demand_rejected():
    with pytest.raises(ValueError):
        calculate_recommendation(
            product(),
            daily_demand=Decimal("-1"),
        )


def test_negative_planning_days_rejected():
    with pytest.raises(ValueError):
        calculate_recommendation(
            product(),
            daily_demand=Decimal("2"),
            lead_time_days=-1,
        )
