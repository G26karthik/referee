"""tools/make_release_zip.py — deterministic builder for the final source release.

Produces, in `dist/` (relative to the repo root):

    REFEREE_final_source.zip
    REFEREE_final_source.manifest.json
    REFEREE_final_source.sha256

Re-running this script against an unchanged working tree reproduces a
byte-identical `.zip` (same member set, same order, same per-entry metadata,
same compression settings) — the manifest's own `generated_at_utc` field is
the one intentionally-varying byte in the output set.

WHAT GOES IN is an ALLOW LIST, not a deny list: only the top-level entries
named in `TOP_LEVEL_DIRS` / `TOP_LEVEL_FILES` / `README_GLOB` /
`CLAUDE_LAUNCH_JSON`, plus a filtered `manuscript/`, are ever considered.
Everything else at the repo root (`papers/`, `projects/`, `reports/`,
`runs_*/`, `tmp/`, stray `*.zip` archives, `db/` backups, ...) is excluded by
omission — it was never on the allow list, so no separate exclusion rule is
needed for it.

Run:  PYTHONUTF8=1 python tools/make_release_zip.py
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
ARCHIVE_STEM = "REFEREE_final_source"

# A fixed per-entry timestamp so two builds of the same tree hash identically.
# zipfile needs a 6-tuple with year >= 1980; the value itself is arbitrary.
FIXED_ZIP_DATE = (2026, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = (0o644 & 0xFFFF) << 16  # regular file, rw-r--r--, on every host OS

# ---------------------------------------------------------------------------
# Allow list — top level
# ---------------------------------------------------------------------------

TOP_LEVEL_DIRS = ["harness", "tests", "tools", "docs"]

TOP_LEVEL_FILES = [
    "run.py",
    "pytest.ini",
    "CLAUDE.md",
    "HANDOFF.md",
    "AGENTS.md",
    "requirements.txt",
    ".env.sandbox.example",
    ".gitattributes",
    ".gitignore",
    "ponytail-enterprise-scale.mdc",
]

README_GLOB = "README*"
CLAUDE_LAUNCH_JSON = Path(".claude/launch.json")

# ---------------------------------------------------------------------------
# Generic exclusions applied inside every included directory tree
# ---------------------------------------------------------------------------

EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".venv",
    "venv",
    "env",
}

EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_FILE_NAMES = {".DS_Store"}


def _is_real_dotenv(name: str) -> bool:
    """A real `.env*` file — the `.env.sandbox.example` template is not one."""
    return (name == ".env" or name.startswith(".env.")) and name != ".env.sandbox.example"


# Sub-directories of an otherwise-included top-level dir that are pruned by
# name rather than by content filter. `docs/handoff_snapshot_*` is a raw
# `git status` + `git diff` dump taken mid-development (12 MB, dominated by a
# `working_tree.patch` that embeds rows of `data/exports/*.csv`) — a run
# artefact that happens to sit under `docs/`, not curated documentation, and
# its CSV-derived noise is exactly what defeats a naive secret grep (e.g. a
# paper titled "Mask-to-Correct" contributes a `sk-` hit).
EXTRA_PRUNE_SUBDIRS = {
    "docs": ["handoff_snapshot_*"],
}


def _is_cloned_checkout_or_cache(rel_posix: str) -> bool:
    """Defense in depth: cloned external repos and literature caches, wherever
    they occur, even though `runs_*/` and `projects/` are already off the
    top-level allow list."""
    if fnmatch.fnmatch(rel_posix, "*runs*/*/repo") or fnmatch.fnmatch(rel_posix, "*runs*/*/repo/*"):
        return True
    if rel_posix.endswith("/literature/cache") or "/literature/cache/" in rel_posix:
        return True
    return False


def generic_file_allowed(rel_path: Path) -> bool:
    name = rel_path.name
    if name in EXCLUDE_FILE_NAMES:
        return False
    if rel_path.suffix in EXCLUDE_FILE_SUFFIXES:
        return False
    if _is_real_dotenv(name):
        return False
    if _is_cloned_checkout_or_cache(rel_path.as_posix()):
        return False
    return True


def walk_allowed(base_dir: Path, prune_globs: list[str] | None = None):
    """Yield paths under base_dir, pruning excluded directory names (and, at
    the top level only, any name matching `prune_globs`), in sorted order at
    every level for determinism."""
    prune_globs = prune_globs or []
    for entry in sorted(base_dir.iterdir(), key=lambda p: p.name):
        if entry.is_dir():
            if entry.name in EXCLUDE_DIR_NAMES:
                continue
            if any(fnmatch.fnmatch(entry.name, g) for g in prune_globs):
                continue
            yield from walk_allowed(entry)
        elif entry.is_file():
            yield entry


# ---------------------------------------------------------------------------
# manuscript/ — SOURCE only: .tex, .bib, .py (generator/build/check scripts —
# none of them are build ARTEFACTS, which is the only thing this filter is
# asked to keep out), and the generated .tex tables. LaTeX build byproducts
# are excluded by suffix; the two carve-outs are manuscript/figures/*.pdf and
# a standalone manuscript/fig*.pdf, which are deliverables, not artefacts of
# a build a referee would re-run.
# ---------------------------------------------------------------------------

LATEX_BUILD_SUFFIXES = {".aux", ".log", ".out", ".bbl", ".blg", ".bak"}
SOURCE_SUFFIXES = {".tex", ".bib", ".py"}


def _manuscript_pdf_is_deliverable(rel_to_manuscript: Path) -> bool:
    parts = rel_to_manuscript.parts
    if len(parts) >= 2 and parts[0] == "figures" and rel_to_manuscript.suffix == ".pdf":
        return True
    if len(parts) == 1 and rel_to_manuscript.name.startswith("fig") and rel_to_manuscript.suffix == ".pdf":
        return True
    return False


def manuscript_file_allowed(rel_to_manuscript: Path) -> bool:
    name = rel_to_manuscript.name
    if name.endswith(".synctex.gz"):
        return False
    suffix = rel_to_manuscript.suffix
    if suffix == ".pdf":
        return _manuscript_pdf_is_deliverable(rel_to_manuscript)
    if suffix in LATEX_BUILD_SUFFIXES:
        return False
    if suffix in SOURCE_SUFFIXES:
        return True
    # Everything else under manuscript/ (.md notes, .json/.txt data dumps,
    # .png renders, stray dotfiles, ...) is not one of the four named source
    # categories, so it is excluded rather than guessed about.
    return False


# ---------------------------------------------------------------------------
# Secret scan
# ---------------------------------------------------------------------------

# The brief names eight literal markers to grep for: `sk-`, `ANTHROPIC_API_KEY`,
# `OPENAI_API_KEY`, `MODAL_TOKEN`, `AWS_`, `password`, `Bearer `, `-----BEGIN`.
# A literal substring grep on those words alone is unusably noisy over prose
# and identifier names — "task-specific", "desk-rejection", "risk-sensitive"
# and "Mask-to-Correct" (a paper title) all contain the substring `sk-`, every
# reference to the *name* `MODAL_TOKEN_ID`/`ANTHROPIC_API_KEY` in docs, error
# strings and `os.environ.get(...)` calls contains that marker without
# containing a value, and "password-gated checkpoint" is prose about a paper,
# not a credential. Confirmed by running the literal grep first (see the
# build history): every one of those was a false positive, and 194 of the
# ~210 total hits came from a single non-source file
# (`docs/handoff_snapshot_*/working_tree.patch`, pruned above) whose CSV-diff
# content is exactly the kind of large corpus text that collides with a
# three-character substring by chance.
#
# So each marker keeps its literal meaning but is anchored to the SHAPE of an
# actual secret: a key-prefixed random token, a PEM header, or an
# `NAME = "value"` / `NAME: "value"` assignment — not a bare mention of the
# name. `-----BEGIN` needs no shape refinement; any occurrence is a PEM block.
SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("PEM key/cert block", re.compile(r"-----BEGIN")),
    ("key-prefixed secret token (sk-...)", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_]{20,}")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}")),
    (
        "credential-shaped assignment",
        re.compile(
            r"\b(ANTHROPIC_API_KEY|OPENAI_API_KEY|MODAL_TOKEN_ID|MODAL_TOKEN_SECRET|"
            r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|PASSWORD)"
            r"\s*[:=]\s*[\"']([^\"'\s]{6,})[\"']",
            re.IGNORECASE,
        ),
    ),
]

_PLACEHOLDER_MARKERS = (
    "test", "fake", "dummy", "example", "changeme", "xxx", "your-", "<",
    "not-a-real", "redacted", "placeholder", "sample", "todo", "...",
)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(m in lowered for m in _PLACEHOLDER_MARKERS)


# Binary/media types are not worth scanning as text and cannot embed a
# plausible credential string this scan is looking for.
_SKIP_SCAN_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz"}

# This builder necessarily spells every marker literally (as the patterns
# above and in its own docstring/comments) in order to define the scan, so it
# is its own guaranteed false positive. Exempted by path, not by weakening
# the patterns.
_SELF_EXEMPT_FROM_SCAN = {"tools/make_release_zip.py"}


def scan_file_for_secrets(path: Path) -> list[tuple[int, str, str]]:
    """Return [(line_no, marker, line_text)] for every genuine-shaped hit in
    a text file. See SECRET_PATTERNS' comment for why this is shape-aware
    rather than a bare substring grep."""
    if path.suffix.lower() in _SKIP_SCAN_SUFFIXES:
        return []
    if path.resolve().relative_to(REPO_ROOT).as_posix() in _SELF_EXEMPT_FROM_SCAN:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            if label == "credential-shaped assignment" and _looks_like_placeholder(m.group(2)):
                continue
            hits.append((lineno, label, line.strip()[:200]))
    return hits


# ---------------------------------------------------------------------------
# Collect the file set
# ---------------------------------------------------------------------------


def collect_files() -> list[Path]:
    """Return the absolute paths of every file to archive, in sorted arcname
    order."""
    absolute: list[Path] = []

    for dirname in TOP_LEVEL_DIRS:
        base = REPO_ROOT / dirname
        if not base.is_dir():
            continue
        for f in walk_allowed(base, EXTRA_PRUNE_SUBDIRS.get(dirname)):
            rel = f.relative_to(REPO_ROOT)
            if generic_file_allowed(rel):
                absolute.append(f)

    for filename in TOP_LEVEL_FILES:
        f = REPO_ROOT / filename
        if f.is_file():
            absolute.append(f)

    for f in sorted(REPO_ROOT.glob(README_GLOB)):
        if f.is_file() and f not in absolute:
            absolute.append(f)

    launch_json = REPO_ROOT / CLAUDE_LAUNCH_JSON
    if launch_json.is_file():
        absolute.append(launch_json)

    manuscript_base = REPO_ROOT / "manuscript"
    if manuscript_base.is_dir():
        for f in walk_allowed(manuscript_base):
            rel = f.relative_to(manuscript_base)
            if manuscript_file_allowed(rel):
                absolute.append(f)

    def arcname(p: Path) -> str:
        return p.relative_to(REPO_ROOT).as_posix()

    absolute = sorted(set(absolute), key=arcname)
    return absolute


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build() -> int:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / f"{ARCHIVE_STEM}.zip"
    manifest_path = DIST_DIR / f"{ARCHIVE_STEM}.manifest.json"
    sha256_path = DIST_DIR / f"{ARCHIVE_STEM}.sha256"

    candidates = collect_files()

    # --- secret scan: exclude, never redact, and say so ---------------------
    excluded_for_secrets: list[str] = []
    kept: list[Path] = []
    for f in candidates:
        rel = f.relative_to(REPO_ROOT).as_posix()
        hits = scan_file_for_secrets(f)
        if hits:
            excluded_for_secrets.append(rel)
            print(f"SECRET SCAN: excluding {rel} — {len(hits)} hit(s):", file=sys.stderr)
            for lineno, marker, line in hits:
                print(f"    line {lineno}: marker {marker!r}: {line}", file=sys.stderr)
        else:
            kept.append(f)

    if not excluded_for_secrets:
        print("Secret scan: no hits across the candidate file set.")
    else:
        print(
            f"Secret scan: excluded {len(excluded_for_secrets)} file(s) with a hit "
            "(see stderr for detail). Nothing was redacted in place.",
            file=sys.stderr,
        )

    def arcname(p: Path) -> str:
        return p.relative_to(REPO_ROOT).as_posix()

    kept = sorted(kept, key=arcname)

    # --- write the zip, one entry per file, sorted, fixed metadata ---------
    manifest_files = []
    total_bytes = 0
    with zipfile.ZipFile(zip_path, mode="w") as zf:
        for f in kept:
            name = arcname(f)
            data = f.read_bytes()
            zinfo = zipfile.ZipInfo(filename=name, date_time=FIXED_ZIP_DATE)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = FIXED_EXTERNAL_ATTR
            zinfo.create_system = 0
            zf.writestr(zinfo, data, compresslevel=9)
            size = len(data)
            total_bytes += size
            manifest_files.append(
                {"path": name, "size": size, "sha256": sha256_of_bytes(data)}
            )

    archive_sha256 = sha256_of(zip_path)

    manifest = {
        "archive": zip_path.name,
        "archive_sha256": archive_sha256,
        "git_revision": git_revision(),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_count": len(manifest_files),
        "total_bytes": total_bytes,
        "excluded_for_secrets": excluded_for_secrets,
        "files": manifest_files,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    sha256_path.write_text(f"{archive_sha256}  {zip_path.name}\n", encoding="utf-8")

    print(f"Wrote {zip_path} ({total_bytes} bytes across {len(manifest_files)} files)")
    print(f"  archive sha256: {archive_sha256}")
    print(f"Wrote {manifest_path}")
    print(f"Wrote {sha256_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(build())
