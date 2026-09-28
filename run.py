"""REFEREE CLI. The workflow (.claude/workflows/referee.js) uses `tasks` and `seal`;
workers writing a check script use `try`; `exec` is started by the harness itself.

  python run.py tasks <paper.pdf|paper-id> [--json] [--wait SECONDS]
  python run.py seal <paper-id> <task-id> <answer.json>
  python run.py try <paper-id> <gen-task-id> <script.py>
  python run.py status [<paper-id>]
  python run.py pack <out.zip> [<paper-id> ...] [--clean]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

from harness import execute, state, tasks

HEAVY = ("repo", "env", "script-env")      # clones, venvs, datasets: rebuilt on demand, never packed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="referee")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tasks")
    t.add_argument("source")
    t.add_argument("--json", action="store_true")
    t.add_argument("--wait", type=int, default=0)
    s = sub.add_parser("seal")
    s.add_argument("pid"), s.add_argument("task"), s.add_argument("file")
    tr = sub.add_parser("try")
    tr.add_argument("pid"), tr.add_argument("task"), tr.add_argument("script")
    e = sub.add_parser("exec")
    e.add_argument("pid"), e.add_argument("check")
    st = sub.add_parser("status")
    st.add_argument("pid", nargs="?")
    p = sub.add_parser("pack")
    p.add_argument("out"), p.add_argument("pids", nargs="*"), p.add_argument("--clean", action="store_true")
    a = ap.parse_args(argv)
    cfg = state.Config()

    if a.cmd == "tasks":
        res = tasks.advance(cfg, a.source, a.wait)
        print(json.dumps(res, indent=None if a.json else 2, ensure_ascii=False))
    elif a.cmd == "seal":
        try:
            print(json.dumps(tasks.seal(cfg, a.pid, a.task, a.file)))
        except (tasks.SealError, FileNotFoundError) as err:
            print(f"SEAL REFUSED: {err}")
            return 2
    elif a.cmd == "try":
        print(json.dumps(tasks.try_(cfg, a.pid, a.task, a.script), indent=1))
    elif a.cmd == "exec":
        check = state.read_json(state.pdir(cfg, a.pid) / "checks" / a.check / "check.json")
        print(json.dumps({k: v for k, v in execute.execute(cfg, a.pid, check).items() if k != "values"}))
    elif a.cmd == "status":
        for d in sorted(cfg.projects.glob(f"{a.pid or '*'}/ledger.json")) or []:
            led = state.read_json(d)
            print(f"{d.parent.name}: {led['scientific_status']} — "
                  + ", ".join(f"{c['id']} {c['status']}" for c in led["checks"]))
        if a.pid:
            print(json.dumps(tasks.advance(cfg, a.pid), indent=1)[:3000])
    elif a.cmd == "pack":
        return pack(cfg, Path(a.out), a.pids, a.clean)
    return 0


def pack(cfg: state.Config, out: Path, pids: list[str], clean: bool) -> int:
    """Zip the review artifacts (never clones/venvs/data), verify the zip, then optionally
    delete the heavy resources. Nothing is deleted unless the zip tested clean."""
    roots = [state.pdir(cfg, p) for p in pids] or [d for d in cfg.projects.iterdir() if (d / "paper").is_dir()]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root in roots:
            for f in root.rglob("*"):
                rel = f.relative_to(root)
                if f.is_file() and rel.parts[0] not in HEAVY and f.name != ".lock":
                    z.write(f, Path(root.name) / rel)
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            print(f"zip failed its integrity test; nothing deleted: {out}")
            return 1
        n = len(z.namelist())
    freed = 0
    if clean:
        heavy = [root / h for root in roots for h in HEAVY] + ([cfg.projects / ".script-env"] if not pids else [])
        for h in heavy:
            if h.exists():
                freed += sum(f.stat().st_size for f in h.rglob("*") if f.is_file() and not f.is_symlink())
                shutil.rmtree(h, ignore_errors=True)
    print(json.dumps({"zip": str(out), "files": n, "bytes": out.stat().st_size, "freed_bytes": freed}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
