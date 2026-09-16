"""SHA256SUMS over a run directory, in the same flat `sha256  path` format the archived
v2 run carries at its root, so the two are comparable the same way.

    python tools/final_run_sha256sums.py [run_dir]
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1] if len(argv) > 1 else "runs_final_2026-09-16")
    out = run_dir / "SHA256SUMS"
    lines = []
    skipped = []
    for p in sorted(run_dir.rglob("*")):
        try:
            is_file = p.is_file()
        except OSError:
            # A symlink or reparse point inside a staged container/venv build (e.g.
            # `env/bin/python`) that this host cannot stat directly. Not a source
            # artifact this manifest is for; recorded rather than silently dropped.
            skipped.append(str(p.relative_to(run_dir)))
            continue
        if is_file and p != out:
            try:
                lines.append(f"{sha(p)}  {p.relative_to(run_dir).as_posix()}")
            except OSError:
                skipped.append(str(p.relative_to(run_dir)))
    if skipped:
        print(f"skipped {len(skipped)} unreadable path(s) (symlinks/reparse points in "
              f"staged env/container builds): {skipped[:5]}{' ...' if len(skipped) > 5 else ''}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(lines)} files hashed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
