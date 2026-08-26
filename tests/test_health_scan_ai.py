"""Unit tests for ``agent.health_scan.analyze_with_ai``.

This is one of the two money paths in the repo: whatever ``analyze_with_ai``
puts in ``issues_to_create`` is handed straight to ``create_github_issues``,
which POSTs real GitHub issues and assigns them to the Copilot coding agent —
a 10-30 minute paid run each. Every other test in the suite mocks this
function out, so its response parsing was previously uncovered.

The Azure OpenAI client is always mocked; these tests never touch the network.

Several tests below are *characterization* tests: they pin down behaviour that
is wrong but real, so the blast radius is visible and a future fix has a
failing test to flip. Each one says so in its docstring.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent import health_scan

PAYLOAD: dict[str, Any] = {
    "health_scores": {"era": {"R": 3, "L": 4, "M": 2, "health": 9}},
    "alerts": ["CI red on era"],
    "recommendations": ["Fix the failing build"],
    "focus_project": "era",
    "issues_to_create": [
        {"repo": "era", "title": "Fix TypeError", "body": "stack", "labels": ["bug"]}
    ],
}
CLEAN_JSON = json.dumps(PAYLOAD)


@pytest.fixture(autouse=True)
def _azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the function at a fake endpoint with a fake key.

    Setting the key keeps it on the api_key branch, so no credential chain is
    started and nothing tries to reach Azure IMDS.
    """
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "fake-key")
    monkeypatch.delenv("HEALTH_SCAN_MODEL", raising=False)


@contextmanager
def mock_openai(content: str | None) -> Iterator[MagicMock]:
    """Patch ``openai.AzureOpenAI`` so the model returns *content* verbatim."""
    client = MagicMock()
    response = MagicMock()
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response.choices = [choice]
    client.chat.completions.create.return_value = response
    with patch("openai.AzureOpenAI", return_value=client) as ctor:
        ctor.client = client
        yield ctor


def analyze(content: str | None) -> Any:
    with mock_openai(content):
        return health_scan.analyze_with_ai({"era": {}}, {"total": 5})


# ── happy paths: the reply shapes the parser is built for ──────────────────
def test_clean_json_object_is_parsed() -> None:
    assert analyze(CLEAN_JSON) == PAYLOAD


def test_json_fenced_reply_is_unwrapped() -> None:
    """```json ... ``` — the case the stripping code at health_scan.py:646 exists for."""
    assert analyze(f"```json\n{CLEAN_JSON}\n```") == PAYLOAD


def test_bare_fenced_reply_is_unwrapped() -> None:
    """A bare ``` ... ``` fence, with no language tag."""
    assert analyze(f"```\n{CLEAN_JSON}\n```") == PAYLOAD


def test_issues_to_create_survives_the_round_trip() -> None:
    """The value that becomes real, paid-for GitHub issues must arrive intact."""
    result = analyze(f"```json\n{CLEAN_JSON}\n```")

    assert result.get("issues_to_create") == [
        {"repo": "era", "title": "Fix TypeError", "body": "stack", "labels": ["bug"]}
    ]


# ── degradation: bad replies must not kill the daily scan ──────────────────
@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("refusal prose", "I'm sorry, I can't help with that."),
        ("empty string", ""),
        ("whitespace only", "   \n  "),
        ("null content", None),
        ("truncated json", '{"alerts": ['),
        ("prose around fence", f"Here you go:\n```json\n{CLEAN_JSON}\n```\nHTH."),
    ],
)
def test_unparseable_reply_degrades_to_error_dict(label: str, content: str | None) -> None:
    """Malformed replies must not raise — a raise here kills the whole sweep.

    ``json.loads`` is inside the same ``try`` as the API call, so every parse
    failure lands in the ``except Exception`` handler and returns the error
    dict. Note ``prose around fence``: the unwrapping only fires when the reply
    *starts* with a fence, so a chatty preamble is not recovered.
    """
    result = analyze(content)

    assert isinstance(result, dict), label
    assert "error" in result, label
    assert result["recommendations"] == []
    assert result["alerts"] == []
    # The consumer's access pattern must stay safe.
    assert result.get("issues_to_create", []) == []


def test_error_dict_has_no_issues_to_create_key() -> None:
    """A failed analysis must not be able to file issues.

    ``run_health_scan`` reads ``analysis.get("issues_to_create", [])``, so the
    key being absent is what makes a failed scan file nothing at all.
    """
    result = analyze("not json")

    assert "issues_to_create" not in result


def test_valid_json_missing_issues_to_create_is_safe() -> None:
    """A well-formed reply that simply omits the key yields [] to the caller."""
    result = analyze(json.dumps({"alerts": ["x"], "recommendations": []}))

    assert result == {"alerts": ["x"], "recommendations": []}
    assert result.get("issues_to_create", []) == []


def test_missing_endpoint_returns_early_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)

    with mock_openai(CLEAN_JSON) as ctor:
        result = health_scan.analyze_with_ai({}, {})

    assert result == {"error": "AZURE_OPENAI_ENDPOINT not set", "recommendations": []}
    ctor.assert_not_called()


def test_api_error_degrades_to_error_dict() -> None:
    """A transport failure must be swallowed the same way a parse failure is."""
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("503 upstream")

    with patch("openai.AzureOpenAI", return_value=client):
        result = health_scan.analyze_with_ai({}, {})

    assert result["error"] == "503 upstream"
    assert result.get("issues_to_create", []) == []


# ── characterization: valid JSON that is not an object ─────────────────────
@pytest.mark.parametrize(
    ("label", "content", "expected"),
    [
        ("list", '[{"repo": "era"}]', [{"repo": "era"}]),
        ("bare string", '"just a string"', "just a string"),
        ("null", "null", None),
        ("number", "42", 42),
    ],
)
def test_non_object_json_is_returned_verbatim(
    label: str, content: str, expected: Any
) -> None:
    """KNOWN DEFECT: a non-object JSON reply is returned as-is, not coerced.

    ``json.loads`` succeeds for any JSON value, so a reply of ``[...]`` or
    ``"..."`` bypasses the error handler entirely and ``analyze_with_ai``
    returns a list / str / None where every caller expects a dict. See
    ``test_non_object_json_breaks_the_caller`` for the consequence.
    """
    assert analyze(content) == expected


@pytest.mark.parametrize("content", ['[{"repo": "era"}]', '"a string"', "null"])
def test_non_object_json_breaks_the_caller(content: str) -> None:
    """KNOWN DEFECT: the non-object reply above crashes the health scan.

    ``run_health_scan`` passes the result to ``generate_report``, which does
    ``analysis.get("alerts", [])``. On a list / str / None that is an
    ``AttributeError``, so a single malformed model reply aborts the whole
    daily sweep — no report, no issues, and no Telegram notification saying
    it failed.
    """
    result = analyze(content)

    with pytest.raises(AttributeError):
        health_scan.generate_report({}, {"total": 0, "by_resource_group": {}}, result)


def test_fenced_reply_containing_a_code_fence_is_truncated() -> None:
    """KNOWN DEFECT: an inner ``` fence discards the entire response.

    The unwrapper is ``raw.split("```")[1]``, which keeps only the text up to
    the *next* fence. The system prompt asks the model for issue bodies about
    "recurring JS exceptions with stack traces" — precisely the content a model
    fences. If the model also fences its whole reply, the JSON is cut mid-string
    and the sweep silently files zero issues.
    """
    body = "Repro:\n```\nTypeError: x is not a function\n```\n"
    payload = json.dumps(
        {"issues_to_create": [{"repo": "era", "title": "Fix it", "body": body}]}
    )

    result = analyze(f"```json\n{payload}\n```")

    assert "error" in result
    assert result.get("issues_to_create", []) == []


# ── request shape: cost discipline and payload wiring ──────────────────────
def test_defaults_to_gpt_4o_mini() -> None:
    """AGENTS.md pins the daily scan to a cheap deployment; keep it pinned."""
    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    kwargs = ctor.client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 2000
    assert kwargs["temperature"] == 0.1


def test_health_scan_model_env_overrides_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTH_SCAN_MODEL", "gpt-5")

    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    assert ctor.client.chat.completions.create.call_args.kwargs["model"] == "gpt-5"


def test_scan_data_reaches_the_user_message() -> None:
    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai(
            {"era": {"open_issues": 7}},
            {"total": 42},
            {"era": {"page_views": 0}},
            {"era": {"status": 500}},
        )

    messages = ctor.client.chat.completions.create.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    user_msg = messages[1]["content"]
    for needle in ("open_issues", "42", "page_views", "500"):
        assert needle in user_msg


def test_api_key_is_passed_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2099-01-01")

    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    kwargs = ctor.call_args.kwargs
    assert kwargs["api_key"] == "fake-key"
    assert kwargs["azure_endpoint"] == "https://fake.openai.azure.com"
    assert kwargs["api_version"] == "2099-01-01"


def test_falls_back_to_managed_identity_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI authenticates with a managed identity, not a key."""
    monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)

    with (
        mock_openai(CLEAN_JSON) as ctor,
        patch("azure.identity.DefaultAzureCredential") as cred,
        patch("azure.identity.get_bearer_token_provider", return_value="token-fn"),
    ):
        result = health_scan.analyze_with_ai({}, {})

    assert result == PAYLOAD
    cred.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert kwargs["azure_ad_token_provider"] == "token-fn"
    assert "api_key" not in kwargs
