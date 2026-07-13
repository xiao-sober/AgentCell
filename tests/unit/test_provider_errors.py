"""Provider exception classification and retry policy tests."""

from __future__ import annotations

import httpx
import pytest
from pydantic_ai import ModelHTTPError

from agentcell.errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderContextLimitError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
)
from agentcell.providers import classify_provider_error, should_retry_provider_error


@pytest.mark.parametrize(
    ("status_code", "body", "expected_type", "retryable"),
    [
        (401, {}, ProviderAuthenticationError, False),
        (403, {}, ProviderPermissionError, False),
        (400, {"code": "context_length_exceeded"}, ProviderContextLimitError, False),
        (422, {"message": "maximum context length exceeded"}, ProviderContextLimitError, False),
        (400, {"code": "invalid_parameter"}, ProviderProtocolError, False),
        (429, {}, ProviderRateLimitError, True),
        (500, {}, ProviderUpstreamError, False),
        (502, {}, ProviderUpstreamError, True),
        (503, {}, ProviderUpstreamError, True),
        (504, {}, ProviderTimeoutError, True),
    ],
)
def test_http_status_classification(
    status_code: int,
    body: object,
    expected_type: type[Exception],
    retryable: bool,
) -> None:
    classified = classify_provider_error(
        "deepseek",
        "deepseek-v4-pro",
        ModelHTTPError(status_code, "deepseek-v4-pro", body),
    )

    assert isinstance(classified, expected_type)
    assert classified.retryable is retryable


def test_transport_errors_are_classified() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/chat")

    timeout = classify_provider_error(
        "bailian",
        "qwen3.7-plus",
        httpx.ReadTimeout("timed out", request=request),
    )
    connection = classify_provider_error(
        "bailian",
        "qwen3.7-plus",
        httpx.ConnectError("unreachable", request=request),
    )

    assert isinstance(timeout, ProviderTimeoutError)
    assert isinstance(connection, ProviderConnectionError)


def test_classified_error_does_not_retain_body_or_credential() -> None:
    api_key = "sk-do-not-log-this"
    error = ModelHTTPError(
        401,
        "qwen3.7-plus",
        {"Authorization": f"Bearer {api_key}", "request": "full prompt"},
    )

    classified = classify_provider_error("bailian", "qwen3.7-plus", error)

    assert api_key not in str(classified)
    assert "full prompt" not in str(classified)
    assert not hasattr(classified, "body")


def test_retry_policy_obeys_classification_and_attempt_ceiling() -> None:
    retryable = ProviderRateLimitError("deepseek", "deepseek-v4-pro", "limited")
    terminal = ProviderAuthenticationError("deepseek", "deepseek-v4-pro", "rejected")

    assert should_retry_provider_error(retryable, attempt=0, max_retries=2)
    assert not should_retry_provider_error(retryable, attempt=2, max_retries=2)
    assert not should_retry_provider_error(terminal, attempt=0, max_retries=2)
