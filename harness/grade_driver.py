"""S2.5 autonomy: grade one candidate finding by shelling out to a blinded reviewer.

Mirrors `audit_driver.py` on everything that matters for provenance and safety —
staging before promotion, a kept `.rejected.txt` on failure, a `.driver.json` sidecar
recording who wrote it, tree-kill on timeout — and differs on exactly what blinding
requires: `--allowedTools ""` (no tools at all, not even `Read` — the grader must not
be able to open `audit/<lens>.json` and read the severity it was not given) and NO
`--add-dir` (no filesystem access outside an empty sandbox cwd, so there is nothing to
add a directory TO).

`_kill_tree` is reused from `audit_driver` rather than re-implemented — one instance of
carefully-tested process-tree-kill logic, not two that could drift apart.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import failures, state
from .artifacts import Grade
from .audit_driver import NonRetryable, RateLimited, _kill_tree, _reset_hint
from .config import Config


class GradeDriverError(RuntimeError):
    """The configured grader did not produce a usable grade.

    Self-classifying, exactly as `audit_driver.AuditDriverError` is and for the same
    reason. NOT a subclass of it: the grading path is optional and must never be able to
    block a report, so an `except AuditDriverError` in the audit path must not
    accidentally swallow a grading failure or vice versa.
    """

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = failures.classify(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


def grade_error(message: str) -> GradeDriverError | RateLimited | NonRetryable:
    """The right class for one grader failure. Mirrors `audit_driver.driver_error`.

    The two `later`/`never` classes are shared with the audit path deliberately: the
    controller's retry accounting keys on them, and a rate limit is the same fact about
    the world whichever subprocess met it.
    """
    kind, retry, hint = failures.classify(message)
    if retry == "later":
        return RateLimited(message, kind=kind, retry=retry, reset_hint=hint)
    if retry == "never":
        return NonRetryable(message, kind=kind, retry=retry, reset_hint=hint)
    return GradeDriverError(message, kind=kind, retry=retry, reset_hint=hint)


def default_cmd() -> str:
    """The built-in grader invocation: zero tools, piped prompt, redirected stdout —
    see the module docstring for why this differs from `audit_driver.default_cmd`."""
    exe = shutil.which("claude")
    if not exe:
        return ""
    reader = "type" if os.name == "nt" else "cat"
    return f'{reader} "{{prompt}}" | "{exe}" -p --output-format text --allowedTools "" > "{{out}}"'


def resolve_cmd(cfg: Config) -> str:
    return cfg.grade_cmd.strip() or default_cmd()


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_grading:
        return False, ("grading gate is closed; set SH_ALLOW_GRADING=1 (or pass "
                       "--auto-grade) to let the controller independently grade findings")
    cmd = resolve_cmd(cfg)
    if not cmd:
        return False, ("no grader is available: SH_GRADE_CMD is empty and the `claude` CLI "
                       "is not on PATH, so there is nothing to delegate grading to")
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, (f"SH_GRADE_CMD must contain both {{prompt}} and {{out}} placeholders; "
                       f"got: {cfg.grade_cmd!r}")
    return True, ""


def parse_grade_json(text: str) -> Grade:
    """A `Grade` from the command's output, or `GradeDriverError` explaining why not.

    Tolerant of prose/fences wrapping the JSON, like `audit_driver.parse_lens_json` —
    same reviewer, same habit. Not tolerant of a missing `verdict`: a grade with no
    verdict at all is not a grade that happened.
    """
    raw = (text or "").strip()
    if not raw:
        raise GradeDriverError("the command wrote an empty file")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise GradeDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise GradeDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise GradeDriverError("output JSON is not an object")
    if not str(data.get("verdict") or "").strip():
        raise GradeDriverError("output JSON has no 'verdict'")
    try:
        return Grade(**data)
    except Exception as e:
        raise GradeDriverError(f"output does not match the grade schema: {e}") from e


def run_candidate(cfg: Config, pid: str, slug: str, prompt: Path, out: Path, *,
             withheld: list[str]) -> dict:
    """Run the configured grader for one candidate. Returns the sidecar record; raises
    on failure. Same staging/promotion discipline as `audit_driver.run_lens` — see that
    function's docstring for why the output is never redirected straight at `out`."""
    ok, why = available(cfg)
    if not ok:
        raise GradeDriverError(why)
    if not prompt.exists():
        raise GradeDriverError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    staged = out.with_suffix(".staged")
    rejected = out.with_name(f"{slug}.rejected.txt")
    for p_ in (staged, rejected):
        p_.unlink(missing_ok=True)

    cmd = resolve_cmd(cfg).replace("{prompt}", str(prompt)).replace("{out}", str(staged))
    started = time.time()
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True})
    sandbox = Path(tempfile.mkdtemp(prefix=f"sh-grade-{slug}-"))
    try:
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", **group_kwargs)
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise GradeDriverError(f"could not start the command: {e}") from e
        try:
            stdout, stderr = proc.communicate(timeout=cfg.grade_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            staged.unlink(missing_ok=True)
            raise GradeDriverError(f"timed out after {cfg.grade_timeout_s}s") from None
        p = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

        if not staged.exists():
            tail = (p.stderr or p.stdout or "").strip()[-300:]
            err = grade_error(tail or f"the command exited {p.returncode} with no output")
            if getattr(err, "kind", "unknown") != "unknown":
                raise err
            raise GradeDriverError(
                f"the command exited {p.returncode} without writing {out.name}"
                + (f" — {tail}" if tail else ""))

        raw = staged.read_text(encoding="utf-8")
        try:
            grade = parse_grade_json(raw)
        except GradeDriverError:
            classified = grade_error(raw.strip()[:300])
            if getattr(classified, "retry", "now") in ("later", "never"):
                staged.replace(rejected)
                raise classified from None
            staged.replace(rejected)
            raise
        staged.unlink(missing_ok=True)
        state.write_json(out, grade.model_dump())

        record = {
            "slug": slug, "paper_id": pid, "command": cmd, "returncode": p.returncode,
            "seconds": round(time.time() - started, 1), "verdict": grade.verdict,
            "written_by": "grade_driver", "withheld": withheld,
            "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state.write_json(out.with_suffix(".driver.json"), record)
        return record
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def fill(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str]) -> dict:
    """Attempt every pending candidate. Mirrors `audit_driver.fill` exactly, including
    the retry-policy buckets — a `later` or `never` failure must not consume a grading
    retry any more than it should consume an audit one."""
    filled, failed, rate_limited, blocked, kinds = [], {}, {}, {}, {}
    grade_dir = state.project_dir(cfg, pid) / "audit" / "grade"
    withheld = ["severity", "severity_rationale", "lens", "other_findings",
               "prior_grades", "verdict_thresholds", "derivation_table"]
    for slug in awaiting:
        prompt = Path(prompts[slug])
        try:
            rec = run_candidate(cfg, pid, slug, prompt, grade_dir / f"{slug}.json",
                                withheld=withheld)
            filled.append(slug)
            state.append_log(cfg, pid, artifact_type="grade_auto", phase="grade",
                             headers={"slug": slug, "verdict": rec["verdict"],
                                      "seconds": rec["seconds"]},
                             path=str(grade_dir / f"{slug}.json"))
        except (GradeDriverError, RateLimited, NonRetryable) as e:
            kind = getattr(e, "kind", "unknown")
            retry = getattr(e, "retry", "now")
            kinds[slug] = {"kind": kind, "retry": retry,
                           "reset_hint": getattr(e, "reset_hint", "")}
            {"later": rate_limited, "never": blocked}.get(retry, failed)[slug] = str(e)
    return {"filled": filled, "failed": failed, "rate_limited": rate_limited,
            "blocked": blocked, "kinds": kinds}


if __name__ == "__main__":       # self-check: python -m harness.grade_driver
    import tempfile as _tf

    cfg = Config.load()
    closed = Config(allow_grading=False, grade_cmd="x {prompt} {out}")
    assert available(closed)[0] is False, "the gate must be closed by default"
    open_gate = Config(allow_grading=True, grade_cmd="")
    if default_cmd():
        assert available(open_gate)[0] is True, available(open_gate)[1]
        assert '--allowedTools ""' in default_cmd(), "the grader must have zero tools"
        assert "--add-dir" not in default_cmd(), "the grader must have no filesystem access"
    bad = Config(allow_grading=True, grade_cmd="review --in {prompt}")
    assert available(bad)[0] is False, "a template without {out} must be refused"

    body = '```json\n{"verdict":"CONFIRMED","severity":"MAJOR","confidence":"HIGH"}\n```'
    g = parse_grade_json(body)
    assert g.verdict == "CONFIRMED" and g.severity == "MAJOR"
    for bad_text, expect in (("", "empty file"), ("no json", "no JSON object"),
                             ('{"nope":1}', "no 'verdict'")):
        try:
            parse_grade_json(bad_text)
            raise AssertionError(f"should have rejected {bad_text!r}")
        except GradeDriverError as e:
            assert expect in str(e), (bad_text, str(e))

    with _tf.TemporaryDirectory() as td:
        prompt = Path(td) / "c-01.md"
        prompt.write_text("prompt", encoding="utf-8")
        silent = Config(allow_grading=True, grade_cmd="python -c \"pass\" {prompt} {out}")
        try:
            run_candidate(silent, "p", "c-01", prompt, Path(td) / "c-01.json", withheld=[])
            raise AssertionError("a command writing no output must raise")
        except GradeDriverError as e:
            assert "without writing" in str(e), str(e)

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_grading}, indent=2))
