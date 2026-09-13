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
            "approach": "Implement serialize_csv in src/export.py using csv.writer.",
            "success_criteria": "pytest tests/test_export.py verifies header order and quoted cells.",
        }],
    }
