"""Optional S2 autonomy: fill lens JSON by shelling out to a configured reviewer.

This exists because "run the whole pipeline without stopping" is a reasonable thing to
want, and the honest way to provide it is to make the delegation explicit rather than to
pretend S2 was never a judgement stage.

What this module does NOT do is invent an auditor. The harness has no model of its own —
no API key, no cloud SDK, by design — so autonomy here means running whatever command the
operator puts in `SH_AUDIT_CMD` and treating its output as a candidate lens report. Three
consequences follow, and all three are enforced below rather than documented and hoped for:

  1. The gate is off by default (`SH_ALLOW_AUTO_AUDIT`). Running an arbitrary configured
     command as part of `review` is a per-invocation decision, like the S3 exec gates.
  2. Every lens filled this way leaves `audit/<lens>.driver.json` recording the command,
     the exit code and the timestamp. A finding that reaches a report should always be
     traceable to who wrote it.
  3. Output is parsed and structurally validated here, and then re-verified downstream by
     `stages/audit.load_reports` exactly like a human-written file — quotes checked against
     the parsed PDF, cell citations checked against the cell. An auto-filled lens gets no
     benefit of the doubt that a hand-written one would not get.

If the command fails, writes nothing, or writes something that is not a lens report, that
lens stays pending. A failed auto-audit degrades to the normal manual pause; it never
writes a placeholder, because an empty findings list is indistinguishable from "this
paper is clean" once it reaches the report.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import state
from .artifacts import LensReport
from .config import Config


class AuditDriverError(RuntimeError):
    """The configured command did not produce a usable lens report."""


def available(cfg: Config) -> tuple[bool, str]:
    """Whether auto-audit can run, and if not, the reason to show the operator."""
    if not cfg.allow_auto_audit:
        return False, ("auto-audit gate is closed; set SH_ALLOW_AUTO_AUDIT=1 to let "
                       "`review` fill lenses by running SH_AUDIT_CMD")
    if not cfg.audit_cmd.strip():
        return False, ("SH_ALLOW_AUTO_AUDIT is set but SH_AUDIT_CMD is empty; there is no "
                       "reviewer to delegate to")
    if "{prompt}" not in cfg.audit_cmd or "{out}" not in cfg.audit_cmd:
        return False, ("SH_AUDIT_CMD must contain both {prompt} and {out} placeholders; "
                       f"got: {cfg.audit_cmd!r}")
    return True, ""


def parse_lens_json(text: str, lens: str) -> LensReport:
    """A LensReport from the command's output file, or AuditDriverError explaining why not.

    Tolerant of one thing only: a JSON object wrapped in prose or a fenced code block,
    because a chat-shaped reviewer will often produce that. Not tolerant of a missing
    `findings` key — a report with no findings list is not a report that found nothing,
    it is a report that did not happen.
    """
    raw = (text or "").strip()
    if not raw:
        raise AuditDriverError("the command wrote an empty file")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AuditDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AuditDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AuditDriverError("output JSON is not an object")
    if "findings" not in data:
        raise AuditDriverError("output JSON has no 'findings' key")
    if not isinstance(data["findings"], list):
        raise AuditDriverError("'findings' is not a list")
    data["lens"] = lens
    try:
        return LensReport(**data)
    except Exception as e:                       # pydantic validation, shape errors
        raise AuditDriverError(f"output does not match the lens schema: {e}") from e


def run_lens(cfg: Config, pid: str, lens: str, prompt: Path, out: Path) -> dict:
    """Run the configured reviewer for one lens. Returns a record; raises on failure."""
    ok, why = available(cfg)
    if not ok:
        raise AuditDriverError(why)
    if not prompt.exists():
        raise AuditDriverError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()                             # never let a stale file look like success

    cmd = cfg.audit_cmd.replace("{prompt}", str(prompt)).replace("{out}", str(out))
    started = time.time()
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=cfg.audit_timeout_s)
    except subprocess.TimeoutExpired as e:
        raise AuditDriverError(f"timed out after {cfg.audit_timeout_s}s") from e
    except OSError as e:
        raise AuditDriverError(f"could not start the command: {e}") from e

    if not out.exists():
        tail = (p.stderr or p.stdout or "").strip()[-300:]
        raise AuditDriverError(
            f"the command exited {p.returncode} without writing {out.name}"
            + (f" — {tail}" if tail else ""))

    report = parse_lens_json(out.read_text(encoding="utf-8"), lens)
    # Rewrite normalised: downstream reads this file, so what is on disk should be exactly
    # what was validated, not whatever prose the command happened to wrap it in.
    state.write_json(out, report.model_dump())

    record = {
        "lens": lens, "paper_id": pid, "command": cmd, "returncode": p.returncode,
        "seconds": round(time.time() - started, 1), "findings": len(report.findings),
        "written_by": "audit_driver", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def fill(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str]) -> dict:
    """Attempt every pending lens. Partial success is success for the lenses that worked."""
    filled, failed = [], {}
    audit_dir = state.project_dir(cfg, pid) / "audit"
    for lens in awaiting:
        prompt = Path(prompts[lens])
        try:
            rec = run_lens(cfg, pid, lens, prompt, audit_dir / f"{lens}.json")
            filled.append(lens)
            state.append_log(cfg, pid, artifact_type="audit_auto", phase="audit",
                             headers={"lens": lens, "findings": rec["findings"],
                                      "seconds": rec["seconds"]},
                             path=str(audit_dir / f"{lens}.json"))
        except AuditDriverError as e:
            failed[lens] = str(e)
    return {"filled": filled, "failed": failed}


if __name__ == "__main__":       # self-check: python -m harness.audit_driver
    import tempfile

    cfg = Config.load()

    # --- gate semantics ---------------------------------------------------------------
    closed = Config(allow_auto_audit=False, audit_cmd="x {prompt} {out}")
    assert available(closed)[0] is False, "the gate must be closed by default"
    no_cmd = Config(allow_auto_audit=True, audit_cmd="")
    assert available(no_cmd)[0] is False and "SH_AUDIT_CMD is empty" in available(no_cmd)[1]
    bad = Config(allow_auto_audit=True, audit_cmd="review --in {prompt}")
    assert available(bad)[0] is False, "a template without {out} must be refused"
    good = Config(allow_auto_audit=True, audit_cmd="cp {prompt} {out}")
    assert available(good) == (True, "")

    # --- parsing ----------------------------------------------------------------------
    body = ('Here you go:\n```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
            '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
            '"evidence_ref":"p1"}],"unasked_question":"u","notes":"n"}\n```')
    rep = parse_lens_json(body, "overclaim")
    assert rep.lens == "overclaim" and len(rep.findings) == 1
    assert rep.findings[0].severity == "MAJOR"

    for bad_text, expect in (("", "empty file"), ("no json here", "no JSON object"),
                             ("{nope}", "not valid JSON"), ('{"lens":"x"}', "no 'findings' key"),
                             ('{"findings":{}}', "not a list")):
        try:
            parse_lens_json(bad_text, "overclaim")
            raise AssertionError(f"should have rejected {bad_text!r}")
        except AuditDriverError as e:
            assert expect in str(e), (bad_text, str(e))

    # --- a command that writes nothing must fail, not produce an empty lens ------------
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("prompt", encoding="utf-8")
        silent = Config(projects_dir=Path(td), allow_auto_audit=True,
                        audit_cmd="python -c \"pass\" {prompt} {out}")
        try:
            run_lens(silent, "p", "overclaim", prompt, Path(td) / "overclaim.json")
            raise AssertionError("a command writing no output must raise")
        except AuditDriverError as e:
            assert "without writing" in str(e), str(e)

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_auto_audit}, indent=2))
