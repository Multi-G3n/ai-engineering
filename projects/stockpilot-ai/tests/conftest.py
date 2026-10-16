import os
import re
import secrets

from app.models import User
from app.security import hash_password, login_rate_limiter

# Test-only settings are generated per pytest process and never serve as app defaults.
os.environ["STOCKPILOT_SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["STOCKPILOT_COOKIE_SECURE"] = "true"

TEST_USERNAME = "test-admin"
TEST_PASSWORD = "test-only-password-2026"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


def add_test_admin(session_factory):
    with session_factory() as db:
        db.add(User(username=TEST_USERNAME, password_hash=hash_password(TEST_PASSWORD), is_admin=True))
        db.commit()


def login_test_client(client):
    login_page = client.get("/login")
    token = CSRF_FIELD.search(login_page.text).group(1)
    response = client.post("/login", data={
        "username": TEST_USERNAME,
        "password": TEST_PASSWORD,
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    business_page = client.get("/products/new")
    token = CSRF_FIELD.search(business_page.text).group(1)
    client.headers["X-CSRF-Token"] = token
    return token


def reset_login_limiter():
    login_rate_limiter.clear()
