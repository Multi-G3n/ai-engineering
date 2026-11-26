"""Bounded, read-only Gemini requests. Never log provider payloads or prompts."""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
import re
from time import monotonic

import httpx
from google import genai
from google.genai import errors, types

from app.config import GeminiRequestSettings, gemini_api_key, gemini_model, gemini_request_settings

try:  # The installed SDK can select httpx2 when available; it is not required here.
    import httpx2
except ImportError:
    httpx2 = None

TIMEOUT_ERRORS = (TimeoutError, httpx.TimeoutException)
TRANSPORT_ERRORS = (httpx.NetworkError, httpx.RemoteProtocolError)
if httpx2 is not None:
    TIMEOUT_ERRORS += (httpx2.TimeoutException,)
    TRANSPORT_ERRORS += (httpx2.NetworkError, httpx2.RemoteProtocolError)

logger = logging.getLogger(__name__)
TRANSIENT_STATUSES = {500, 502, 503, 504}
MESSAGES = {
    "timeout": (504, "The AI request timed out. Please try again."),
    "rate_limit": (429, "The AI service is rate-limited. Please try again later."),
    "authentication": (502, "The AI service could not authenticate. Please contact the administrator."),
    "invalid_request": (502, "The AI service could not process this request. Please try a different question."),
    "configuration": (503, "The AI service is not configured correctly. Please contact the administrator."),
    "service": (503, "The Gemini service is currently unavailable."),
    "unexpected": (503, "The Gemini service is currently unavailable."),
}


class AIServiceError(Exception):
    """Only allowlisted category messages are sent to the browser."""

    def __init__(self, message: str | None = None, *, category: str = "unexpected"):
        self.category = category if category in MESSAGES else "unexpected"
        self.http_status, self.public_message = MESSAGES[self.category]
        super().__init__(message or self.public_message)


def _classify(exc: Exception) -> tuple[str, int | None, bool]:
    status = exc.code if isinstance(exc, errors.APIError) else None
    if isinstance(exc, TIMEOUT_ERRORS) or status in (408, 504):
        return "timeout", status, True
    if status == 429:
        return "rate_limit", status, False  # Retried only with a valid Retry-After.
    if status in (401, 403):
        return "authentication", status, False
    if status is not None and 400 <= status < 500:
        return "invalid_request", status, False
    if status is not None and 500 <= status < 600:
        return "service", status, status in TRANSIENT_STATUSES
    if isinstance(exc, TRANSPORT_ERRORS):
        return "service", None, True
    return "unexpected", None, False


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "headers", {}).get("retry-after")
    if not isinstance(value, str):
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            delay = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) and delay >= 0 else None


def _record(model: str, started: float, attempt: int, category: str,
            status: int | None = None, *, stage: str = "request") -> None:
    # Model is configurable: redact malformed identifiers and anything resembling a key.
    safe_model = model if re.fullmatch(r"(?:models/)?gemini-[a-z0-9.-]{1,80}", model) else "[redacted]"
    elapsed = round(monotonic() - started, 3)
    logger.log(
        logging.INFO if category == "success" else logging.WARNING,
        "Gemini provider category=%s status=%s model=%s elapsed=%s attempt=%s stage=%s",
        category, status, safe_model, elapsed, attempt, stage,
        extra={"provider_status": status, "error_category": category, "model": safe_model,
               "elapsed_seconds": elapsed,
               "attempt": attempt, "stage": stage},
    )


async def _request(prompt: str, key: str, model: str, settings: GeminiRequestSettings) -> str:
    started = monotonic()
    deadline = started + settings.total_budget_seconds
    client = None
    attempt = 0
    last_category = "timeout"
    try:
        # This bounds awaited I/O and backoff, unlike per-socket transport timeouts alone.
        async with asyncio.timeout(settings.total_budget_seconds):
            try:
                client = genai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(
                        timeout=int(settings.timeout_seconds * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                )
            except Exception as exc:
                category, status, _ = _classify(exc)
                _record(model, started, 0, category, status, stage="initialization")
                raise AIServiceError(category=category) from None

            for attempt in range(1, settings.max_attempts + 1):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                timeout = min(settings.timeout_seconds, remaining)
                try:
                    async with asyncio.timeout(timeout):
                        response = await client.aio.models.generate_content(
                            model=model, contents=prompt,
                            config=types.GenerateContentConfig(
                                temperature=0.2, max_output_tokens=1000,
                                http_options=types.HttpOptions(
                                    timeout=max(1, int(timeout * 1000)),
                                    retry_options=types.HttpRetryOptions(attempts=1),
                                ),
                            ),
                        )
                    if monotonic() >= deadline:
                        raise TimeoutError
                    if not response.text or not response.text.strip():
                        _record(model, started, attempt, "service")
                        raise AIServiceError(category="service")
                    _record(model, started, attempt, "success", 200)
                    return response.text.strip()
                except AIServiceError:
                    raise
                except Exception as exc:
                    category, status, retryable = _classify(exc)
                    last_category = category
                    _record(model, started, attempt, category, status)
                    delay = random.uniform(0.25 * 2 ** (attempt - 1), 0.5 * 2 ** (attempt - 1))
                    if category == "rate_limit":
                        retry_after = _retry_after(exc)
                        retryable = retry_after is not None
                        if retryable:
                            delay = max(delay, retry_after)
                    if not retryable or attempt == settings.max_attempts:
                        raise AIServiceError(category=category) from None
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError from None
                    if delay >= remaining:
                        # Never shorten a Retry-After to squeeze in another attempt.
                        raise AIServiceError(category=category) from None
                    await asyncio.sleep(delay)
    except TimeoutError:
        _record(model, started, attempt, "timeout", stage="budget")
        raise AIServiceError(category="timeout") from None
    finally:
        if client is not None:
            try:
                # Cleanup is bounded separately and never replaces the request result.
                async with asyncio.timeout(1):
                    await client.aio.aclose()
            except Exception:
                _record(model, started, attempt, "unexpected", stage="cleanup")
            finally:
                try:
                    client.close()
                except Exception:
                    _record(model, started, attempt, "unexpected", stage="cleanup")
    raise AIServiceError(category=last_category)


def generate_inventory_answer(prompt: str) -> str:
    """Called by the synchronous FastAPI handler in its worker thread."""
    model = gemini_model()
    started = monotonic()
    try:
        settings = gemini_request_settings()
        key = gemini_api_key()
        if not key or not key.strip():
            raise ValueError("Missing provider configuration")
    except ValueError:
        _record(model, started, 0, "configuration", stage="configuration")
        raise AIServiceError(category="configuration") from None
    return asyncio.run(_request(prompt, key, model, settings))
