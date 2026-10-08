"""HTTP throttling and server errors from ``responses.create`` stay transient.

Azure error bodies carry their own codes (``"429"``, ``"InternalServerError"``,
``"ServiceUnavailable"``). If those won over the HTTP status, a throttled plan run
would raise instead of returning ``None`` and lose the functional-plan retry that the
classic runs path had.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from agent import foundry_agent as f


def _status_error(status: int, code: str | None) -> openai.APIStatusError:
    body = {"error": {"code": code, "message": "boom"}} if code is not None else None
    request = httpx.Request("POST", "https://example.invalid/openai/v1/responses")
    response = httpx.Response(status, request=request, json=body)
    client = openai.OpenAI(api_key="test", base_url="https://example.invalid")
    return client._make_status_error_from_response(response)


@pytest.mark.parametrize(
    ("status", "body_code", "expected"),
    [
        (429, "429", "rate_limit_exceeded"),
        (429, None, "rate_limit_exceeded"),
        (500, "InternalServerError", "server_error"),
        (503, "ServiceUnavailable", "server_error"),
    ],
)
def test_throttling_and_server_errors_are_transient(status, body_code, expected) -> None:
    code = f._status_error_code(_status_error(status, body_code))
    assert code == expected
    assert code in f.TRANSIENT_FAILURE_CODES


def test_client_errors_keep_their_body_code() -> None:
    assert f._status_error_code(_status_error(400, "invalid_prompt")) == "invalid_prompt"
    assert f._status_error_code(_status_error(404, None)) == "http_404"
