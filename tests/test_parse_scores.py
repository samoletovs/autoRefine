"""Tests for agent/parse_scores.py — specifically the multi-object scanner."""

import json
import os
import tempfile

from agent.parse_scores import extract_score_objects


def _write_report(objects: list[dict]) -> str:
    """Write report objects to a temp file and return its path."""
    content = "\n".join(json.dumps(obj, indent=2) for obj in objects)
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.write(content)
    f.close()
    return f.name


def _make_report(name: str, score: int) -> dict:
    return {
        "project": name,
        "stage": "active",
        "findings": [{"category": "test", "description": f"issue for {name}", "priority": "P2"}],
        "score": score,
    }


def test_single_project():
    path = _write_report([_make_report("agentMode", 95)])
    try:
        result = extract_score_objects(path)
        assert len(result) == 1
        assert result[0]["project"] == "agentMode"
        assert result[0]["score"] == 95
    finally:
        os.unlink(path)


def test_two_projects():
    objects = [_make_report("agentMode", 95), _make_report("amberRepublic", 90)]
    path = _write_report(objects)
    try:
        result = extract_score_objects(path)
        assert len(result) == 2
        names = [r["project"] for r in result]
        assert "agentMode" in names
        assert "amberRepublic" in names
    finally:
        os.unlink(path)


def test_all_sixteen_projects():
    """Regression test: idx = size (not idx += size) ensures all objects are found."""
    names = [
        "agentMode", "amberRepublic", "era", "atlas", "foundryLab",
        "golazo", "playground", "portaBaltica", "rosette", "tPlan",
        "turgo", "art", "payArc", "mindMe", "autoRefine", "agentFlow",
    ]
    scores = list(range(80, 96))  # 16 distinct scores
    objects = [_make_report(n, scores[i % len(scores)]) for i, n in enumerate(names)]
    path = _write_report(objects)
    try:
        result = extract_score_objects(path)
        assert len(result) == 16, f"Expected 16 objects, got {len(result)}: {[r['project'] for r in result]}"
        found_names = {r["project"] for r in result}
        assert found_names == set(names)
    finally:
        os.unlink(path)


def test_skips_objects_without_project_or_score():
    objects = [
        {"only_score": 90},
        _make_report("agentMode", 95),
        {"project": "noScore"},
        _make_report("era", 88),
    ]
    path = _write_report(objects)
    try:
        result = extract_score_objects(path)
        assert len(result) == 2
        names = {r["project"] for r in result}
        assert names == {"agentMode", "era"}
    finally:
        os.unlink(path)


def test_empty_file():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.close()
    try:
        result = extract_score_objects(f.name)
        assert result == []
    finally:
        os.unlink(f.name)


def test_findings_with_special_chars():
    """Descriptions with special characters should not break parsing."""
    obj = _make_report("rosette", 92)
    obj["findings"][0]["description"] = 'Use {x} and "quotes" and \\backslash'
    path = _write_report([obj, _make_report("turgo", 88)])
    try:
        result = extract_score_objects(path)
        assert len(result) == 2
    finally:
        os.unlink(path)
