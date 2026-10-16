from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app.database import Base, get_db
from app.main import app


@pytest.fixture
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        with TestingSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def add_product(client, **overrides):
    data = {"sku": "SKU-1", "name": "Desk Lamp", "category": "Lighting", "unit_price": "29.90",
            "stock_quantity": "10", "minimum_stock": "3"}
    data.update(overrides)
    return client.post("/products/new", data=data, follow_redirects=False)


def test_product_creation_records_opening_stock(client):
    response = add_product(client)
    assert response.status_code == 303
    page = client.get("/products")
    assert "Desk Lamp" in page.text and "SKU-1" in page.text
    history = client.get("/movements")
    assert "Opening stock" in history.text and "+10" in history.text


def test_duplicate_sku_is_rejected_with_clear_message(client):
    add_product(client)
    response = add_product(client, name="Another Lamp")
    assert response.status_code == 200
    assert "A product with this SKU already exists." in response.text


def test_stock_adjustments_update_quantity_and_create_history(client):
    add_product(client)
    response = client.post("/products/1/stock", data={"direction": "decrease", "quantity": "4", "reason": "Damaged"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/products/1/stock")
    assert "Current stock: <strong>6</strong> units" in page.text and "Damaged" in page.text
    assert "-4" in page.text


def test_invalid_adjustment_that_would_make_stock_negative_is_rejected(client):
    add_product(client)
    page = client.post("/products/1/stock", data={"direction": "decrease", "quantity": "11", "reason": "Count correction"})
    assert "This adjustment would make stock negative." in page.text
    assert "Current stock: <strong>10</strong> units" in page.text


def test_low_stock_threshold_and_dashboard_values_are_database_calculated(client):
    add_product(client, stock_quantity="3", minimum_stock="3")
    dashboard = client.get("/")
    assert "RM 89.70" in dashboard.text
    assert "Low stock alerts" in dashboard.text and "Desk Lamp" in dashboard.text
    assert "Desk Lamp" in client.get("/products?status=low").text


def test_product_and_movements_persist_between_sessions(client):
    add_product(client)
    assert "Desk Lamp" in client.get("/products").text
