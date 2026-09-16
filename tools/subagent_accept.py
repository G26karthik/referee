"""Seal a lens reading produced by an isolated subagent of the controlling session.

`run.py accept` seals a WHOLE-paper lens file and nothing else. A paper that does not
fit one pass is read as N bounded parts plus one synthesis per lens, and those units are
sealed by `audit_driver.run_lens` — which spawns a CLI subprocess. So the only way to
read a long paper without that subprocess was to side-write the part file, which
`unit_is_accepted` correctly refuses: no sidecar, no provenance, no prompt pinning.

This is that gate for a part and a synthesis, through the SAME validation
(`audit_driver.parse_lens_json`) and the SAME seal (`content_sha256` over the written
bytes, `prompt_sha256` over the prompt that produced it). What it may claim is decided
by `harness.delegation`, not here: SESSION_SUBAGENT reports real context isolation and
`tool_policy: unrecorded`, because this session did not build the delegate's argv and
cannot prove what it could reach.

    python tools/subagent_accept.py pending <paper-id> [...]
    python tools/subagent_accept.py seal <paper-id> <unit-id> <staged-file>

`unit-id` is what `AuditUnit.unit_id` prints: `overclaim` for a whole-paper reading,
`overclaim/part-02` or `overclaim/synthesis` for a split one.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import audit_driver, delegation, state  # noqa: E402
from harness.artifacts import PaperDoc  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.stages import audit as audit_stage  # noqa: E402

REVIEWER = "desktop session subagent (Agent tool), one isolated context per reading"


def units(cfg: Config, pid: str) -> list[audit_stage.AuditUnit]:
    root = state.project_dir(cfg, pid)
    doc = PaperDoc(**state.read_json(root / "paper" / "doc.json"))
    return audit_stage.units_for(root, audit_stage.LENSES, audit_stage.plan_for(doc))


def pending(cfg: Config, pid: str) -> list[dict]:
    """Every reading this paper still needs, with the prompt that asks for it.

    A synthesis whose parts are not all sealed is reported as BLOCKED rather than
    pending: `run_audit` refuses to render its prompt until they are, so dispatching it
    would be asking a question whose input does not exist yet.
    """
    all_units = units(cfg, pid)
    sealed = {u.unit_id for u in all_units if audit_stage.unit_is_accepted(u)[0]}
    out = []
    for u in all_units:
        if u.unit_id in sealed:
            continue
        parts = [p for p in all_units if p.lens == u.lens and p.kind == "part"]
        blocked = (u.kind == "synthesis"
                   and not all(p.unit_id in sealed for p in parts))
        out.append({"unit_id": u.unit_id, "lens": u.lens, "kind": u.kind,
                    "prompt": str(u.prompt_path),
                    "prompt_exists": u.prompt_path.is_file(),
                    "out": str(u.out_path),
                    "state": "BLOCKED_ON_PARTS" if blocked else
                             ("PENDING" if u.prompt_path.is_file() else "NO_PROMPT_YET")})
    return out


def seal(cfg: Config, pid: str, unit_id: str, staged: Path) -> dict:
    """Validate a staged reading and write it plus its provenance sidecar."""
    match = [u for u in units(cfg, pid) if u.unit_id == unit_id]
    if not match:
        raise SystemExit(f"{pid}: no such unit {unit_id!r}; run `pending` to list them")
    unit = match[0]
    raw = staged.read_text(encoding="utf-8")
    report = audit_driver.parse_lens_json(raw, unit.lens)   # the one validation path
    unit.out_path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(unit.out_path, report.model_dump())
    # The raw response is kept beside the accepted one for the same reason `run_lens`
    # keeps its own: a reader tracing a finding must be able to see what was returned
    # before this harness touched it.
    raw_path = unit.out_path.with_suffix(".raw.txt")
    raw_path.write_text(raw, encoding="utf-8")
    record = {
        "lens": unit.lens, "unit_id": unit.unit_id, "paper_id": pid,
        **delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=REVIEWER),
        "controller": "claude code desktop (Claude Opus 5 session)",
        "model_requested": "sonnet",
        "authorization": "RUN_AUTHORIZATION.json",
        "content_sha256": hashlib.sha256(unit.out_path.read_bytes()).hexdigest(),
        "prompt_sha256": audit_driver.prompt_fingerprint(unit.prompt_path),
        "prompt_path": str(unit.prompt_path),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "raw_response": raw_path.name,
        "findings": len(report.findings),
        "ts": state.now(),
    }
    state.write_json(unit.sidecar_path, record)
    ok, why = audit_stage.unit_is_accepted(unit)
    if not ok:
        raise SystemExit(f"{pid}/{unit_id}: sealed and still refused: {why}")
    return record


STAGE_DIR = ".staged_subagent"


def sweep(cfg: Config, pid: str) -> list[dict]:
    """Seal every staged reading for one paper, and consume what it sealed.

    A staged file that survives sealing gets re-sealed on the next sweep, rewriting a
    sealed artifact's sidecar with a fresh timestamp and making a re-run look like new
    work — the same reason `audit_driver.run_lens` unlinks its own staging file after
    promotion. The promoted copy is the record.

    The filename carries the unit: `<lens>.json` is a whole-paper reading and
    `<lens>__part-02.json` / `<lens>__synthesis.json` are the split ones.
    """
    root = state.project_dir(cfg, pid) / "audit" / STAGE_DIR
    out = []
    for src in sorted(root.glob("*.json")) if root.is_dir() else []:
        unit_id = src.stem.replace("__", "/")
        try:
            rec = seal(cfg, pid, unit_id, src)
        except SystemExit as e:
            out.append({"unit_id": unit_id, "error": str(e)})
            continue
        except Exception as e:                        # noqa: BLE001 — kept, never deleted
            src.replace(src.with_suffix(".rejected.txt"))
            out.append({"unit_id": unit_id, "error": f"{type(e).__name__}: {e}"})
            continue
        src.unlink(missing_ok=True)
        out.append(rec)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    cfg = Config.load()
    if argv[0] == "sweep":
        bad = 0
        for pid in argv[1:]:
            for rec in sweep(cfg, pid):
                if "error" in rec:
                    bad += 1
                    print(f"REFUSED  {pid}/{rec['unit_id']}: {rec['error'][:160]}")
                else:
                    print(f"sealed   {pid}/{rec['unit_id']}: {rec['findings']} finding(s)")
        return 1 if bad else 0
    if argv[0] == "pending":
        rows = {pid: pending(cfg, pid) for pid in argv[1:]}
        print(json.dumps(rows, indent=1))
        return 0
    if argv[0] == "seal":
        rec = seal(cfg, argv[1], argv[2], Path(argv[3]))
        print(f"{argv[1]}/{argv[2]}: {rec['findings']} finding(s) "
              f"sha={rec['content_sha256'][:12]} mode={rec['delegation_mode']}")
        return 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
