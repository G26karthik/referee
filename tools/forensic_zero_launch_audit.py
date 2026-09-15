"""Reproduce the diagnostic run's 11-question and 75-target zero-launch audit."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs_final_codex_2026-09-15"
PAPERS = (
    "0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
    "apt-icml", "cvpr", "iclr", "sanchez24a-icml")

D = {
    ("acl", "TGT-REP-P60-155"),
    ("apt-icml", "TGT-CLM-T1r0c5-2"),
    ("apt-icml", "TGT-CLM-T2r2c1"),
    ("apt-icml", "TGT-DAT-T1r0c5"),
}
J = {
    ("cvpr", "TGT-CLM-P14127-170"),
    ("iclr", "TGT-CLM-T17r3c1"),
    ("iclr", "TGT-DAT-T10r0c3"),
    ("iclr", "TGT-DAT-T11r1c1"),
}
K = {
    ("0c06a98d7c818f6f", "TGT-DAT-T1r0c1"),
    ("0c06a98d7c818f6f", "TGT-DAT-T1r0c2"),
    ("0c06a98d7c818f6f", "TGT-DAT-T1r0c3"),
    ("2024-icml-sapg", "TGT-DAT-T1r0c5"),
    ("2024-icml-sapg", "TGT-DAT-T1r10c6"),
    ("2024-icml-sapg", "TGT-DAT-T1r12c6"),
    ("sanchez24a-icml", "TGT-CLM-P17713-852"),
    ("sanchez24a-icml", "TGT-CLM-T5r28c9"),
    ("sanchez24a-icml", "TGT-DAT-T5r28c9"),
}

CLASS = {
    "D": ("alignment/identity", "ARTIFACT",
          "exhaustive author-code alignment recorded no unique legitimate command"),
    "I": ("target selection", "METHOD/HARNESS",
          "the run-owned target cap deferred a warranted target"),
    "J": ("question/target/routing", "METHOD/HARNESS",
          "target minting, identity routing, or cached outcome persistence was defective"),
    "K": ("reconstruction generation", "METHOD/HARNESS",
          "the reconstruction driver was disabled or no durable outcome was retained"),
}


def load(pid: str) -> dict:
    return json.loads((RUN / "projects" / pid / "discovery" / "targets.json").read_text(
        encoding="utf-8"))


def main() -> None:
    question_rows, target_rows = [], []
    for pid in PAPERS:
        ts = load(pid)
        objects = {o["target_id"]: o for o in ts.get("objects", [])}
        current_plans = {}
        for p in ts.get("plans", []):
            current_plans[p["target_id"]] = p
        outcomes = {}
        for o in ts.get("outcomes", []):
            outcomes[o["target_id"]] = o

        warranted = [p for p in current_plans.values() if p.get("requires_execution")]
        for plan in warranted:
            tid = plan["target_id"]
            key = (pid, tid)
            code = "D" if key in D else "J" if key in J else "K" if key in K else "I"
            stage, owner, forensic_reason = CLASS[code]
            obj, out = objects.get(tid, {}), outcomes.get(tid, {})
            target_rows.append({
                "paper_id": pid, "target_id": tid, "question_id": obj.get("question_id", ""),
                "address": (obj.get("ref") or {}).get("ref", ""),
                "route": plan.get("route", ""), "classification": code,
                "first_nonlaunch_stage": stage, "blocker_owner": owner,
                "persisted_disposition": out.get("disposition", "MISSING"),
                "persisted_reason": out.get("reason") or plan.get("reason", ""),
                "forensic_reason": forensic_reason,
            })

        q_by = {q["question_id"]: q for q in ts.get("questions", [])}
        for qid in ts.get("route_exhaustion_question_ids", []):
            q = q_by[qid]
            bound = [o for o in ts.get("objects", []) if o.get("question_id") == qid]
            attempts = [a for a in ts.get("route_attempts", []) if a.get("question_id") == qid]
            route_text = []
            for a in attempts:
                route_text.append({
                    "route": a.get("route"), "attempted": bool(a.get("attempted")),
                    "completed": bool(a.get("completed")),
                    "exhausted": bool(a.get("exhausted")), "state": a.get("state"),
                    "reason": a.get("reason") or a.get("blocker") or "no attempt record",
                })
            owner = ("METHOD/HARNESS" if any(
                a.get("state") in {"NOT_TRIED", "GATE_CLOSED", "DEFERRED_BUDGET",
                                   "DEFERRED_POLICY"} for a in attempts) else "PAPER")
            question_rows.append({
                "paper_id": pid, "question_id": qid,
                "originating_findings": q.get("source_finding_ids") or
                                        ([q.get("from_finding")] if q.get("from_finding") else []),
                "paper_address": (q.get("claim_ref") or {}).get("ref", ""),
                "materiality_basis": sorted({o.get("materiality_basis", "NONE") for o in bound}),
                "why_material": q.get("why_it_matters") or "stored as a CENTRAL question",
                "candidate_targets": [o.get("target_id") for o in bound],
                "routes": route_text, "blocker_owner": owner,
                "fixable_by_referee": owner == "METHOD/HARNESS",
                "next_legitimate_route": next((a["route"] for a in route_text
                                                if not a["exhausted"]), "none"),
                "final_question_state": ("EXHAUSTED" if route_text and
                                          all(a["exhausted"] for a in route_text)
                                          else "OPEN"),
            })

    assert len(question_rows) == 11, len(question_rows)
    assert len(target_rows) == 75, len(target_rows)
    counts = {c: sum(r["classification"] == c for r in target_rows) for c in "ABCDEFGHIJKL"}
    assert counts == {"A": 0, "B": 0, "C": 0, "D": 4, "E": 0, "F": 0,
                      "G": 0, "H": 0, "I": 58, "J": 4, "K": 9, "L": 0}, counts

    result = {"diagnostic_run": RUN.name, "material_questions": question_rows,
              "warranted_targets": target_rows, "classification_counts": counts}
    out = RUN / "reports" / "forensic_zero_launch_audit.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = ["# Forensic audit of the diagnostic zero-launch run", "",
             "This report is about `runs_final_codex_2026-09-15` only.", "",
             "## A. All 11 material questions", "",
             "| Paper | Question | Finding(s) | Address | Materiality basis | Why material | Targets | Applicable route results | Owner | Fixable | Next route | Final |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in question_rows:
        routes = "<br>".join(
            f"{a['route']}: attempted={a['attempted']}, completed={a['completed']}, "
            f"exhausted={a['exhausted']}, {a['state']}; {a['reason']}" for a in r["routes"])
        cells = [r["paper_id"], r["question_id"], ", ".join(r["originating_findings"]) or "none",
                 r["paper_address"], ", ".join(r["materiality_basis"]), r["why_material"],
                 ", ".join(r["candidate_targets"]), routes, r["blocker_owner"],
                 str(r["fixable_by_referee"]), r["next_legitimate_route"],
                 r["final_question_state"]]
        lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in cells) + " |")
    lines += ["", "## B. All 75 experiment-warranted targets", "",
              "Counts conserve: " + ", ".join(f"{k}={v}" for k, v in counts.items()) + ".", "",
              "| Paper | Target | Question | Address | Route | Class | First stop | Owner | Persisted state | Reason |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in target_rows:
        cells = [r[k] for k in ("paper_id", "target_id", "question_id", "address", "route",
                                "classification", "first_nonlaunch_stage", "blocker_owner",
                                "persisted_disposition", "forensic_reason")]
        lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in cells) + " |")
    lines += ["", "## C. Root cause", "",
              "The 75-to-zero funnel is an interaction of 58 run-owned budget deferrals, "
              "nine disabled reconstruction paths, four planner/routing defects, and four "
              "author-code identity boundaries. Docker was available, but the diagnostic "
              "configuration selected the local backend and left repository and reconstruction "
              "gates closed. No target reached an authorized process launch.", ""]
    (ROOT / "docs" / "FORENSIC_ZERO_LAUNCH_AUDIT.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print(json.dumps({"questions": len(question_rows), "targets": len(target_rows),
                      "counts": counts, "json": str(out)}, indent=2))


if __name__ == "__main__":
    main()
