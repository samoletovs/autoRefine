"""The agent instructions must stay long enough to be cacheable.

Azure prompt caching only engages on a prefix of **at least 1024 identical
tokens**, then in 128-token increments, and bills a hit at a reduced input
rate. Below that threshold nothing caches at all.

That makes prompt length a cost cliff with no telemetry behind it: the Agents
run object reports only ``prompt_tokens``/``completion_tokens``/``total_tokens``
(see ``RunCompletionUsage`` in the installed ``azure-ai-agents``), with no
``cached_tokens`` field, so a run that quietly stopped caching looks exactly
like one that did not. The only instrument is the Azure bill, weeks later.

These tests are the substitute for that missing telemetry. They turn an
invisible cost cliff into a failing build.

Note the guarantee we pin is deliberately stronger than "instructions + tools
exceed 1024": Microsoft documents *that* tool definitions are cacheable but not
*where* they are serialised relative to the system message. So we require
``system.md`` to clear the threshold **on its own**, which holds whatever that
order turns out to be.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent import foundry_agent

# Azure: "A minimum of 1,024 tokens in length. The first 1,024 tokens in the
# prompt must be identical." A single differing character in that window is a
# miss.
CACHE_MIN_PREFIX_TOKENS = 1024
# Headroom so an ordinary edit cannot silently drop the prefix under the cliff.
# Caching resumes in 128-token increments above the minimum, so one increment
# is the natural margin.
CACHE_TARGET_TOKENS = CACHE_MIN_PREFIX_TOKENS + 128

# gpt-4o / gpt-4o-mini tokenise with o200k_base.
ENCODING_NAME = "o200k_base"


def _encoding() -> Any:
    """The o200k_base encoder, or skip.

    ``tiktoken`` resolves its vocabulary over the network on first use and
    caches it, so this skips rather than fails when the package is missing or
    the machine is offline. A red suite for a network blip would train people
    to ignore it; the hermetic character floor below still guards the
    invariant when this cannot run.
    """
    tiktoken = pytest.importorskip("tiktoken", reason="tiktoken not installed")
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    except Exception as exc:  # noqa: BLE001 - offline or vocab fetch failure
        pytest.skip(f"could not load {ENCODING_NAME}: {exc}")


def _plan_tool_definitions() -> list[dict]:
    """The tool definitions ``create_agent`` actually puts on a plan run.

    Captured through ``create_agent`` rather than rebuilt here, so adding or
    removing a tool moves this measurement instead of silently drifting from
    it.
    """
    captured: dict[str, Any] = {}

    class Recorder:
        # No list_agents, so the orphan sweep is skipped.
        def create_agent(self, **kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(id="agent-1")

    foundry_agent.create_agent(Recorder(), mode="plan")
    return [definition.as_dict() for definition in captured["tools"]]


def test_system_prompt_alone_clears_the_cache_threshold() -> None:
    """``system.md`` must be cacheable without help from the tool definitions."""
    encoding = _encoding()
    tokens = len(encoding.encode(foundry_agent.SYSTEM_PROMPT))

    assert tokens >= CACHE_TARGET_TOKENS, (
        f"agent/prompts/system.md is {tokens} tokens, under the {CACHE_TARGET_TOKENS}-token "
        f"target (hard floor {CACHE_MIN_PREFIX_TOKENS}). Below 1024 tokens Azure caches "
        "nothing, so every tool round of every run pays full input rate. Add durable "
        "guidance rather than filler, or accept the cost knowingly."
    )


def test_full_stable_prefix_clears_the_cache_threshold() -> None:
    """Instructions plus the real serialised tool definitions, as sent on a run."""
    encoding = _encoding()
    instructions = len(encoding.encode(foundry_agent.SYSTEM_PROMPT))
    tools = len(encoding.encode(json.dumps(_plan_tool_definitions(), sort_keys=True)))

    assert instructions + tools >= CACHE_TARGET_TOKENS, (
        f"stable prefix is {instructions + tools} tokens "
        f"(instructions {instructions} + tools {tools})"
    )


def test_prefix_char_floor_is_hermetic() -> None:
    """A no-dependency backstop for when the token tests skip.

    ``system.md`` measures ~4.2 characters per token under o200k_base. Five is
    a pessimistic bound for English prose, so this many characters implies at
    least ``CACHE_TARGET_TOKENS`` tokens without needing a tokeniser — which
    means CI still fails on a prompt that has been cut below the cliff even
    with no network and no ``tiktoken``.
    """
    pessimistic_chars_per_token = 5
    floor = CACHE_TARGET_TOKENS * pessimistic_chars_per_token
    actual = len(foundry_agent.SYSTEM_PROMPT)

    assert actual >= floor, (
        f"agent/prompts/system.md is {actual} characters, under the {floor}-character "
        f"floor that guarantees {CACHE_TARGET_TOKENS} tokens. See this module's docstring."
    )


def test_system_prompt_still_teaches_the_validated_contract() -> None:
    """The added guidance has to keep matching what the code actually enforces.

    ``is_specified`` drops any improvement whose ``approach`` or
    ``success_criteria`` is missing or merely restates the title, and only
    P0-P2 are filed. Guidance that drifts from those rules is worse than none,
    so the prompt is pinned to mention them.
    """
    prompt = foundry_agent.SYSTEM_PROMPT

    for term in ("approach", "success_criteria", "P0", "P1", "P2", "P3"):
        assert term in prompt, f"system.md no longer mentions {term!r}"
