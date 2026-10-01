"""Re-decide finished script checks from their recorded runs under the current kernel: no model,
no container, nothing written into a project. `python tools/replay.py <projects dir> [--out f.json]`.

Per RELEASED_DATA / RECONSTRUCTION check that ended by reconciliation: its checkpointed seeds
(seeds.jsonl) are folded by the very function a new poll uses (execute.reuse_checkpoints) and decided
by the very function a finished check uses (execute._reconciled), so this tool cannot drift from the
harness. `fresh` re-derives, from each seed's own recorded stdout (execution.jsonl, same script sha and
seed), what the kernel now judges by: per-run detail, the result schema and the readings' cohorts. A
seed whose stdout is gone keeps what its row recorded (a row without detail is evidence missing, never
identical runs). Also the harness's own draft run (smoke) of that script. A blocker, a refusal or an
operator stop stays as recorded.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import execute, independence, report  # noqa: E402


def replay(cdir: Path) -> dict | None:
    o, c = (json.loads((cdir / f).read_text(encoding="utf-8")) if (cdir / f).exists() else None
            for f in ("outcome.json", "check.json"))
    if not o or not c or c.get("kind") not in ("RELEASED_DATA", "RECONSTRUCTION"):
        return None
    base = {"check": c["id"], "kind": c["kind"], "old_status": o["status"],
            "old_state": report._state(cdir.parent.parent, c["id"], o)}
    if o["status"] in ("BLOCKED", "NOT_CHECKABLE") or str(o.get("reason", "")).startswith("infrastructure failure"):
        return {**base, "replayed": False, "why": "ended by a blocker, a refusal or a stop: kept as recorded"}
    st: dict = {}
    rows = execute.reuse_checkpoints(cdir.parent.parent, cdir, c, st, fresh=True)
    log = cdir.parent.parent / "execution.jsonl"
    recs = [r for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())
            if r.get("target") == c["id"] and r.get("script_sha256") == c.get("script_sha256")] if log.exists() else []
    script = cdir / "script.py"
    rng = independence.seed_flow(script.read_text(encoding="utf-8")) if script.exists() else None
    smoke = [execute.result_schema(r, c) for r in recs if r.get("mode") == "try" and r.get("smoke")
             and not execute.classify(r)["failed"]]
    det = c["kind"] == "RELEASED_DATA" or c.get("stochastic") is False
    new = execute._reconciled(c, st, True, "", failure="")
    ex = {"runs_planned": int(c.get("runs") or 1), "runs_ended": len(rows), "runs_exited_ok": st.get("ok_runs", 0),
          "runs_failed": sorted(st.get("failed_seeds") or {}, key=int), "schema_defects": st.get("schema_defects") or []}
    tables = [new] + list((new.get("readings") or {}).values())     # every reading's own stages, not the first one's
    ident = sum(1 for t in tables for p in (t.get("stages") or {}).values() if p.get("n_independent") == 1) + sum(
        1 for t in tables if t.get("n_independent") == 1)
    out = {**base, "replayed": True, "new_status": new["status"], "new_reason": new["reason"][:300],
           "new_state": report._state(cdir.parent.parent, c["id"], {**new, "values": st.get("values", []), "execution": ex}),
           "execution": ex, "smoke_schema_defects": smoke, "identical_stages": ident,
           "stages": len(new.get("stages") or {}), "seed_reaches_rng": rng,
           "stage_statuses": _counts(new.get("stages") or {}), "old_stage_statuses": _counts(o.get("stages") or {}),
           **({"readings_differ_in": new["readings_differ_in"]} if new.get("readings_differ_in") else {})}
    if c["kind"] == "RECONSTRUCTION" and out["identical_stages"] and not det:
        # What the same records would decide had the author declared the pipeline deterministic.
        alt = execute._reconciled({**c, "stochastic": False}, st, True, "", failure="")
        out["if_declared_deterministic"] = {"status": alt["status"], "status_on_completed": alt.get("status_on_completed"),
                                            "reason": alt["reason"][:300]}
    return out


def _counts(stages: dict) -> dict:
    out: dict = {}
    for p in stages.values():
        out[p["status"]] = out.get(p["status"], 0) + 1
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
                     + (f"; stages {r['old_stage_statuses']} -> {r['stage_statuses']}" if r["stage_statuses"] else "")
                     if r["replayed"] else f" kept: {r['why']}"))


if __name__ == "__main__":
    main(sys.argv[1:])
