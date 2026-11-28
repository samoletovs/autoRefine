"""Parse evaluation score objects from the autoRefine JSON report output.

Usage: python parse_scores.py <report_file>
Stdout: one JSON line per project evaluation object (has both 'score' and 'project').
"""

import json
import sys


def extract_json_objects(path: str) -> list[dict]:
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8") as f:
        content = f.read()
    idx = 0
    objects = []
    while idx < len(content):
        try:
            obj, size = dec.raw_decode(content, idx)
            if isinstance(obj, dict):
                objects.append(obj)
            idx = size
        except json.JSONDecodeError:
            idx += 1
    return objects


def extract_score_objects(path: str) -> list[dict]:
    return [
        obj for obj in extract_json_objects(path)
        if "score" in obj and "project" in obj
    ]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: parse_scores.py <report_file>", file=sys.stderr)
        sys.exit(1)
    for obj in extract_score_objects(sys.argv[1]):
        print(json.dumps(obj))
