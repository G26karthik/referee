"""The material-question route-exhaustion table the manuscript's `j_routes` reads.

`python tools/final_run_material_routes.py [run_dir]` writes
`<run_dir>/reports/material_question_routes.md`.

Every row is read off `discovery/targets.json`'s own `route_attempts` and
`route_exhaustion_question_ids` — the exact denominator `harness/exhaustion.py` computes
for route-exhaustion coverage — and nothing here re-derives that denominator or
re-classifies a route outcome. A question qualifies for this table because
`_material_question_ids` already put it in `route_exhaustion_question_ids`; this script
only renders what is on disk.

This replaces an equivalent report from an earlier run that was never checked into this
repository as a script — a gap of the same shape CLAUDE.md documents for
`harness/alignment/trial.py` and (as of this run) `harness/artifact_review_driver.py`:
a real capability whose OUTPUT existed but whose PRODUCTION was not reproducible. This
script closes that gap for this one report.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.exhaustion import (  # noqa: E402
    CONFIGURATION_STATES, DISCHARGING_STATES)

PAPERS = ["0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
          "apt-icml", "cvpr", "iclr", "sanchez24a-icml"]

# route, state, blocker -> the human phrase this table prints, matching the register the
# manuscript already uses ("paper check: completed inconclusive", "author code: identity
# blocked", ...). Every combination actually observed on this corpus is named here;
# anything unnamed raises rather than printing a guess.
_PHRASE = {
    ("PAPER_INTERNAL_CHECK", "COMPLETED_INCONCLUSIVE", ""):
        "paper check: completed inconclusive",
    ("AUTHOR_CODE_EXECUTION", "DISCHARGED_BLOCKED", "IDENTITY_BLOCKED"):
        "author code: identity blocked",
    ("INDEPENDENT_RECONSTRUCTION", "GATE_CLOSED", "AUTHORIZATION_BLOCKED"):
        "reconstruction: gate closed (SH_ALLOW_REIMPLEMENTATION_DRIVER not open)",
    ("INDEPENDENT_RECONSTRUCTION", "NOT_TRIED", ""):
        "reconstruction: not tried",
    ("AUTHOR_CODE_EXECUTION", "GATE_CLOSED", "AUTHORIZATION_BLOCKED"):
        "author code: gate closed (SH_ALLOW_REPO_EXEC path not authorized for this target)",
    ("AUTHOR_CODE_EXECUTION", "NOT_TRIED", ""):
        "author code: not tried",
    ("FOCUSED_VALIDATION_EXPERIMENT", "NOT_TRIED", ""):
        "focused validation: not tried",
    ("FOCUSED_VALIDATION_EXPERIMENT", "DISCHARGED_BLOCKED", "SPECIFICATION_BLOCKED"):
        "focused validation: specification blocked",
    ("INDEPENDENT_RECONSTRUCTION", "COMPLETED_INCONCLUSIVE", ""):
        "reconstruction: attempted, completed inconclusive (environment limitation)",
}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001 — a missing file is a fact
        return None


def rows_for(run_dir: Path, pid: str) -> list[dict]:
    d = _load(run_dir / "projects" / pid / "discovery" / "targets.json") or {}
    q_by_id = {q["question_id"]: q for q in (d.get("questions") or [])}
    exhaustion_ids = d.get("route_exhaustion_question_ids") or []
    by_question: dict[str, list] = {}
    for a in (d.get("route_attempts") or []):
        by_question.setdefault(a["question_id"], []).append(a)

    out = []
    for qid in exhaustion_ids:
        q = q_by_id.get(qid) or {}
        # ALL rows for this question, including a never-attempted (`NOT_TRIED`) one —
        # matching `harness.exhaustion.coverage` exactly. Filtering to `attempted` rows
        # first, as an earlier version of this script did, silently dropped questions
        # whose only route was never tried and mis-classified questions with a mix of a
        # discharged row and a not-tried row as fully exhausted. Corpus-wide that
        # produced 30/40 exhausted; the harness's own `route_exhaustion.exhausted` is 22.
        rows = by_question.get(qid, [])
        states = [str(a.get("state", "")) for a in rows]
        if states and all(s in DISCHARGING_STATES for s in states):
            bucket = "exhausted"
        elif any(s in CONFIGURATION_STATES for s in states):
            bucket = "open_because_of_this_harness"
        else:
            bucket = "open_because_untried"
        phrases = []
        for a in rows:
            key = (a.get("route", ""), a.get("state", ""), a.get("blocker", "") or "")
            phrase = _PHRASE.get(key)
            if phrase is None:
                raise SystemExit(f"unrecognised route outcome for {pid}/{qid}: {key!r} "
                                 "— add it to _PHRASE rather than guessing")
            phrases.append(phrase)
        final = {
            "exhausted": "routes exhausted, unresolved",
            "open_because_of_this_harness": "open — blocked by this harness's own configuration",
            "open_because_untried": "open — not attempted",
        }[bucket]
        out.append({
            "paper": pid, "question_id": qid,
            "finding": ", ".join(q.get("source_finding_ids") or [q.get("from_finding", "")]),
            "address": (q.get("claim_ref") or {}).get("ref", ""),
            "targets": ", ".join(q.get("evidence_refs") or []) or "n/a",
            "outcomes": "; ".join(phrases) if phrases else "no route attached",
            "bucket": bucket, "final": final,
        })
    return out


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1] if len(argv) > 1 else "runs_final_2026-09-16")
    all_rows: list[dict] = []
    for pid in PAPERS:
        all_rows.extend(rows_for(run_dir, pid))

    lines = [
        "# Material-question route exhaustion",
        "",
        f"All {len(all_rows)} material questions this run's route-exhaustion accounting "
        "covers are retained below, in the same three-way split "
        "`harness.exhaustion.coverage` reports: EXHAUSTED (every applicable route "
        "discharged), OPEN BECAUSE OF THIS HARNESS (a route was gated, budgeted or "
        "policy-deferred — ours to fix by opening a gate, not evidence about the paper), "
        "and OPEN BECAUSE UNTRIED (nobody attempted the route). `Completed inconclusive` "
        "means the implemented paper-internal check reached its boundary without "
        "settling the scientific question. `Identity blocked` and `specification "
        "blocked` are exhausted external or scientific boundaries, not established "
        "paper failures.",
        "",
        "| Paper | Question | Finding | Address | Target(s) | Applicable implemented "
        "route outcomes | Final state |",
        "|---|---|---|---|---|---|---|",
    ]
    counts = {"exhausted": 0, "open_because_of_this_harness": 0, "open_because_untried": 0}
    for r in all_rows:
        lines.append(
            f"| `{r['paper']}` | `{r['question_id']}` | {r['finding']} | "
            f"`{r['address']}` | `{r['targets']}` | {r['outcomes']} | {r['final']} |")
        counts[r["bucket"]] += 1
    lines += [
        "",
        f"Totals: {len(all_rows)} material questions; {counts['exhausted']} reached full "
        f"route exhaustion, {counts['open_because_of_this_harness']} are open because of "
        f"this harness's own configuration (a gate, budget or set-level policy), and "
        f"{counts['open_because_untried']} are open because no applicable route was "
        "attempted. No route settled a paper claim, and no route established a material "
        "failure on this corpus.",
        "",
    ]
    out = run_dir / "reports" / "material_question_routes.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}: {len(all_rows)} material question rows, {counts['exhausted']} "
          f"exhausted, {counts['open_because_of_this_harness']} harness-blocked, "
          f"{counts['open_because_untried']} untried")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
