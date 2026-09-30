"""Re-decide finished script checks from their recorded runs under the current kernel: no model,
no container, nothing written into a project. `python tools/replay.py <projects dir> [--out f.json]`.

Per RELEASED_DATA / RECONSTRUCTION check that ended by reconciliation: its checkpointed seeds
(seeds.jsonl), folded as a new poll would reuse them, and the result schema of every recorded
evidence run of the approved script (execution.jsonl, raw stdout); also the harness's own draft
run (smoke) of that script. A blocker, a refusal or an operator stop stays as recorded.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import execute, report  # noqa: E402
from harness.reconcile import reconcile  # noqa: E402


def replay(cdir: Path) -> dict | None:
    o, c = (json.loads((cdir / f).read_text(encoding="utf-8")) if (cdir / f).exists() else None
            for f in ("outcome.json", "check.json"))
    if not o or not c or c.get("kind") not in ("RELEASED_DATA", "RECONSTRUCTION"):
        return None
    base = {"check": c["id"], "kind": c["kind"], "old_status": o["status"],
            "old_state": report._state(cdir.parent.parent, c["id"], o)}
    if o["status"] in ("BLOCKED", "NOT_CHECKABLE") or str(o.get("reason", "")).startswith("infrastructure failure"):
        return {**base, "replayed": False, "why": "ended by a blocker, a refusal or a stop: kept as recorded"}
    st = {"values": [], "staged": [], "failed_seeds": {}, "stage_errors": {}, "ok_runs": 0, "schema_defects": set()}
    rows = execute._checkpoints(cdir, c)
    for row in rows:
        st["values"] += row["values"]
        st["staged"] += row.get("staged") or []
        if row.get("error"):
            st["failed_seeds"][str(row["seed"])] = row["error"]
        st["ok_runs"] += not row.get("error")
        errs, defects = execute.split_units(set(row.get("units") or []), row.get("staged") or [], bool(row.get("error")), "")
        st["stage_errors"].update({s: row["stage_errors"][s] for s in errs if s in (row.get("stage_errors") or {})})
        st["schema_defects"] |= set(defects)
    log = cdir.parent.parent / "execution.jsonl"
    recs = [r for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())
            if r.get("target") == c["id"] and r.get("script_sha256") == c.get("script_sha256")]
    for r in [r for r in recs if r.get("mode") == "evidence"]:
        if not execute.classify(r)["failed"]:
            st["schema_defects"] |= set(execute.result_schema(r, c))
    smoke = [execute.result_schema(r, c) for r in recs if r.get("mode") == "try" and r.get("smoke")
             and not execute.classify(r)["failed"]]
    det = c["kind"] == "RELEASED_DATA" or c.get("stochastic") is False
    args = dict(rel=(c.get("target") or {}).get("relation", ""), staged=st["staged"], failed=st["failed_seeds"],
                stage_errors=st["stage_errors"], test=c.get("test", ""))
    seeded = c["kind"] == "RECONSTRUCTION" and len(rows) > 1
    new = reconcile(c["kind"], c.get("printed", ""), st["values"], "", {}, seeded, True, "", deterministic=det, **args)
    ex = {"runs_planned": int(c.get("runs") or 1), "runs_ended": len(rows), "runs_exited_ok": st["ok_runs"],
          "runs_failed": sorted(st["failed_seeds"], key=int), "schema_defects": sorted(st["schema_defects"])}
    out = {**base, "replayed": True, "new_status": new["status"], "new_reason": new["reason"][:300],
           "new_state": report._state(cdir.parent.parent, c["id"], {**new, "values": st["values"], "execution": ex}),
           "execution": ex, "smoke_schema_defects": smoke,
           "identical_stages": sum(1 for p in (new.get("stages") or {}).values() if p.get("n_independent") == 1)
           + (1 if new.get("n_independent") == 1 else 0),
           "stages": len(new.get("stages") or {})}
    if c["kind"] == "RECONSTRUCTION" and out["identical_stages"] and not det:
        # What the same records would decide had the author declared the pipeline deterministic.
        alt = reconcile(c["kind"], c.get("printed", ""), st["values"], "", {}, seeded, True, "", deterministic=True, **args)
        out["if_declared_deterministic"] = {"status": alt["status"], "status_on_completed": alt.get("status_on_completed"),
                                            "reason": alt["reason"][:300]}
    return out


def main(argv: list[str]) -> None:
    projects, out = Path(argv[0]), argv[argv.index("--out") + 1] if "--out" in argv else ""
    res = {p.name: [r for d in sorted((p / "checks").glob("*"), key=lambda d: (len(d.name), d.name))
                    if (r := replay(d))] for p in sorted(projects.iterdir()) if (p / "checks").is_dir()}
    text = json.dumps(res, indent=1, ensure_ascii=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    for pid, rows in res.items():
        print(pid)
        for r in rows:
            print(f"  {r['check']} {r['kind']}: {r['old_status']} ({r['old_state']})"
                  + (f" -> {r['new_status']} ({r['new_state']})" + (f"; schema defects {len(r['execution']['schema_defects'])}"
                                                                     if r["execution"]["schema_defects"] else "")
                     + (f"; {r['identical_stages']} identical-rerun result(s)" if r["identical_stages"] else "")
                     if r["replayed"] else f" kept: {r['why']}"))


if __name__ == "__main__":
    main(sys.argv[1:])
