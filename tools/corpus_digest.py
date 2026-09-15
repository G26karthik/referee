"""A REPRODUCIBLE digest of the pre-fix corpus artifacts, so immutability is checkable.

    python tools/corpus_digest.py

The eight-paper results under `projects/` are historical experimental evidence and no
step of this work may rewrite them. Two things were being used to establish that and only
one of them is worth anything:

  * `git status` on `projects/` — VACUOUS. `.gitignore` line 4 ignores `projects/`
    outright, so that directory can never show as modified whatever happens to it. A check
    that cannot fail is not a check.
  * a digest — real, and only as good as its recipe. Checkpoint A printed a value and did
    not record how it was computed, so the number could not be recomputed later and could
    not be compared against. This file is that recipe, fixed and re-runnable.

The digest covers every reader-facing and machine-facing result the corpus run produced:
the reports, the ledgers and the target sets of the eight papers, hashed over both the
path and the bytes so a rename is as visible as an edit.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.config import BASE_DIR                                  # noqa: E402

CORPUS = ("0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
          "apt-icml", "cvpr", "iclr", "sanchez24a-icml")

PATTERNS = ("reports/*.json", "reports/*.md", "discovery/targets.json",
            "runs/*/probe_results.json")


def files() -> list[Path]:
    root = BASE_DIR / "projects"
    out: list[Path] = []
    for pid in CORPUS:
        for pattern in PATTERNS:
            out += sorted((root / pid).glob(pattern))
    return sorted(out)


def digest() -> tuple[str, int]:
    h = hashlib.sha256()
    paths = files()
    for p in paths:
        h.update(p.relative_to(BASE_DIR).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), len(paths)


def main() -> int:
    d, n = digest()
    print(f"pre-fix corpus digest (sha256, {n} files): {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
