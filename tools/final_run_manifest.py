"""The run's own provenance record: what was read, by what, under which revision.

`python tools/final_run_manifest.py [run_dir]` writes `<run_dir>/RUN_MANIFEST.json`.

Section 12 of the delivery contract asks for the controller, the model, the reasoning
configuration, the paper hashes, the prompt hashes, the code revision and the
environment. This writes exactly those and NOTHING it cannot read off an artifact.

Two things it deliberately does not do. It does not pool delegation modes: a run whose
readings came from a CLI subprocess and from a session subagent is reported as both, with
counts, because `harness.delegation.summarise` exists precisely so a corpus can never be
described as one homogeneous run when it is not. And it does not claim a tool policy for
a mode that cannot prove one — the per-lens sidecar's own `tool_policy_provable` is
carried through rather than re-asserted here.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import delegation  # noqa: E402

PAPERS = ["0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
          "apt-icml", "cvpr", "iclr", "sanchez24a-icml"]
LENSES = ("confound", "contradiction", "overclaim", "protocol")


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001 — absence is a fact
        return None


def _readings(root: Path, lens: str) -> list[dict]:
    """Every sealed reading behind one lens, whether it was one pass or several.

    A composed lens file's provenance is BY REFERENCE — `compose_lens` writes each part's
    hash into `composed_from` — so walking the composed record is what makes a split
    paper's manifest as complete as a whole-paper one, rather than recording a single
    hash for an artifact nobody read in one sitting.
    """
    side = _json(root / "audit" / f"{lens}.driver.json")
    if not isinstance(side, dict):
        return []
    parts = side.get("composed_from")
    if not parts:
        return [{
            "unit_id": lens,
            "delegation_mode": delegation.mode_of(side),
            "model_reported": (side.get("envelope") or {}).get("model_reported", ""),
            "model_requested": side.get("model_requested", ""),
            "tool_policy": side.get("tool_policy", "unrecorded"),
            "tool_policy_provable": bool(side.get("tool_policy_provable")),
            "prompt_sha256": side.get("prompt_sha256", ""),
            "content_sha256": side.get("content_sha256", ""),
            "findings": side.get("findings"),
        }]
    out = []
    for ref in parts:
        unit_side = _json(Path(str(ref.get("path", ""))).with_suffix(".driver.json")) or {}
        out.append({
            "unit_id": ref.get("unit_id", ""),
            "kind": ref.get("kind", ""),
            "delegation_mode": delegation.mode_of(unit_side),
            "model_reported": ref.get("model_reported", ""),
            "model_requested": unit_side.get("model_requested", ""),
            "tool_policy": unit_side.get("tool_policy", "unrecorded"),
            "tool_policy_provable": bool(unit_side.get("tool_policy_provable")),
            "prompt_sha256": ref.get("prompt_sha256", ""),
            "content_sha256": ref.get("content_sha256", ""),
            "findings": ref.get("findings"),
        })
    return out


def paper(run: Path, pid: str) -> dict:
    root = run / "projects" / pid
    ctrl = _json(root / "controller.json") or {}
    pdf = Path(str(ctrl.get("pdf_path") or ""))
    if pdf and not pdf.is_absolute():
        pdf = Path(__file__).resolve().parents[1] / pdf
    readings = {lens: _readings(root, lens) for lens in LENSES}
    grades = sorted((root / "audit" / "grade").glob("*.json"))
    return {
        "paper_id": pid,
        "terminal": {"phase": ctrl.get("phase"), "status": ctrl.get("status"),
                     "failure_kind": ctrl.get("failure_kind") or "",
                     "disposition": ctrl.get("disposition")},
        "sha256": {
            "paper_pdf": _sha(pdf),
            "rendered_paper": _sha(root / "paper" / "doc.json"),
            "targets": _sha(root / "discovery" / "targets.json"),
            "reviewer_report": _sha(root / "reports" / f"{pid}.review.md"),
            "machine_report": _sha(root / "reports" / f"{pid}.json"),
            "ledger": _sha(root / "reports" / f"{pid}.ledger.json"),
        },
        "readings": readings,
        "readings_total": sum(len(v) for v in readings.values()),
        "grades": len([g for g in grades if not g.name.endswith(".driver.json")]),
    }


def build(run: Path) -> dict:
    rows = [paper(run, pid) for pid in PAPERS]
    sidecars = [r for row in rows for v in row["readings"].values() for r in v]
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, cwd=Path(__file__).resolve().parents[1],
                             timeout=20).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                    text=True, cwd=Path(__file__).resolve().parents[1],
                                    timeout=30).stdout.strip())
    except Exception:                                  # noqa: BLE001
        rev, dirty = "", True
    return {
        "run_id": run.name,
        "code_revision": rev,
        "working_tree_dirty": dirty,
        "authorization": "RUN_AUTHORIZATION.json",
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        # NOT one sentence about "the delegation path". Two modes in one corpus is a fact
        # a reader must be able to see, and `homogeneous` is False the moment it is true.
        "delegation": delegation.summarise(sidecars),
        "readings_total": sum(r["readings_total"] for r in rows),
        "papers": rows,
    }


def main(argv: list[str]) -> int:
    run = Path(argv[1] if len(argv) > 1 else "runs_final_2026-09-16")
    manifest = build(run)
    out = run / "RUN_MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    d = manifest["delegation"]
    print(f"{out}: {manifest['readings_total']} reading(s) over {len(manifest['papers'])} paper(s)")
    print(f"  revision  : {manifest['code_revision'][:12]} "
          f"{'(DIRTY)' if manifest['working_tree_dirty'] else '(clean)'}")
    print(f"  delegation: {d.get('by_mode')} homogeneous={d.get('homogeneous')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
