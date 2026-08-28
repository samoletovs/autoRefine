"""Suite-wide fixtures.

The one here exists because a correct guard in the wrong scope protects nothing.
``tests/test_run_cost_rows.py`` has unset ``AUTOREFINE_*`` for its own module since the
cost log was added — the author knew these variables leak. It could not help
``tests/test_foundry_agent.py``, which also calls ``run_agent``, and on 2026-08-28 that
module wrote 18 fixture rows for a project named ``demo`` into the live cost log during a
production sweep: autoRefine plans itself, the model calls ``run_project_tests``, and
pytest inherited the entrypoint's ``AUTOREFINE_COST_LOG``.

``agent.foundry_agent._test_subprocess_env`` closes that path at the chokepoint, so
production is safe whatever the suite does. This closes it from the other side, for the
developer who exports the variable in their own shell, and states the rule the suite
should have had all along: **no test may depend on an ``AUTOREFINE_*`` it did not set
itself.** A test that wants one still sets it with ``monkeypatch.setenv``.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_autorefine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("AUTOREFINE_")]:
        monkeypatch.delenv(key, raising=False)
