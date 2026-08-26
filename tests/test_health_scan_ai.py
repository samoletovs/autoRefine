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
import logging
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
    monkeypatch.delenv("HEALTH_SCAN_MAX_TOKENS", raising=False)


@contextmanager
def mock_openai(content: str | None, finish_reason: str = "stop") -> Iterator[MagicMock]:
    """Patch ``openai.AzureOpenAI`` so the model returns *content* verbatim."""
    client = MagicMock()
    response = MagicMock()
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    response.choices = [choice]
    client.chat.completions.create.return_value = response
    with patch("openai.AzureOpenAI", return_value=client) as ctor:
        ctor.client = client
        yield ctor


def analyze(content: str | None, finish_reason: str = "stop") -> Any:
    with mock_openai(content, finish_reason):
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
        ("unclosed fence", "```json\n{\"alerts\": [1, 2"),
    ],
)
def test_unparseable_reply_degrades_to_error_dict(label: str, content: str | None) -> None:
    """Malformed replies must not raise — a raise here kills the whole sweep.

    Parsing sits inside the same ``try`` as the API call, so every failure
    lands in the ``except Exception`` handler and returns the error dict.
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


# ── non-object JSON is coerced to the degraded shape (was a defect) ────────
@pytest.mark.parametrize(
    ("label", "content", "type_name"),
    [
        ("list", '[{"repo": "era"}]', "list"),
        ("bare string", '"just a string"', "str"),
        ("null", "null", "NoneType"),
        ("number", "42", "int"),
    ],
)
def test_non_object_json_degrades_to_error_dict(
    label: str, content: str, type_name: str
) -> None:
    """FIXED: valid JSON of the wrong shape no longer escapes as a non-dict.

    ``json.loads`` succeeds for any JSON value, so a reply of ``[...]`` or
    ``"..."`` used to bypass the error handler and return a list / str / None
    where every caller expects a dict. It is now rejected and degraded like any
    other bad reply.
    """
    result = analyze(content)

    assert isinstance(result, dict), label
    assert type_name in result["error"]
    assert result["recommendations"] == []
    assert result["alerts"] == []


@pytest.mark.parametrize("content", ['[{"repo": "era"}]', '"a string"', "null", "42"])
def test_non_object_json_no_longer_breaks_the_caller(content: str) -> None:
    """FIXED: a wrong-shaped reply no longer aborts the sweep.

    ``run_health_scan`` passes the result to ``generate_report`` at
    health_scan.py:1008 — before ``commit_report``, before
    ``create_github_issues`` and before the Telegram send. An ``AttributeError``
    there killed the run silently: no report, no issues, and no notification
    saying it had failed.
    """
    result = analyze(content)

    report = health_scan.generate_report(
        {"era": {"open_issues": 1}}, {"total": 0, "by_resource_group": {}}, result
    )

    assert "NauroLabs Health Report" in report


def test_non_object_json_files_no_issues() -> None:
    """A rejected reply must not be able to file issues."""
    result = analyze('[{"repo": "era"}]')

    assert "issues_to_create" not in result
    assert result.get("issues_to_create", []) == []


def test_fenced_reply_containing_a_code_fence_is_recovered() -> None:
    """FIXED: an inner ``` fence no longer discards the response.

    The old unwrapper was ``raw.split("```")[1]``, which keeps only the text up
    to the *next* fence. The system prompt asks the model for issue bodies about
    "recurring JS exceptions with stack traces" — precisely the content a model
    fences — so a fenced reply containing a fenced body was cut mid-string and
    the sweep silently filed zero issues.
    """
    body = "Repro:\n```\nTypeError: x is not a function\n```\n"
    payload = {"issues_to_create": [{"repo": "era", "title": "Fix it", "body": body}]}

    result = analyze(f"```json\n{json.dumps(payload)}\n```")

    assert result == payload
    assert result["issues_to_create"][0]["body"] == body


def test_reply_with_prose_around_the_fence_is_recovered() -> None:
    """A chatty preamble and postamble no longer cost the sweep its answer."""
    result = analyze(f"Here you go:\n```json\n{CLEAN_JSON}\n```\nHope that helps!")

    assert result == PAYLOAD


def test_reply_with_bare_prose_around_raw_json_is_recovered() -> None:
    """No fence at all, just prose either side of the object."""
    result = analyze(f"Sure. {CLEAN_JSON} Let me know if you need more.")

    assert result == PAYLOAD


# ── truncation is reported as truncation, not as a parse error ─────────────
def test_truncated_reply_is_reported_as_truncation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reply cut off at the ceiling must say so, loudly and specifically.

    Silent truncation is the same class of loss as the fence bug — a good model
    producing a long answer files zero issues — but the cause and the fix are
    different, so a generic JSON error would send the reader the wrong way.
    """
    with caplog.at_level(logging.ERROR, logger="agent.health_scan"):
        result = analyze('{"alerts": ["a", "b"', finish_reason="length")

    assert "truncated" in result["error"]
    assert "4000" in result["error"]
    assert result.get("issues_to_create", []) == []
    assert any(
        "token output ceiling" in r.message and "HEALTH_SCAN_MAX_TOKENS" in r.message
        for r in caplog.records
    )


def test_truncation_is_detected_even_when_the_json_happens_to_parse(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``finish_reason`` is authoritative — the model wanted to write more.

    A reply can be cut off at a point where the JSON still parses (a closed
    object with the issues list missing). Trusting the parse there would file a
    silently incomplete analysis.
    """
    with caplog.at_level(logging.ERROR, logger="agent.health_scan"):
        result = analyze(CLEAN_JSON, finish_reason="length")

    assert "truncated" in result["error"]
    assert "issues_to_create" not in result


def test_normal_finish_reason_is_not_treated_as_truncation() -> None:
    assert analyze(CLEAN_JSON, finish_reason="stop") == PAYLOAD


def test_missing_finish_reason_is_not_treated_as_truncation() -> None:
    """Older or stubbed clients may not populate finish_reason at all."""
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = CLEAN_JSON
    del choice.finish_reason
    response.choices = [choice]
    client.chat.completions.create.return_value = response

    with patch("openai.AzureOpenAI", return_value=client):
        assert health_scan.analyze_with_ai({}, {}) == PAYLOAD


# ── request shape: cost discipline and payload wiring ──────────────────────
def test_defaults_to_gpt_4o_mini() -> None:
    """AGENTS.md pins the daily scan to a cheap deployment; keep it pinned."""
    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    kwargs = ctor.client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["temperature"] == 0.1


def test_output_ceiling_fits_a_full_fleet_answer() -> None:
    """2000 was not enough and truncated silently; see the comment at the call.

    Tokenised with o200k_base, the prompt's schema for a 24-repo fleet with the
    5 issues ``create_github_issues`` will actually file measures 2035 tokens
    compact / 2312 indented — over the old ceiling, and the overflow lands on
    ``issues_to_create`` because it is emitted last.
    """
    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    assert ctor.client.chat.completions.create.call_args.kwargs["max_tokens"] == 4000


def test_max_tokens_env_overrides_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can dial the ceiling back without a code change."""
    monkeypatch.setenv("HEALTH_SCAN_MAX_TOKENS", "1500")

    with mock_openai(CLEAN_JSON) as ctor:
        health_scan.analyze_with_ai({}, {})

    assert ctor.client.chat.completions.create.call_args.kwargs["max_tokens"] == 1500


@pytest.mark.parametrize("bad", ["", "abc", "2000tokens", "0", "-1", "3.5"])
def test_unusable_max_tokens_env_falls_back_to_the_default(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd knob must not abort the scan.

    This is read before the request's ``try``, so an uncaught ``int()`` failure
    here would take down the whole sweep — the failure mode this change exists
    to remove.
    """
    monkeypatch.setenv("HEALTH_SCAN_MAX_TOKENS", bad)

    with mock_openai(CLEAN_JSON) as ctor:
        result = health_scan.analyze_with_ai({}, {})

    assert result == PAYLOAD
    assert ctor.client.chat.completions.create.call_args.kwargs["max_tokens"] == 4000


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


# ── _parse_analysis_reply, directly ────────────────────────────────────────
@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("bare object", CLEAN_JSON),
        ("json fence", f"```json\n{CLEAN_JSON}\n```"),
        ("bare fence", f"```\n{CLEAN_JSON}\n```"),
        ("JSON fence, uppercase tag", f"```JSON\n{CLEAN_JSON}\n```"),
        ("preamble", f"Sure thing:\n{CLEAN_JSON}"),
        ("postamble", f"{CLEAN_JSON}\nLet me know!"),
        ("both", f"Here:\n```json\n{CLEAN_JSON}\n```\nDone."),
        ("leading whitespace", f"\n\n  {CLEAN_JSON}  \n"),
    ],
)
def test_parse_analysis_reply_accepts_wrappers(label: str, raw: str) -> None:
    assert health_scan._parse_analysis_reply(raw) == PAYLOAD, label


def test_parse_analysis_reply_takes_the_outermost_braces() -> None:
    """Nested objects must not truncate the span the way the old split did."""
    nested = {"health_scores": {"era": {"R": 1, "L": 2, "M": 3, "health": 6}}}

    assert health_scan._parse_analysis_reply(
        f"```json\n{json.dumps(nested)}\n```"
    ) == nested


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("empty", ""),
        ("whitespace", "  \n "),
        ("prose only", "I cannot help with that."),
        ("no closing brace", '{"alerts": ['),
    ],
)
def test_parse_analysis_reply_rejects_unparseable_text(label: str, raw: str) -> None:
    with pytest.raises(ValueError):
        health_scan._parse_analysis_reply(raw)


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("json array", '[{"repo": "era"}]'),
        ("json string", '"hello"'),
        ("json null", "null"),
        ("json number", "42"),
    ],
)
def test_parse_analysis_reply_rejects_non_objects(label: str, raw: str) -> None:
    """Valid JSON of the wrong shape is a type mismatch, not a parse failure."""
    with pytest.raises(TypeError, match="expected a JSON object"):
        health_scan._parse_analysis_reply(raw)
