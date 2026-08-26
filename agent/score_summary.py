"""Summarise autoRefine evaluation scores for the CI workflow.

Usage: ``python -m agent.score_summary <report_file>``

Writes ``scores``, ``total`` and ``avg`` to ``$GITHUB_OUTPUT`` (stdout when that
is unset, which is what makes it runnable by hand). Exits 1 when the report
holds no score objects, which the workflow reads as ``steps.*.outcome ==
'failure'``.

This exists for the same reason ``parse_scores.py`` does: the arithmetic used to
live in a ``run:`` block as a jq pipeline, where it was unreachable by the test
suite. It got the count wrong for a year — ``total`` was measured on the list
*after* ``head -20`` had truncated it, so a 25-project manifest reported "20
projects, avg 82/100": two different denominators in one sentence. In Python the
count and the average read the same list, and a test can say so.
"""

from __future__ import annotations

import math
import os
import sys
import uuid
from dataclasses import dataclass

from agent.parse_scores import extract_score_objects

# How many per-project lines to show before collapsing the rest into a count.
# Telegram renders long messages badly, and the average already covers the tail.
MAX_LISTED = 20


@dataclass(frozen=True)
class Summary:
    """The three values the workflow hands to the Telegram notifier."""

    scores: str
    total: int
    avg: int


def _coverage_suffix(obj: dict) -> str:
    """Render ``" (2/6 measured)"`` for one project, or ``""`` when unknown.

    A score is a numerator; ``coverage`` is its denominator. Most quality checks
    only run when the project's own ``project.yaml`` declares the trait, so a
    project that declares nothing scores 100/100 having been measured on one
    dimension out of six. Printing the score alone reads as praise for that.

    Absent or malformed coverage renders as nothing rather than as ``0/0``:
    reports predating this field, and the tests that build objects by hand, must
    keep summarising cleanly.
    """
    coverage = obj.get("coverage")
    if not isinstance(coverage, dict):
        return ""
    measured, total = coverage.get("measured"), coverage.get("total")
    # `bool` is an `int` in Python, and " (True/6 measured)" is not a message
    # worth sending anyone.
    if isinstance(measured, bool) or isinstance(total, bool):
        return ""
    if not isinstance(measured, int) or not isinstance(total, int) or total <= 0:
        return ""
    return f" ({measured}/{total} measured)"


def summarise(objects: list[dict], max_listed: int = MAX_LISTED) -> Summary:
    """Build the score summary from every evaluated project.

    ``total`` and ``avg`` are both computed over ``objects`` — the full list.
    Only ``scores`` is truncated, and when it is, it says so rather than
    silently dropping projects off the end.

    Each line carries its coverage, so "100/100" cannot be read as a clean bill
    of health when only two of six dimensions were ever inspected.
    """
    if not objects:
        raise ValueError("no score objects to summarise")

    lines = [
        f"{obj['project']}: {obj['score']}/100{_coverage_suffix(obj)}"
        for obj in objects
    ]
    total = len(objects)

    shown = lines[:max_listed]
    if total > max_listed:
        shown.append(f"… and {total - max_listed} more")

    # floor(), matching the jq `add / length | floor` this replaced, so the
    # number in Telegram does not shift the day this lands.
    avg = math.floor(sum(obj["score"] for obj in objects) / total)

    return Summary(scores="\n".join(shown), total=total, avg=avg)


def render_github_output(summary: Summary) -> str:
    """Render the summary as ``$GITHUB_OUTPUT`` lines.

    ``scores`` is multi-line, so it needs a heredoc delimiter. A fixed one is a
    parser injection waiting to happen: any value containing that word on its
    own line closes the block early and every key after it is corrupted. The
    delimiter is therefore random *and* re-drawn until it is absent from the
    payload, which is GitHub's own documented guidance.
    """
    delimiter = f"scores_{uuid.uuid4().hex}"
    while delimiter in summary.scores:  # pragma: no cover - practically unreachable
        delimiter = f"scores_{uuid.uuid4().hex}"

    return (
        f"scores<<{delimiter}\n"
        f"{summary.scores}\n"
        f"{delimiter}\n"
        f"total={summary.total}\n"
        f"avg={summary.avg}\n"
        # Kept as an alias because the name predates `avg` and is cheap to honour.
        f"score={summary.avg}\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: score_summary.py <report_file>", file=sys.stderr)
        return 2

    report_file = argv[1]
    try:
        objects = extract_score_objects(report_file)
    except OSError as exc:
        print(f"::warning::Could not read {report_file}: {exc}", file=sys.stderr)
        objects = []

    if not objects:
        print(f"::warning::No score found in {report_file}", file=sys.stderr)
        return 1

    payload = render_github_output(summarise(objects))

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload)

    print(f"Summarised {len(objects)} project(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
