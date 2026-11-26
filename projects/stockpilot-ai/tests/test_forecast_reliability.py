"""Phase 5.2: inventory truth, history completeness and reporting consistency."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai_service import ask_inventory_assistant, build_inventory_snapshot
from app.database import Base, get_db
from app.forecasting import build_forecast_report, calculate_recommendation
from app.main import app
from app.models import Product, SalesRecord, SalesTrackingDay
from app.services import daily_demand_summary, demand_forecast_history, simple_demand_forecast
from conftest import add_test_admin, login_test_client, reset_login_limiter

AS_OF = date(2026, 11, 26)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def product(db, sku="TEST", stock=10, minimum=5, completed=(), quantities=None):
    item = Product(sku=sku, name=sku, category="Test", unit_price=Decimal("10.00"),
                   stock_quantity=stock, minimum_stock=minimum)
    db.add(item)
    db.flush()
    for days_ago in completed:
        db.add(SalesTrackingDay(product_id=item.id, sales_date=AS_OF - timedelta(days=days_ago)))
    for days_ago, quantity in (quantities or {}).items():
        db.add(SalesRecord(product_id=item.id, sale_date=AS_OF - timedelta(days=days_ago),
                           quantity_sold=quantity, unit_selling_price=Decimal("10.00")))
    db.commit()
    return item


@pytest.mark.parametrize("stock,daily,days,physical,available,risk", [
    (0, None, 0, "Out of stock", False, None),
    (0, None, 6, "Out of stock", False, None),
    (0, Decimal("2"), 7, "Out of stock", True, "High risk"),
    (0, Decimal("0"), 7, "Out of stock", True, "No observed demand"),
    (2, None, 6, "Low stock", False, None),
    (5, None, 6, "Low stock", False, None),
    (6, None, 6, "In stock", False, None),
    (5, Decimal("0"), 7, "Low stock", True, "No observed demand"),
    (5, Decimal("0.5"), 7, "Low stock", True, "Low risk"),
])
def test_independent_status_dimensions(stock, daily, days, physical, available, risk):
    item = SimpleNamespace(id=1, sku="TEST", name="Test", stock_quantity=stock, minimum_stock=5)
    result = calculate_recommendation(item, daily, tracked_days=days)
    assert result.physical_inventory_status == physical
    assert result.forecast_available is available
    assert result.forecast_availability == ("Available" if available else "Insufficient data")
    assert result.forecast_risk == risk
    assert result.tracked_days == days
    if stock == 0:
        assert result.risk == "Out of stock"  # Corrected legacy combined label.
    if not available:
        assert result.daily_demand is None
        assert result.demand_7_days is None
        assert result.demand_30_days is None
        assert result.days_of_stock is None
        assert result.reorder_quantity is None


@pytest.mark.parametrize("days", [0, 6, 7, 28])
def test_tracking_threshold_preserves_actual_count(db, days):
    item = product(db, completed=range(1, days + 1))
    history = demand_forecast_history(db, item.id, AS_OF)
    recommendation = build_forecast_report(db, AS_OF)["products"][0]
    assert history.tracked_days == days
    assert recommendation.tracked_days == days
    assert recommendation.forecast_available is (days >= 7)
    forecast = simple_demand_forecast(db, item.id, AS_OF)
    if days >= 7:
        assert forecast.daily_average == Decimal("0")
        assert forecast.weekly_estimate == Decimal("0")
        assert recommendation.forecast_risk == "No observed demand"
    else:
        assert forecast is None


def test_window_includes_day_28_excludes_day_29_today_and_future(db):
    item = product(db, completed=[1, 2, 3, 4, 5, 6, 28, 29, 0, -1],
                   quantities={28: 14, 29: 700, 0: 700, -1: 700})
    history = demand_forecast_history(db, item.id, AS_OF)
    assert history.tracked_days == 7
    assert history.forecast.daily_average == Decimal("2")
    assert history.forecast.weekly_estimate == Decimal("14")
    assert history.forecast.lookback_days == 28


def test_today_and_expired_coverage_cannot_unlock_forecast(db):
    item = product(db, completed=[1, 2, 3, 4, 5, 6, 29, 0])
    history = demand_forecast_history(db, item.id, AS_OF)
    assert history.tracked_days == 6
    assert history.forecast is None


def test_six_complete_sales_days_keep_all_forecast_values_unknown(db):
    product(db, stock=0, completed=range(1, 7), quantities={i: 2 for i in range(1, 7)})
    report = build_forecast_report(db, AS_OF)
    item = report["products"][0]
    assert item.tracked_days == 6
    assert item.daily_demand is None
    assert item.reorder_quantity is None
    assert report["out_of_stock_count"] == 1
    assert report["forecast_available_count"] == 0


def test_zero_stock_and_zero_minimum_remain_out_of_stock(db):
    product(db, stock=0, minimum=0, completed=range(1, 8))
    item = build_forecast_report(db, AS_OF)["products"][0]
    assert item.physical_inventory_status == "Out of stock"
    assert item.forecast_risk == "No observed demand"
    assert item.reorder_quantity == 0


def test_forecast_risk_at_lead_time_boundary():
    item = SimpleNamespace(id=1, sku="TEST", name="Test", stock_quantity=14, minimum_stock=5)
    assert calculate_recommendation(item, Decimal("2"), tracked_days=7).forecast_risk == "High risk"
    item.stock_quantity = 15
    assert calculate_recommendation(item, Decimal("2"), tracked_days=7).forecast_risk == "Low risk"


def test_confirmed_zero_and_unknown_partial_sales_are_distinct(db):
    item = product(db, completed=range(1, 8), quantities={1: 14, 8: 1000})
    points = daily_demand_summary(db, item.id, AS_OF - timedelta(days=9), AS_OF - timedelta(days=1))
    assert [p.demand_quantity for p in points[:3]] == [None, None, 0]
    assert points[1].recorded_quantity == 1000
    history = demand_forecast_history(db, item.id, AS_OF)
    assert history.tracked_days == 7
    assert history.forecast.daily_average == Decimal("2")


def mixed_products(db):
    product(db, "A-empty-unknown", stock=0, completed=range(1, 7))
    product(db, "B-empty-demand", stock=0, completed=range(1, 8), quantities={i: 2 for i in range(1, 8)})
    product(db, "C-empty-zero", stock=0, completed=range(1, 8))
    product(db, "D-low-unknown", stock=5, completed=range(1, 7))
    product(db, "E-low-predicted", stock=5, completed=range(1, 8), quantities={i: 2 for i in range(1, 8)})
    product(db, "F-in-stock-unknown", stock=6)
    product(db, "G-in-stock-predicted", stock=10, completed=range(1, 8), quantities={i: 2 for i in range(1, 8)})


def test_summary_has_exclusive_physical_buckets_and_unique_attention(db):
    mixed_products(db)
    report = build_forecast_report(db, AS_OF)
    assert report["total_products"] == 7
    assert report["out_of_stock_count"] == 3
    assert report["physical_low_stock_count"] == 2
    assert report["in_stock_count"] == 2
    assert report["current_shortage_count"] == 5
    assert report["predicted_shortage_count"] == 2
    assert report["high_risk_count"] == 5  # Three current stockouts + two positive-stock predictions.
    assert report["attention_count"] == 6  # Low + predicted overlap counted once.
    assert report["forecast_available_count"] == 4
    assert report["insufficient_data_count"] == 3
    assert report["recommended_units"] == 74
    assert {item.sku: item.reorder_quantity for item in report["products"]} == {
        "A-empty-unknown": None, "B-empty-demand": 28, "C-empty-zero": 5,
        "D-low-unknown": None, "E-low-predicted": 23, "F-in-stock-unknown": None,
        "G-in-stock-predicted": 18,
    }
    assert [item.sku for item in report["products"][:3]] == ["A-empty-unknown", "B-empty-demand", "C-empty-zero"]


def test_snapshot_matches_report_fields_and_scoped_summary(db):
    mixed_products(db)
    report = build_forecast_report(db, AS_OF)
    snapshot = build_inventory_snapshot(db, AS_OF)
    by_sku = {item.sku: item for item in report["products"]}
    for row in snapshot["products"]:
        item = by_sku[row["sku"]]
        for name in ("physical_inventory_status", "forecast_available", "forecast_availability", "forecast_risk", "tracked_days"):
            assert row[name] == getattr(item, name)
        assert row["stockout_risk"] == item.risk
        assert row["reorder_quantity"] == item.reorder_quantity
        assert row["demand_7_days"] == item.demand_7_days
        if not item.forecast_available:
            assert row["daily_demand_estimate"] is None
            assert row["weekly_demand_estimate"] is None
    for key in ("out_of_stock_count", "physical_low_stock_count", "in_stock_count", "high_risk_count",
                "predicted_shortage_count", "attention_count", "forecast_available_count", "insufficient_data_count"):
        assert snapshot[key] == report[key]
    assert snapshot["low_stock_count"] == 5  # Legacy inclusive threshold count retained.
    assert snapshot["recommended_reorder_units"] == report["recommended_units"]


def test_snapshot_truncation_summaries_cover_only_included_products(db):
    for index in range(51):
        product(db, f"P-{index:02d}", stock=0)
    snapshot = build_inventory_snapshot(db, AS_OF)
    assert snapshot["truncated"] is True
    assert snapshot["product_count"] == 50
    assert snapshot["out_of_stock_count"] == 50
    assert snapshot["high_risk_count"] == 50
    assert build_forecast_report(db, AS_OF)["out_of_stock_count"] == 51


@pytest.fixture
def client(db, monkeypatch):
    factory = sessionmaker(bind=db.bind)
    add_test_admin(factory)
    reset_login_limiter()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr("app.main.build_forecast_report", lambda session: build_forecast_report(session, AS_OF))
    try:
        with TestClient(app, base_url="https://testserver") as browser:
            yield browser
    finally:
        app.dependency_overrides.clear()
        reset_login_limiter()


def test_forecast_page_requires_login(client):
    assert client.get("/forecast", follow_redirects=False).status_code == 303


def test_forecast_page_renders_independent_statuses(client, db):
    mixed_products(db)
    login_test_client(client)
    response = client.get("/forecast")
    assert response.status_code == 200
    for text in ("PHYSICAL STATUS", "FORECAST AVAILABILITY", "COMPLETED DAYS", "FORECAST RISK",
                 "6 / 28", "7 / 28", "Out of stock", "Low stock", "In stock", "Insufficient data",
                 "Available", "High risk", "No observed demand", "Unavailable", "Predicted shortages",
                 "Products needing attention", "Today is excluded"):
        assert text in response.text
    row = response.text.split("A-empty-unknown", 1)[1].split("</tr>", 1)[0]
    assert row.count("Unavailable") == 5
    assert "Out of stock" in row
    assert "Insufficient data" in row


def test_empty_forecast_page(client):
    login_test_client(client)
    response = client.get("/forecast")
    assert response.status_code == 200
    assert 'colspan="10"' in response.text


def test_sales_page_shows_subthreshold_completed_count(client, db, monkeypatch):
    item = product(db, completed=range(1, 7))
    monkeypatch.setattr("app.main.demand_forecast_history",
                        lambda session, product_id: demand_forecast_history(session, product_id, AS_OF))
    login_test_client(client)
    response = client.get("/sales", params={"product_id": item.id})
    assert response.status_code == 200
    assert "6 of the previous 28 days are fully tracked (7 required)" in response.text
    assert "Today is excluded" in response.text


def test_empty_summary_and_snapshot_have_zero_counts(db):
    report = build_forecast_report(db, AS_OF)
    snapshot = build_inventory_snapshot(db, AS_OF)
    for key in ("out_of_stock_count", "physical_low_stock_count", "in_stock_count", "attention_count",
                "forecast_available_count", "insufficient_data_count", "predicted_shortage_count"):
        assert report[key] == snapshot[key] == 0


def test_ai_prompt_explains_independent_statuses_without_provider_calls(monkeypatch):
    generate = Mock(return_value="Explained")
    monkeypatch.setattr("app.ai_service.generate_inventory_answer", generate)
    assert ask_inventory_assistant("What is out of stock?", {"products": []}) == "Explained"
    prompt = generate.call_args.args[0]
    assert "physical_inventory_status" in prompt
    assert "null means unavailable, not low risk" in prompt
    assert "do not add overlapping counts" in prompt
