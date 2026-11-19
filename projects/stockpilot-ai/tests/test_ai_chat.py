
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai_service import AIServiceError
from app.database import Base, get_db
from app.main import app
from app.models import Product, SalesRecord, SalesTrackingDay
from conftest import (
    add_test_admin,
    login_test_client,
    reset_login_limiter,
)


# ============================================================
# TEST DATABASE AND AUTHENTICATION
# ============================================================

@pytest.fixture
def ai_env(tmp_path):
    """
    Create an isolated SQLite database for AI endpoint tests.

    No tests modify the real StockPilot database.
    """

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ai-test.db'}",
        connect_args={"check_same_thread": False},
    )

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    Base.metadata.create_all(engine)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db

    add_test_admin(session_factory)
    reset_login_limiter()

    with session_factory() as db:
        db.add(
            Product(
                sku="AI-001",
                name="Test Ice Machine",
                category="Equipment",
                unit_price=Decimal("1000.00"),
                stock_quantity=2,
                minimum_stock=5,
            )
        )
        db.commit()

    try:
        with TestClient(
            app,
            base_url="https://testserver",
        ) as client:
            yield client, session_factory

    finally:
        app.dependency_overrides.clear()
        reset_login_limiter()
        Base.metadata.drop_all(engine)
        engine.dispose()


# ============================================================
# AUTHENTICATION TESTS
# ============================================================

def test_ai_chat_requires_login(ai_env):
    client, _ = ai_env

    with patch(
        "app.main.ask_inventory_assistant"
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={
                "question": "Which products are low on stock?"
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    mock_ai.assert_not_called()


# ============================================================
# CSRF SECURITY TESTS
# ============================================================

@pytest.mark.parametrize(
    "token",
    [None, "invalid-token"],
)
def test_ai_chat_rejects_invalid_csrf(ai_env, token):
    client, _ = ai_env

    login_test_client(client)

    client.headers.pop("X-CSRF-Token", None)

    if token is not None:
        client.headers["X-CSRF-Token"] = token

    with patch(
        "app.main.ask_inventory_assistant"
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={
                "question": "Which products are low on stock?"
            },
        )

    assert response.status_code == 403

    mock_ai.assert_not_called()


# ============================================================
# INPUT VALIDATION TESTS
# ============================================================

@pytest.mark.parametrize(
    "question",
    [
        "",
        "ab",
        "   ",
        "x" * 501,
    ],
)
def test_ai_chat_rejects_invalid_questions(
    ai_env,
    question,
):
    client, _ = ai_env

    login_test_client(client)

    with patch(
        "app.main.ask_inventory_assistant"
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={"question": question},
        )

    assert response.status_code == 422

    mock_ai.assert_not_called()


# ============================================================
# EXISTING AI INVENTORY ANALYSIS
# ============================================================

def test_ai_chat_returns_inventory_analysis(ai_env):
    client, _ = ai_env

    login_test_client(client)

    with patch(
        "app.main.ask_inventory_assistant",
        return_value=(
            "Test Ice Machine is below its minimum stock."
        ),
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={
                "question": "Which products are low on stock?"
            },
        )

    assert response.status_code == 200, response.json()

    data = response.json()

    assert "Test Ice Machine" in data["answer"]
    assert data["source"] == "StockPilot inventory database"
    assert data["products_analyzed"] == 1

    mock_ai.assert_called_once()

    arguments = mock_ai.call_args.kwargs
    snapshot = arguments["snapshot"]

    assert snapshot["product_count"] == 1
    assert snapshot["products"][0]["sku"] == "AI-001"
    assert snapshot["products"][0]["stock_quantity"] == 2
    assert snapshot["products"][0]["minimum_stock"] == 5


# ============================================================
# GEMINI ERROR HANDLING
# ============================================================

def test_ai_chat_handles_gemini_failure(ai_env):
    client, _ = ai_env

    login_test_client(client)

    with patch(
        "app.main.ask_inventory_assistant",
        side_effect=AIServiceError(
            "The Gemini service is currently unavailable."
        ),
    ):

        response = client.post(
            "/ai/chat",
            json={
                "question": "What should I restock?"
            },
        )

    assert response.status_code == 503, response.json()

    assert "unavailable" in response.json()["detail"]


# ============================================================
# NEW TEST 1 — RESTOCK FIELDS WITHOUT SALES HISTORY
# ============================================================

def test_ai_snapshot_includes_restock_fields(ai_env):
    """
    Verify the AI receives restock fields even when
    insufficient historical sales data is available.
    """

    client, _ = ai_env

    login_test_client(client)

    with patch(
        "app.main.ask_inventory_assistant",
        return_value="Inventory analysis completed.",
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={
                "question": "What should I restock first?"
            },
        )

    assert response.status_code == 200, response.json()

    snapshot = mock_ai.call_args.kwargs["snapshot"]

    assert snapshot["product_count"] == 1

    product = snapshot["products"][0]

    # Observed inventory
    assert product["sku"] == "AI-001"
    assert product["name"] == "Test Ice Machine"
    assert product["stock_quantity"] == 2
    assert product["minimum_stock"] == 5
    assert product["is_low_stock"] is True

    # No complete sales history
    assert product["forecast_available"] is False
    assert product["tracked_days"] == 0

    # No invented demand forecasts
    assert product["daily_demand_estimate"] is None
    assert product["demand_7_days"] is None
    assert product["demand_30_days"] is None
    assert product["days_of_stock"] is None

    # No invented reorder quantity
    assert product["reorder_quantity"] is None
    assert product["stockout_risk"] == "Insufficient data"

    # Snapshot summary
    assert snapshot["low_stock_count"] == 1
    assert snapshot["insufficient_data_count"] == 1
    assert snapshot["high_risk_count"] == 0
    assert snapshot["recommended_reorder_units"] == 0


# ============================================================
# NEW TEST 2 — RESTOCK CALCULATIONS WITH SALES HISTORY
# ============================================================

def test_ai_snapshot_uses_calculated_forecast(ai_env):
    """
    Verify the AI receives Python-calculated forecasts
    after seven fully tracked sales days.

    Gemini is mocked, so no external API call is made.
    """

    client, session_factory = ai_env

    login_test_client(client)

    today = date.today()

    # Add seven days of confirmed sales to the test database.
    with session_factory() as db:

        product = db.query(Product).filter_by(
            sku="AI-001"
        ).one()

        for days_ago in range(1, 8):

            sale_date = today - timedelta(
                days=days_ago
            )

            db.add(
                SalesRecord(
                    product_id=product.id,
                    sale_date=sale_date,
                    quantity_sold=2,
                    unit_selling_price=Decimal("1000.00"),
                )
            )

            db.add(
                SalesTrackingDay(
                    product_id=product.id,
                    sales_date=sale_date,
                )
            )

        db.commit()

    # Call the real FastAPI endpoint.
    # Only Gemini's response generation is mocked.
    with patch(
        "app.main.ask_inventory_assistant",
        return_value="Restock recommendation available.",
    ) as mock_ai:

        response = client.post(
            "/ai/chat",
            json={
                "question": "How many units should I reorder?"
            },
        )

    assert response.status_code == 200, response.json()

    snapshot = mock_ai.call_args.kwargs["snapshot"]

    assert snapshot["product_count"] == 1

    product = snapshot["products"][0]

    # Forecast availability
    assert product["forecast_available"] is True
    assert product["tracked_days"] == 7

    # Daily average: 2 units
    assert product["daily_demand_estimate"] == 2.0

    # Demand estimates
    assert product["weekly_demand_estimate"] == 14.0
    assert product["demand_7_days"] == 14
    assert product["demand_30_days"] == 60

    # Stock coverage: 2 units / 2 per day = 1 day
    assert product["days_of_stock"] == 1.0

    # Risk assessment
    assert product["stockout_risk"] == "High risk"

    # Reorder target:
    # (7-day lead time + 7-day safety stock) * 2
    # = 28 target units
    #
    # Current stock = 2
    # Recommended reorder = 28 - 2 = 26
    assert product["reorder_quantity"] == 26

    # Overall inventory summary
    assert snapshot["high_risk_count"] == 1
    assert snapshot["low_stock_count"] == 1
    assert snapshot["insufficient_data_count"] == 0
    assert snapshot["recommended_reorder_units"] == 26


# ============================================================
# NEW TEST 3 — AI PAGE REQUIRES LOGIN
# ============================================================

def test_ai_page_requires_login(ai_env):
    client, _ = ai_env

    response = client.get(
        "/ai",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ============================================================
# NEW TEST 4 — AUTHENTICATED AI PAGE LOADS
# ============================================================

def test_ai_page_loads_for_authenticated_user(ai_env):
    client, _ = ai_env

    login_test_client(client)

    response = client.get("/ai")

    assert response.status_code == 200
    assert "AI Assistant" in response.text
