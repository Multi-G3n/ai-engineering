
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai_service import AIServiceError
from app.database import Base, get_db
from app.main import app
from app.models import Product
from conftest import (
    add_test_admin,
    login_test_client,
    reset_login_limiter,
)


@pytest.fixture
def ai_env(tmp_path):
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
                unit_price=1000,
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
            yield client
    finally:
        app.dependency_overrides.clear()
        reset_login_limiter()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_ai_chat_requires_login(ai_env):
    client = ai_env

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


@pytest.mark.parametrize(
    "token",
    [None, "invalid-token"],
)
def test_ai_chat_rejects_invalid_csrf(ai_env, token):
    client = ai_env

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


@pytest.mark.parametrize(
    "question",
    ["", "ab", "   ", "x" * 501],
)
def test_ai_chat_rejects_invalid_questions(
    ai_env,
    question,
):
    client = ai_env
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


def test_ai_chat_returns_inventory_analysis(ai_env):
    client = ai_env
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


def test_ai_chat_handles_gemini_failure(ai_env):
    client = ai_env
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
