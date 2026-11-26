"""No Gemini traffic and no real retry sleeps; exercise the provider boundary."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from google.genai import errors

from app import ai_provider as provider
from app import config
from app.ai_service import ask_inventory_assistant


def api_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    cls = errors.ClientError if status < 500 else errors.ServerError
    return cls(status, {"error": {"message": "SECRET_PROVIDER_PAYLOAD", "status": "PRIVATE_STATUS"}},
               response=httpx.Response(status, headers=headers))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET_TEST_KEY")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_SECONDS", "60")
    monkeypatch.setenv("GEMINI_MAX_ATTEMPTS", "2")
    clock = SimpleNamespace(now=0.0, sleeps=[])
    monkeypatch.setattr(provider, "monotonic", lambda: clock.now)
    monkeypatch.setattr(provider.random, "uniform", lambda low, high: high)

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(provider.asyncio, "sleep", sleep)
    generate = AsyncMock(return_value=SimpleNamespace(text=" Inventory answer. "))
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=AsyncMock()),
        close=Mock(),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(provider.genai, "Client", factory)
    return SimpleNamespace(clock=clock, generate=generate, client=client, factory=factory)


def test_success_first_request_and_sdk_timeout_units(env, caplog):
    caplog.set_level(logging.INFO, logger=provider.__name__)
    assert ask_inventory_assistant("What should I restock?", {"private": "SECRET_INVENTORY"}) == "Inventory answer."
    assert env.generate.await_count == 1
    assert env.clock.sleeps == []
    options = env.factory.call_args.kwargs["http_options"]
    assert options.timeout == 30000
    assert options.retry_options.attempts == 1
    request = env.generate.call_args.kwargs
    assert request["model"] == "gemini-3.1-flash-lite"
    assert request["config"].http_options.timeout == 30000
    assert request["config"].http_options.retry_options.attempts == 1
    assert caplog.records[-1].error_category == "success"
    assert caplog.records[-1].provider_status == 200
    env.client.aio.aclose.assert_awaited_once()
    env.client.close.assert_called_once()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_failure_then_success(env, status):
    env.generate.side_effect = [api_error(status), SimpleNamespace(text="Recovered")]
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert env.generate.await_count == 2
    assert env.clock.sleeps == [0.5]
    # The second timeout is capped by the remaining overall budget when necessary.
    assert all(call.kwargs["config"].http_options.retry_options.attempts == 1
               for call in env.generate.call_args_list)


@pytest.mark.parametrize("status,category,http_status", [
    (500, "service", 503), (502, "service", 503), (503, "service", 503),
    (504, "timeout", 504),
])
def test_exhausted_retries(env, status, category, http_status):
    env.generate.side_effect = api_error(status)
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == category
    assert caught.value.http_status == http_status
    assert env.generate.await_count == 2
    assert env.clock.sleeps == [0.5]


@pytest.mark.parametrize("status,category", [
    (400, "invalid_request"), (401, "authentication"), (403, "authentication"),
    (404, "invalid_request"), (422, "invalid_request"), (501, "service"),
])
def test_non_retryable_statuses(env, status, category):
    env.generate.side_effect = api_error(status)
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == category
    assert env.generate.await_count == 1
    assert env.clock.sleeps == []


@pytest.mark.parametrize("header", [None, "invalid", "-1", "nan", "inf", "999999999"])
def test_rate_limit_without_usable_retry_window(env, header):
    env.generate.side_effect = api_error(429, header)
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == "rate_limit"
    assert caught.value.http_status == 429
    assert env.generate.await_count == 1
    assert env.clock.sleeps == []


def test_rate_limit_respects_retry_after_seconds(env):
    env.generate.side_effect = [api_error(429, "3"), SimpleNamespace(text="Recovered")]
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert env.clock.sleeps == [3]


def test_rate_limit_http_date(env):
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    env.generate.side_effect = [api_error(429, future), SimpleNamespace(text="Recovered")]
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert 3 <= env.clock.sleeps[0] <= 5


def test_exhausted_rate_limit_retries(env):
    env.generate.side_effect = api_error(429, "0")
    with pytest.raises(provider.AIServiceError, match="rate-limited"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 2


@pytest.mark.parametrize("failure,category", [
    (ValueError("SECRET_INITIALIZATION"), "unexpected"),
    (api_error(401), "authentication"),
])
def test_client_initialization_failure(env, failure, category, caplog):
    env.factory.side_effect = failure
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == category
    assert env.factory.call_count == 1
    env.generate.assert_not_called()
    assert caplog.records[-1].stage == "initialization"
    assert caplog.records[-1].attempt == 0


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("SECRET_TIMEOUT"), TimeoutError("SECRET_TIMEOUT")])
def test_transport_timeouts(env, exc):
    env.generate.side_effect = exc
    with pytest.raises(provider.AIServiceError, match="timed out"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 2


def test_unexpected_exception_is_generic_and_not_retried(env):
    env.generate.side_effect = RuntimeError("SECRET_UNEXPECTED")
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.public_message == "The Gemini service is currently unavailable."
    assert env.generate.await_count == 1


def test_empty_response_does_not_retry(env):
    env.generate.return_value = SimpleNamespace(text="   ")
    with pytest.raises(provider.AIServiceError):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1


def test_single_attempt_configuration(env, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_ATTEMPTS", "1")
    env.generate.side_effect = api_error(503)
    with pytest.raises(provider.AIServiceError):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1
    assert env.clock.sleeps == []


def test_remaining_budget_caps_second_request_timeout(env, monkeypatch):
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_SECONDS", "31")

    async def generate(**kwargs):
        if env.generate.await_count == 1:
            env.clock.now += 29
            raise api_error(503)
        return SimpleNamespace(text="Recovered")

    env.generate.side_effect = generate
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert env.generate.call_args_list[1].kwargs["config"].http_options.timeout == 1500


def test_budget_exhaustion_stops_retry(env, monkeypatch):
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_SECONDS", "1")

    async def fail(**kwargs):
        env.clock.now = 2
        raise api_error(503)

    env.generate.side_effect = fail
    with pytest.raises(provider.AIServiceError, match="timed out"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1
    assert env.clock.sleeps == []


def test_wall_clock_budget_cancels_stalled_provider(env, monkeypatch):
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_SECONDS", "0.1")

    async def stall(**kwargs):
        await asyncio.Event().wait()

    env.generate.side_effect = stall
    with pytest.raises(provider.AIServiceError, match="timed out"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1
    env.client.aio.aclose.assert_awaited_once()


def test_per_attempt_deadline_retries_within_total_budget(env, monkeypatch):
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "0.1")

    async def stall(**kwargs):
        await asyncio.Event().wait()

    env.generate.side_effect = stall
    with pytest.raises(provider.AIServiceError, match="timed out"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 2
    assert env.clock.sleeps == [0.5]


def test_answer_arriving_after_budget_is_discarded(env, monkeypatch):
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_SECONDS", "1")

    async def late(**kwargs):
        env.clock.now = 2
        return SimpleNamespace(text="Late answer")

    env.generate.side_effect = late
    with pytest.raises(provider.AIServiceError, match="timed out"):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1


def test_sanitized_logging(env, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=provider.__name__)
    monkeypatch.setenv("GEMINI_MODEL", "SECRET_TEST_KEY\nSECRET_MODEL")
    env.generate.side_effect = api_error(504)
    with pytest.raises(provider.AIServiceError):
        ask_inventory_assistant("SECRET_QUESTION", {"inventory": "SECRET_SNAPSHOT"})
    assert "SECRET_" not in caplog.text
    for record in caplog.records:
        assert record.model == "[redacted]"
        assert record.provider_status == 504
        assert record.error_category == "timeout"
        assert record.attempt in (1, 2)
        assert record.elapsed_seconds >= 0
        assert record.exc_info is None


def test_cleanup_failure_does_not_replace_answer(env):
    env.client.aio.aclose.side_effect = RuntimeError("SECRET_CLEANUP")
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Inventory answer."
    env.client.close.assert_called_once()


def test_network_failure_is_retried(env):
    env.generate.side_effect = [httpx.ConnectError("SECRET_NETWORK"), SimpleNamespace(text="Recovered")]
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert env.generate.await_count == 2


def test_invalid_local_protocol_is_not_retried(env):
    env.generate.side_effect = httpx.UnsupportedProtocol("SECRET_PROTOCOL")
    with pytest.raises(provider.AIServiceError):
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert env.generate.await_count == 1


def test_expired_retry_after_date_uses_backoff(env):
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=10), usegmt=True)
    env.generate.side_effect = [api_error(429, past), SimpleNamespace(text="Recovered")]
    assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Recovered"
    assert env.clock.sleeps == [0.5]


@pytest.mark.parametrize("name,value", [
    ("GEMINI_REQUEST_TIMEOUT_SECONDS", "0"),
    ("GEMINI_REQUEST_TIMEOUT_SECONDS", "nan"),
    ("GEMINI_REQUEST_TIMEOUT_SECONDS", "inf"),
    ("GEMINI_REQUEST_TIMEOUT_SECONDS", "301"),
    ("GEMINI_REQUEST_TIMEOUT_SECONDS", "SECRET_INVALID"),
    ("GEMINI_TOTAL_BUDGET_SECONDS", "-1"),
    ("GEMINI_TOTAL_BUDGET_SECONDS", "601"),
    ("GEMINI_MAX_ATTEMPTS", "0"),
    ("GEMINI_MAX_ATTEMPTS", "3"),
    ("GEMINI_MAX_ATTEMPTS", "1.5"),
])
def test_invalid_configuration(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as caught:
        config.gemini_request_settings()
    assert "SECRET_INVALID" not in str(caught.value)
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == "configuration"
    env.factory.assert_not_called()


def test_missing_key(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(provider.AIServiceError) as caught:
        provider.generate_inventory_answer("PRIVATE_PROMPT")
    assert caught.value.category == "configuration"
    env.factory.assert_not_called()


def test_installed_sdk_with_offline_http_transport(env, monkeypatch):
    """Verify the actual SDK request/config contract without any network transport."""
    from google.genai.client import Client
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(504, json={"error": {"code": 504, "status": "DEADLINE_EXCEEDED"}})
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Offline SDK answer"}]}}],
        })

    async_transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sync_transport = httpx.Client(transport=httpx.MockTransport(respond))

    def client(**kwargs):
        kwargs["http_options"].httpx_async_client = async_transport
        kwargs["http_options"].httpx_client = sync_transport
        return Client(**kwargs)

    monkeypatch.setattr(provider.genai, "Client", client)
    try:
        assert provider.generate_inventory_answer("PRIVATE_PROMPT") == "Offline SDK answer"
        assert len(calls) == 2
        assert calls[0].extensions["timeout"]["read"] == 30
    finally:
        asyncio.run(async_transport.aclose())
        sync_transport.close()
