"""Load every real REFEREE-produced JSON artifact through the actual schema.py models and
report any `ValidationError` raised by one of the ten ENFORCED fields — the empirical half
of building `schema.LEGACY_VALUES`, called for in that module's own docstring.

Uses `model_validate` against the REAL classes (not a blind recursive key-name walk, which
over-matches: this repo's `projects/` tree also holds an unrelated ML-experiment pipeline's
own `project.json`/`manifest.json` files that happen to share field names like `status` and
`phase` with completely different meanings). Scoped to the known REFEREE file roles only:

    controller.json                              -> CaseState
    runs/<pid>/spec.json, .../targets/*/spec.json -> ProbeSpec
    control/targets/*/outcome.json, runs/<pid>/targets/*/outcome.json,
    discovery/targets.json's `outcomes` list      -> TargetOutcome

`python tools/validate_legacy_json.py` runs it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "projects"
EXCLUDE_PARTS = {"repo", "env", ".git", "venv"}


def is_harness_authored(path: Path) -> bool:
    return not (set(path.relative_to(ROOT).parts) & EXCLUDE_PARTS)


def check(model, data, path_hint: str, violations: list[str]) -> None:
    try:
        model.model_validate(data)
    except Exception as e:
        violations.append(f"{path_hint}: {e}")


def main() -> int:
    violations: list[str] = []
    files = [f for f in ROOT.rglob("*.json") if is_harness_authored(f)]

    for f in files:
        rel = str(f.relative_to(ROOT))
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        parts = f.relative_to(ROOT).parts
        if f.name == "controller.json" and len(parts) == 2:
            check(schema.CaseState, data, rel, violations)
        elif f.name == "spec.json" and "runs" in parts and "paper_id" in data:
            # `paper_id` presence excludes an unrelated ML-experiment pipeline sharing this
            # repo's `projects/` tree, which also names a file `spec.json` under
            # `studies/`/`candidates/` for an unrelated idea-specification shape.
            check(schema.ProbeSpec, data, rel, violations)
        elif f.name == "outcome.json":
            check(schema.TargetOutcome, data, rel, violations)
        elif f.name == "targets.json" and "outcomes" in data:
            for i, o in enumerate(data.get("outcomes") or []):
                check(schema.TargetOutcome, o, f"{rel}#outcomes[{i}]", violations)

    if not violations:
        print("no ValidationError from any of the 10 enforced fields, across "
              f"{len(files)} harness-authored JSON files")
        return 0

    print(f"{len(violations)} validation failure(s):\n")
    for v in violations:
        print(v)
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
