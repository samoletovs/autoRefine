"""Quick local evaluation of all projects with project.yaml."""
from pathlib import Path

from agent.config import ProjectConfig
from agent.tools.quality_tools import run_quality_checks

root = Path(__file__).parent.parent.parent  # workspace root


def main() -> None:
    projects = []
    for d in sorted(root.iterdir()):
        yaml_path = d / "project.yaml"
        if not yaml_path.exists():
            continue
        config = ProjectConfig.from_yaml(yaml_path)
        findings = run_quality_checks(str(d), config)
        score = max(0, 100 - sum(f.weight for f in findings))
        projects.append({
            "project": config.name,
            "stage": config.stage,
            "score": score,
            "findings": len(findings),
            "details": [f"{f.priority} {f.category}: {f.description}" for f in findings],
        })

    projects.sort(key=lambda p: p["score"])
    print(f"\n{'=' * 60}")
    print(f"autoRefine Evaluation — {len(projects)} projects")
    print(f"{'=' * 60}\n")
    for p in projects:
        if p["score"] >= 90:
            icon = "+"
        elif p["score"] >= 70:
            icon = "~"
        else:
            icon = "!"
        print(f"[{icon}] {p['project']:20s} {p['score']:3d}/100  ({p['stage']})  [{p['findings']} findings]")
        for d in p["details"]:
            print(f"     {d}")
        print()


if __name__ == "__main__":
    main()
