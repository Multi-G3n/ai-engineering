from datetime import date, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import InventoryMovement, Product, SalesRecord, SalesTrackingDay
from app.schemas import SaleInput
from app.services import (DemandRangeError, daily_demand_summary, mark_sales_day_complete,
                          record_sale)


@pytest.fixture
def sales_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sales-test.db'}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def add_product(client, quantity="12"):
    response = client.post("/products/new", data={
        "sku": "SALE-1", "name": "Sales Test Product", "category": "Test",
        "unit_price": "10.50", "stock_quantity": quantity, "minimum_stock": "2",
    }, follow_redirects=False)
    assert response.status_code == 303


def sale_form(**overrides):
    values = {"product_id": "1", "quantity_sold": "3", "sale_date": "2026-10-15",
              "unit_selling_price": "", "mark_day_complete": "false", "idempotency_key": str(uuid4())}
    values.update(overrides)
    return values


def test_sales_page_is_in_navigation_and_sale_updates_stock_and_movement(sales_env):
    client, session_factory = sales_env
    add_product(client)
    page = client.get("/sales")
    assert page.status_code == 200
    assert "Sales data" in page.text and "Record a sale" in page.text

    response = client.post("/sales", data=sale_form(mark_day_complete="true"), follow_redirects=False)
    assert response.status_code == 303
    with session_factory() as db:
        product = db.get(Product, 1)
        sale = db.scalar(select(SalesRecord).where(SalesRecord.product_id == 1))
        movements = db.scalars(select(InventoryMovement).where(InventoryMovement.product_id == 1)).all()
        assert product.stock_quantity == 9
        assert sale.quantity_sold == 3
        assert sale.unit_selling_price == Decimal("10.50")
        sale_movement = next(m for m in movements if m.quantity == -3)
        assert sale_movement.sales_record_id == sale.id
        assert sale_movement.sales_record.id == sale.id


@pytest.mark.parametrize("quantity", ["0", "-1", "not-a-number"])
def test_invalid_sale_quantity_shows_error_without_changing_stock(sales_env, quantity):
    client, session_factory = sales_env
    add_product(client)
    response = client.post("/sales", data=sale_form(quantity_sold=quantity))
    assert response.status_code == 200
    assert "greater than 0" in response.text or "valid integer" in response.text
    with session_factory() as db:
        assert db.get(Product, 1).stock_quantity == 12
        assert db.scalar(select(func.count(SalesRecord.id))) == 0


def test_insufficient_stock_is_rejected_without_sale_or_movement(sales_env):
    client, session_factory = sales_env
    add_product(client, quantity="2")
    response = client.post("/sales", data=sale_form(quantity_sold="3"))
    assert response.status_code == 200
    assert "There is not enough stock to record this sale." in response.text
    with session_factory() as db:
        assert db.get(Product, 1).stock_quantity == 2
        assert db.scalar(select(func.count(SalesRecord.id))) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1  # opening stock only


def test_sale_transaction_rolls_back_stock_sale_and_movement_on_commit_failure(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with session_factory() as db:
        db.add(Product(sku="ROLLBACK", name="Rollback Product", category="Test", unit_price=Decimal("8.00"),
                       stock_quantity=7, minimum_stock=1))
        db.commit()
    db = session_factory()

    def fail_commit():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        record_sale(db, SaleInput(product_id=1, quantity_sold=2, sale_date=date(2026, 10, 15), mark_day_complete=True, idempotency_key=uuid4()))
    db.close()
    with session_factory() as check:
        assert check.get(Product, 1).stock_quantity == 7
        assert check.scalar(select(func.count(SalesRecord.id))) == 0
        assert check.scalar(select(func.count(InventoryMovement.id))) == 0
        assert check.scalar(select(func.count(SalesTrackingDay.id))) == 0
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_daily_summary_aggregates_and_keeps_untracked_days_unknown(sales_env):
    _, session_factory = sales_env
    first = date(2026, 10, 12)
    with session_factory() as db:
        product = Product(sku="DEMAND-1", name="Demand Product", category="Test", unit_price=Decimal("5.00"),
                          stock_quantity=100, minimum_stock=0)
        db.add(product)
        db.commit()
        product_id = product.id
        record_sale(db, SaleInput(product_id=product_id, quantity_sold=2, sale_date=first, idempotency_key=uuid4()))
        record_sale(db, SaleInput(product_id=product_id, quantity_sold=3, sale_date=first, mark_day_complete=True, idempotency_key=uuid4()))
        mark_sales_day_complete(db, product_id, first + timedelta(days=1))  # tracked day with zero sales
        record_sale(db, SaleInput(product_id=product_id, quantity_sold=4, sale_date=first + timedelta(days=2), idempotency_key=uuid4()))

        points = daily_demand_summary(db, product_id, first, first + timedelta(days=3))
    assert [point.recorded_quantity for point in points] == [5, 0, 4, 0]
    assert [point.is_tracked for point in points] == [True, True, False, False]
    assert [point.demand_quantity for point in points] == [5, 0, None, None]


def test_sales_and_coverage_persist_in_separate_sessions(sales_env):
    client, session_factory = sales_env
    add_product(client)
    client.post("/sales", data=sale_form(mark_day_complete="true"), follow_redirects=False)
    with session_factory() as db:
        assert db.scalar(select(func.count(SalesRecord.id))) == 1
        assert db.scalar(select(func.count(SalesRecord.id)).where(SalesRecord.quantity_sold == 3)) == 1
        summary = daily_demand_summary(db, 1, date(2026, 10, 15), date(2026, 10, 15))
        assert summary[0].demand_quantity == 3
        assert summary[0].is_tracked is True


def test_user_can_mark_a_verified_zero_sales_day_complete(sales_env):
    client, session_factory = sales_env
    add_product(client)
    response = client.post("/sales/coverage", data={"product_id": "1", "sales_date": "2026-10-14"},
                           follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/sales", params={"product_id": 1, "start_date": "2026-10-14", "end_date": "2026-10-14"})
    assert "Complete" in page.text and "OBSERVED DEMAND</th>" in page.text
    assert "<td>0</td>" in page.text
    with session_factory() as db:
        assert db.scalar(select(func.count(SalesTrackingDay.id))) == 1
        assert db.scalar(select(func.count(SalesRecord.id))) == 0


def test_repeated_form_submission_creates_one_sale_and_returns_clear_message(sales_env):
    client, session_factory = sales_env
    add_product(client)
    values = sale_form()
    first = client.post("/sales", data=values, follow_redirects=False)
    second = client.post("/sales", data=values, follow_redirects=False)
    assert first.status_code == 303 and second.status_code == 200
    assert "This sale form was already submitted" in second.text
    with session_factory() as db:
        assert db.get(Product, 1).stock_quantity == 9
        assert db.scalar(select(func.count(SalesRecord.id))) == 1
        assert db.scalar(select(func.count(InventoryMovement.id)).where(InventoryMovement.sales_record_id.is_not(None))) == 1


def test_identical_separate_sales_with_distinct_tokens_are_allowed(sales_env):
    client, session_factory = sales_env
    add_product(client)
    first = sale_form()
    second = {**first, "idempotency_key": str(uuid4())}
    assert client.post("/sales", data=first, follow_redirects=False).status_code == 303
    assert client.post("/sales", data=second, follow_redirects=False).status_code == 303
    with session_factory() as db:
        assert db.get(Product, 1).stock_quantity == 6
        assert db.scalar(select(func.count(SalesRecord.id))) == 2


def test_foreign_keys_enabled_and_orphan_sales_rejected(sales_env):
    _, session_factory = sales_env
    with session_factory() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        with pytest.raises(Exception):
            db.execute(text("INSERT INTO sales_records (product_id, sale_date, quantity_sold, unit_selling_price) VALUES (999, '2026-10-15', 1, 1.00)"))
            db.commit()
        db.rollback()


def test_excessive_daily_range_is_rejected_before_query():
    class NoQuery:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("range must be checked before database queries")

    with pytest.raises(DemandRangeError, match="3660 days or fewer"):
        daily_demand_summary(NoQuery(), 1, date(2000, 1, 1), date(2026, 1, 1))


def test_sales_page_shows_excessive_range_error(sales_env):
    client, _ = sales_env
    add_product(client)
    page = client.get("/sales", params={"product_id": 1, "start_date": "2000-01-01", "end_date": "2026-01-01"})
    assert page.status_code == 200
    assert "Choose a date range of 3660 days or fewer." in page.text


def test_competing_same_token_sales_cannot_deduct_stock_twice(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with session_factory() as db:
        db.add(Product(sku="RACE", name="Race item", category="Test", unit_price=Decimal("2.00"),
                       stock_quantity=10, minimum_stock=0))
        db.commit()
    token = uuid4()
    barrier = Barrier(2)

    def submit():
        with session_factory() as db:
            barrier.wait()
            try:
                record_sale(db, SaleInput(product_id=1, quantity_sold=3, sale_date=date(2026, 10, 15),
                                          idempotency_key=token))
                return "created"
            except Exception as exc:
                return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _index: submit(), range(2)))
    with session_factory() as db:
        assert sorted(outcomes) == ["DuplicateSaleError", "created"]
        assert db.get(Product, 1).stock_quantity == 7
        assert db.scalar(select(func.count(SalesRecord.id))) == 1
        assert db.scalar(select(func.count(InventoryMovement.id)).where(InventoryMovement.sales_record_id.is_not(None))) == 1
    Base.metadata.drop_all(engine)
    engine.dispose()
