"""Contract: nothing autoRefine owns resolves to an Azure OpenAI model that retires.

gpt-4o, gpt-4o-mini and gpt-4.1(-mini/-nano) retire 2027-04-14; o3 and o4-mini
retire 2026-11-19. Their deployments auto-upgrade to gpt-6-luna, which rejects
``max_tokens`` (HTTP 400) and rejects ``temperature`` in a prompt-agent
definition at response time. Each test here fails if one of those paths returns.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent import config, foundry_agent, health_scan

ROOT = Path(__file__).resolve().parents[1]
RETIRING = re.compile(r"gpt-4o|gpt-4\.1|\bo3\b|o3-mini|o4-mini", re.IGNORECASE)


def _assert_not_retiring(value: str) -> None:
    assert not RETIRING.search(value), f"retiring model resolved: {value!r}"


def test_no_default_or_allowlist_names_a_retiring_model() -> None:
    _assert_not_retiring(config.DEFAULT_FOUNDRY_DEPLOYMENT)
    _assert_not_retiring(foundry_agent.DEFAULT_DEPLOYMENT)
    for name in config.SUPPORTED_FOUNDRY_DEPLOYMENTS:
        _assert_not_retiring(name)
    for name in foundry_agent.MODEL_PRICES:
        _assert_not_retiring(name)
    assert config.DEFAULT_FOUNDRY_DEPLOYMENT == "gpt-6-luna"


@pytest.mark.parametrize("path", ["infrastructure/main.bicep", ".env.example"])
def test_deployment_config_names_no_retiring_model(path: str) -> None:
    _assert_not_retiring((ROOT / path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini", "gpt-6-sol"])
def test_foundry_gate_refuses_retiring_and_unadmitted_models(model: str) -> None:
    with pytest.raises(ValueError):
        config.require_foundry_deployment(model)


@pytest.mark.parametrize("mode", ["plan", "refine"])
def test_luna_agent_definition_has_no_sampling_params_and_no_reasoning(mode: str) -> None:
    definition = foundry_agent.build_agent_definition(mode, "gpt-6-luna")
    serialized = definition.as_dict()

    assert serialized["model"] == "gpt-6-luna"
    assert "temperature" not in serialized
    assert "top_p" not in serialized
    assert serialized["reasoning"]["effort"] == "none"


def _health_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "fake-key")
    monkeypatch.delenv("HEALTH_SCAN_MODEL", raising=False)
    monkeypatch.delenv("HEALTH_SCAN_MAX_TOKENS", raising=False)
    client = MagicMock()
    choice = MagicMock(finish_reason="stop")
    choice.message.content = '{"issues_to_create": []}'
    client.chat.completions.create.return_value = MagicMock(choices=[choice])
    with patch("openai.AzureOpenAI", return_value=client):
        health_scan.analyze_with_ai({}, {})
    return client.chat.completions.create.call_args.kwargs


def test_health_request_uses_luna_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _health_request(monkeypatch)

    assert body["model"] == "gpt-6-luna"
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] <= 4000
    assert body["reasoning_effort"] == "none"
    assert "temperature" not in body
    assert "top_p" not in body
