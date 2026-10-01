"""REFEREE CLI. The workflow (.claude/workflows/referee.js) uses `tasks` and `seal`;
workers writing a check script use `try`; `exec` and `env` poll to completion by hand.

  python run.py tasks <paper.pdf|paper-id> [--json] [--wait SECONDS]
  python run.py seal <paper-id> <task-id> <answer.json>
  python run.py try <paper-id> <gen-task-id> <script.py>
  python run.py discover <paper-id> "<dataset name>" [--registry zenodo|datacite|huggingface|huggingface-models]
  python run.py discover <paper-id> --files <record url>   # the files of a cited repository record
  python run.py reopen <paper-id> <check> <why>             # a check that ended without a finding, after a harness fix
  python run.py env <paper-id>          # build the authors' environment (started by the harness)
  python run.py stop <paper-id> <check> <why>   # the operator ends a running check
  python run.py status [<paper-id>]
  python run.py pack <out.zip> [<paper-id> ...] [--clean]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path

from harness import discover, execute, report, state, tasks

HEAVY = ("repo", "env", "env-extra", "script-env")      # clones, venvs, datasets: rebuilt on demand, never packed


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
    dv = sub.add_parser("discover")
    dv.add_argument("pid"), dv.add_argument("query", nargs="?", default=""), dv.add_argument("--registry", default="")
    dv.add_argument("--files", default="", help="list the files of a cited repository record (names, sizes, checksums)")
    ro = sub.add_parser("reopen")
    ro.add_argument("pid"), ro.add_argument("check"), ro.add_argument("why")
    e = sub.add_parser("exec")
    e.add_argument("pid"), e.add_argument("check")
    sub.add_parser("env").add_argument("pid")
    so = sub.add_parser("stop")
    so.add_argument("pid"), so.add_argument("check"), so.add_argument("why")
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
    elif a.cmd == "discover":         # public registries only; grants no code, no clone, no execution
        print(json.dumps(discover.files(cfg, a.pid, a.files) if a.files else discover.search(cfg, a.pid, a.query, a.registry),
                         indent=1, ensure_ascii=False))
    elif a.cmd == "reopen":           # a check that ended without a finding, after its cause was fixed
        print(json.dumps(tasks.reopen(cfg, a.pid, a.check, a.why)))
    elif a.cmd == "exec":             # poll one started check to its end (the harness polls it anyway)
        while execute.poll(cfg, a.pid, a.check):
            time.sleep(10)
        print(json.dumps(state.read_json(state.pdir(cfg, a.pid) / "checks" / a.check / "outcome.json")))
    elif a.cmd == "stop":             # the operator ends a running check (recorded, never a finding)
        print(json.dumps({k: v for k, v in execute.stop(cfg, a.pid, a.check, a.why).items() if k != "values"}))
    elif a.cmd == "env":
        while (env := execute.author_env(cfg, state.pdir(cfg, a.pid))) is None:
            time.sleep(10)
        print(json.dumps(env))
    elif a.cmd == "status":
        for d in sorted(cfg.projects.glob(f"{a.pid or '*'}/ledger.json")) or []:
            led = state.read_json(d)
            print(f"{d.parent.name}: {led['scientific_status']} — "
                  + ", ".join(f"{c['id']} {c['status']}" for c in led["checks"]))
            if led.get("completion"):
                print("  completion: " + report._completion_line(led["completion"]))
    elif a.cmd == "pack":
        return pack(cfg, Path(a.out), a.pids, a.clean)
    return 0


def _size(f: Path) -> int:
    """A regular file's size; 0 for links and for entries Windows cannot stat (a container's
    Linux symlinks are reparse points that os.stat refuses)."""
    try:
        st = f.lstat()
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def _heavy(rel: Path) -> bool:
    """Clones, venvs, and each author-code run's working copy of the checkout (checks/<id>/work)."""
    return rel.parts[0] in HEAVY or (rel.parts[:1] == ("checks",) and rel.parts[2:3] == ("work",))


def _writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)   # git's object files are read-only on Windows
    func(path)


def pack(cfg: state.Config, out: Path, pids: list[str], clean: bool) -> int:
    """Zip the review artifacts (never clones/venvs/data), verify the zip, then optionally
    delete the heavy resources. Nothing is deleted unless the zip tested clean."""
    roots = [state.pdir(cfg, p) for p in pids] or [d for d in cfg.projects.iterdir() if (d / "paper").is_dir()]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root in roots:
            for f in root.rglob("*"):
                rel = f.relative_to(root)
                # heavy first: a container venv's symlinks cannot even be stat'ed on Windows
                if not _heavy(rel) and f.name != ".lock" and f.is_file():
                    z.write(f, Path(root.name) / rel)
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            print(f"zip failed its integrity test; nothing deleted: {out}")
            return 1
        n = len(z.namelist())
    freed, left = 0, []
    if clean:
        heavy = [root / h for root in roots for h in HEAVY] + [w for root in roots for w in root.glob("checks/*/work")]
        envs = [root / "env" for root in roots] + [d for root in roots for d in root.glob("env-extra/*")] + (
            [cfg.projects / ".script-env"] if not pids else [])
        for e in envs:                    # the venvs themselves live in Docker named volumes
            execute._docker(["docker", "volume", "rm", "-f", execute.volume(e)], 120)
        ckpts = [execute.ckpt_volume(c.parent, k) for root in roots for c in root.glob("checks/*/check.json")
                 for k in range(int((state.read_json(c) or {}).get("runs") or 1))] + [
            (state.read_json(d / "data.json") or {}).get("volume") or execute.data_volume(root, d.name)
            for root in roots for d in root.glob("checks/*") if (d / "data.json").exists()]
        for v in ckpts + ([execute.DOWNLOAD_CACHE] if not pids else []):
            execute._docker(["docker", "volume", "rm", "-f", v], 120)
        for h in heavy + ([cfg.projects / ".script-env"] if not pids else []):
            if h.exists():
                freed += sum(_size(f) for f in h.rglob("*"))
                try:
                    shutil.rmtree(h, onexc=_writable)
                except OSError:
                    left.append(str(h))
    print(json.dumps({"zip": str(out), "files": n, "bytes": out.stat().st_size, "freed_bytes": freed,
                      "not_deleted": left}))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
