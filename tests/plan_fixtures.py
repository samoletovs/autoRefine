"""Synthetic, fileable plans for completed-run contract tests."""


def valid_plan(score: int = 72) -> dict:
    return {
        "score": score,
        "summary": "A promised export is missing.",
        "improvements": [{
            "title": "Export the visible rows",
            "description": "Users cannot download the rows currently displayed.",
            "priority": "P1",
            "effort": "S",
            "category": "feature",
            "approach": "Create src/export.py with serialize_csv built on csv.writer.",
            "success_criteria": (
                "A new tests/test_export.py passes under pytest tests/test_export.py, "
                "checking header order and quoted cells."
            ),
        }],
    }
