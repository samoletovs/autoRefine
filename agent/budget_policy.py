"""Fail-closed gate for the scheduled autoRefine budget policy."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_POLICY_AGE = timedelta(hours=36)
MAX_FUTURE_SKEW = timedelta(minutes=5)
TIERS = frozenset({"normal", "low", "critical"})

log = logging.getLogger("autorefine.budget")


class BudgetPolicyError(ValueError):
    """The published policy is missing, stale, or invalid."""


@dataclass(frozen=True)
class BudgetPolicy:
    generated_at: datetime
    tier: str
    cadence_per_day: int
    run_deep_pass: bool
    has_fallback_reason: bool

    @property
    def permits_run(self) -> bool:
        return self.cadence_per_day > 0 and not self.has_fallback_reason


def load_budget_policy(path: Path, *, now: datetime | None = None) -> BudgetPolicy:
    """Load a fresh policy; invalid or unknown budget evidence never permits inference."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BudgetPolicyError("policy file is missing") from exc
    except OSError as exc:
        raise BudgetPolicyError(f"policy file cannot be read ({type(exc).__name__})") from exc
    except json.JSONDecodeError as exc:
        raise BudgetPolicyError("policy file is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise BudgetPolicyError("policy must be a JSON object")

    generated = payload.get("generated_at")
    if not isinstance(generated, str):
        raise BudgetPolicyError("generated_at must be a timestamp")
    try:
        generated_at = datetime.fromisoformat(generated)
    except ValueError as exc:
        raise BudgetPolicyError("generated_at must be an ISO-8601 timestamp") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise BudgetPolicyError("generated_at must include a timezone")

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a timezone")
    age = current_time.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)
    if age < -MAX_FUTURE_SKEW:
        raise BudgetPolicyError("generated_at is too far in the future")
    if age > MAX_POLICY_AGE:
        raise BudgetPolicyError("policy is stale")

    tier = payload.get("tier")
    cadence = payload.get("cadence_per_day")
    deep_pass = payload.get("run_deep_pass")
    fallback = payload.get("fallback_reason")
    if not isinstance(tier, str) or tier not in TIERS:
        raise BudgetPolicyError("tier is invalid")
    if type(cadence) is not int or cadence not in {0, 1}:
        raise BudgetPolicyError("cadence_per_day must be 0 or 1 for the daily job")
    if type(deep_pass) is not bool:
        raise BudgetPolicyError("run_deep_pass must be a boolean")
    if fallback is not None and not isinstance(fallback, str):
        raise BudgetPolicyError("fallback_reason must be a string when present")
    has_fallback = bool(fallback)
    if has_fallback and (tier != "critical" or cadence != 0 or deep_pass):
        raise BudgetPolicyError("fallback policy must stop scheduled inference")
    if (cadence == 0) != (tier == "critical"):
        raise BudgetPolicyError("tier and cadence disagree")

    return BudgetPolicy(
        generated_at=generated_at,
        tier=tier,
        cadence_per_day=cadence,
        run_deep_pass=deep_pass,
        has_fallback_reason=has_fallback,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        policy = load_budget_policy(args.policy)
    except BudgetPolicyError as exc:
        log.error("BUDGET POLICY INVALID: %s", exc)
        return 2

    if not policy.permits_run:
        log.warning(
            "BUDGET POLICY STOP: cadence_per_day=0 (tier=%s, generated_at=%s)",
            policy.tier,
            policy.generated_at.isoformat(),
        )
        return 3

    log.info(
        "budget policy permits scheduled inference (tier=%s, cadence_per_day=%d)",
        policy.tier,
        policy.cadence_per_day,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
