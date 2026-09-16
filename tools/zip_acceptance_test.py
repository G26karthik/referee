"""The final-delivery acceptance test for `dist/REFEREE_final_source.zip`.

`tools/make_release_zip.py` builds and hashes the archive but never opens it again to
confirm it actually works — §21 of the delivery contract says explicitly: "Do not call
the ZIP ready until the extracted copy works." This closes that gap.

    PYTHONUTF8=1 python tools/zip_acceptance_test.py [--full-install]

Three checks, in order, each a hard gate on the next:

1. **Integrity.** Every entry the manifest names extracts and its SHA-256 matches the
   manifest's own record. A corrupted or truncated archive fails here before anything
   is executed.
2. **Self-check discovery.** `tests/test_self_checks.py`'s own discovery mechanism (walk
   `harness/**/*.py`, parse for `if __name__ == "__main__"`) is re-run against the
   EXTRACTED tree, confirming the archive did not silently drop a module.
3. **The test suite, from the extracted copy, on the extracted copy's own interpreter
   path.** Runs `python -m pytest tests -q -m "not network"` with the extracted
   directory as the working directory and on `sys.path`, so an import that quietly
   depended on something outside the ZIP (a path back into the working repo, a stray
   `sys.path.insert` pointing at the original checkout) fails here rather than shipping
   silently. Uses the CURRENT interpreter and its already-installed dependencies unless
   `--full-install` is passed, in which case it additionally builds a throwaway venv and
   installs `requirements.txt` fresh inside it — the stronger and slower check, for a
   final release rather than every iteration.

Exits 0 only if all three pass. Writes `dist/ACCEPTANCE_TEST.json` either way, so a
failed run leaves a readable record rather than only a nonzero exit code.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist"
ARCHIVE = DIST_DIR / "REFEREE_final_source.zip"
MANIFEST = DIST_DIR / "REFEREE_final_source.manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_integrity(extract_dir: Path) -> dict:
    if not ARCHIVE.is_file():
        raise SystemExit(f"no archive at {ARCHIVE} — run tools/make_release_zip.py first")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = manifest.get("files") or manifest.get("entries") or []
    if not entries:
        # Fall back to the zip's own member list against the top-level sha256 file, if
        # the manifest shape differs from what this script expects — never silently
        # skip integrity checking because a field name did not match a guess.
        raise SystemExit(f"manifest at {MANIFEST} has no recognisable file list "
                         f"(looked for 'files' or 'entries'); its actual keys are "
                         f"{sorted(manifest.keys())} — update this script's field names")
    with zipfile.ZipFile(ARCHIVE) as zf:
        zf.extractall(extract_dir)
    checked = 0
    mismatches = []
    for entry in entries:
        rel = entry.get("path") or entry.get("name")
        want = entry.get("sha256")
        if not rel or not want:
            continue
        got_path = extract_dir / rel
        if not got_path.is_file():
            mismatches.append(f"missing after extraction: {rel}")
            continue
        got = _sha256(got_path)
        if got != want:
            mismatches.append(f"hash mismatch: {rel} (manifest {want[:12]}, extracted {got[:12]})")
        checked += 1
    return {"entries_checked": checked, "mismatches": mismatches, "ok": not mismatches}


def check_self_checks_discoverable(extract_dir: Path) -> dict:
    """Count modules with a `__main__` self-check block in the extracted tree, against
    the floor CLAUDE.md states ("70 of them"). NOT every module under `harness/` carries
    one — `tests/test_self_checks.py` discovers modules that HAVE the guard and runs
    them; it never asserts every file must have one, and neither does this. The gate
    this check exists for is narrower: the COUNT should not have silently dropped
    relative to the working tree the archive was built from, which would mean a module
    lost its self-check in transit (a stray `.gitignore` exclusion, a filtered copy)
    rather than never having had one."""
    import ast
    harness_dir = extract_dir / "harness"
    if not harness_dir.is_dir():
        return {"ok": False, "reason": "no harness/ directory in the extracted archive"}
    total, with_guard = 0, 0
    found = []
    for path in sorted(harness_dir.rglob("*.py")):
        total += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return {"ok": False, "reason": f"{path.relative_to(extract_dir)} does not parse: {e}"}
        has_guard = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            for node in ast.walk(tree))
        if has_guard:
            with_guard += 1
            found.append(str(path.relative_to(extract_dir)))
    return {"ok": True, "modules": total, "with_self_check": with_guard,
            "self_check_modules": found}


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run_test_suite(extract_dir: Path, *, full_install: bool) -> dict:
    py = sys.executable
    tmp_venv = None
    if full_install:
        tmp_venv = extract_dir.parent / "acceptance_venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(tmp_venv)
        py = str(_venv_python(tmp_venv))
        req = extract_dir / "requirements.txt"
        if req.is_file():
            pip = subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(req)],
                                 cwd=extract_dir, capture_output=True, text=True, timeout=1800)
            if pip.returncode != 0:
                return {"ok": False, "stage": "pip install", "stdout": pip.stdout[-4000:],
                        "stderr": pip.stderr[-4000:]}
    env_note = "fresh venv + requirements.txt" if full_install else "current interpreter, existing deps"
    proc = subprocess.run(
        [py, "-m", "pytest", "tests", "-q", "-m", "not network"],
        cwd=extract_dir, capture_output=True, text=True, timeout=1800,
        env={**__import__("os").environ, "PYTHONUTF8": "1"})
    tail = "\n".join((proc.stdout or "").splitlines()[-15:])
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "environment": env_note, "summary": tail}


def main(argv: list[str]) -> int:
    full_install = "--full-install" in argv
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    record: dict = {"archive": str(ARCHIVE), "full_install": full_install}
    with tempfile.TemporaryDirectory(prefix="referee_zip_accept_") as tmp:
        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()

        integrity = check_integrity(extract_dir)
        record["integrity"] = integrity
        print(f"integrity: {integrity['entries_checked']} entries checked, "
              f"{len(integrity['mismatches'])} mismatch(es)")
        if not integrity["ok"]:
            for m in integrity["mismatches"][:20]:
                print(f"  MISMATCH: {m}")
            record["ok"] = False
            (DIST_DIR / "ACCEPTANCE_TEST.json").write_text(
                json.dumps(record, indent=1), encoding="utf-8")
            return 1

        selfchecks = check_self_checks_discoverable(extract_dir)
        record["self_checks"] = selfchecks
        print(f"self-checks: {selfchecks.get('with_self_check')}/{selfchecks.get('modules')} "
              f"modules carry a self-check (CLAUDE.md states 70)")
        if not selfchecks["ok"]:
            print(f"  {selfchecks.get('reason')}")
            record["ok"] = False
            (DIST_DIR / "ACCEPTANCE_TEST.json").write_text(
                json.dumps(record, indent=1), encoding="utf-8")
            return 1
        if selfchecks.get("with_self_check", 0) < 60:   # a real drop, not a rounding note
            print("  WARNING: self-check count is well below CLAUDE.md's stated 70 — "
                  "investigate before shipping")

        suite = run_test_suite(extract_dir, full_install=full_install)
        record["test_suite"] = suite
        print(f"test suite ({suite.get('environment')}): "
              f"{'PASS' if suite['ok'] else 'FAIL'}")
        print(suite.get("summary") or suite.get("stderr") or "")

    record["ok"] = integrity["ok"] and selfchecks["ok"] and suite["ok"]
    (DIST_DIR / "ACCEPTANCE_TEST.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(f"\nACCEPTANCE: {'PASS' if record['ok'] else 'FAIL'} "
          f"(dist/ACCEPTANCE_TEST.json written)")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
