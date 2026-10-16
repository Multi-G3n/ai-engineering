import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Product, User
from app.security import hash_password, verify_password
from conftest import TEST_PASSWORD, TEST_USERNAME, add_test_admin, reset_login_limiter


@pytest.fixture
def security_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'security-test.db'}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    add_test_admin(session_factory)
    reset_login_limiter()
    with TestClient(app, base_url="https://testserver") as client:
        yield client, session_factory
    app.dependency_overrides.clear()
    reset_login_limiter()
    Base.metadata.drop_all(engine)
    engine.dispose()


def csrf_from(response):
    import re
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def valid_login(client, username=TEST_USERNAME, password=TEST_PASSWORD):
    token = csrf_from(client.get("/login"))
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                       follow_redirects=False)


def test_unauthenticated_users_cannot_access_pages_or_submit_mutations(security_env):
    client, session_factory = security_env
    for path in ("/", "/products", "/products/new", "/movements", "/sales"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    posts = (
        ("/products/new", {"sku": "SEC-1", "name": "Item", "category": "Test", "unit_price": "1",
                            "stock_quantity": "2", "minimum_stock": "1"}),
        ("/products/1/edit", {}), ("/products/1/stock", {}), ("/sales", {}), ("/sales/coverage", {}),
        ("/logout", {}),
    )
    for path, data in posts:
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303, (path, response.text)
    with session_factory() as db:
        assert db.scalar(select(func.count(Product.id))) == 0


def test_valid_login_sets_secure_session_cookie_and_grants_access(security_env):
    client, _ = security_env
    response = valid_login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("username,password", [("missing-user", TEST_PASSWORD), (TEST_USERNAME, "wrong-password")])
def test_invalid_credentials_have_generic_response(security_env, username, password):
    client, _ = security_env
    response = valid_login(client, username, password)
    assert response.status_code == 401
    assert "Invalid username or password." in response.text
    assert "missing-user" not in response.text


def test_logout_clears_session(security_env):
    client, _ = security_env
    assert valid_login(client).status_code == 303
    token = csrf_from(client.get("/products/new"))
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/products", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_missing_or_invalid_csrf_rejects_mutations(security_env, token):
    client, session_factory = security_env
    valid_login(client)
    data = {"sku": "SEC-1", "name": "Item", "category": "Test", "unit_price": "1",
            "stock_quantity": "2", "minimum_stock": "1"}
    if token is not None:
        data["csrf_token"] = token
    response = client.post("/products/new", data=data)
    assert response.status_code == 403
    with session_factory() as db:
        assert db.scalar(select(func.count(Product.id))) == 0


def test_valid_form_csrf_allows_product_creation_and_password_is_hashed(security_env):
    client, session_factory = security_env
    valid_login(client)
    token = csrf_from(client.get("/products/new"))
    response = client.post("/products/new", data={
        "sku": "SEC-1", "name": "Secure Item", "category": "Test", "unit_price": "1",
        "stock_quantity": "2", "minimum_stock": "1", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    with session_factory() as db:
        user = db.scalar(select(User).where(User.username == TEST_USERNAME))
        assert user.password_hash != TEST_PASSWORD
        assert verify_password(TEST_PASSWORD, user.password_hash)
        assert db.scalar(select(func.count(Product.id))) == 1


def test_login_throttle_blocks_repeated_failures(security_env):
    client, _ = security_env
    for _ in range(5):
        assert valid_login(client, password="wrong-password").status_code == 401
    assert valid_login(client).status_code == 401
