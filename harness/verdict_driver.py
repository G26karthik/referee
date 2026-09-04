"""One model-written, whole-paper opinion per review — see `harness/prompts/verdict.py`
for what it is and is not. Deliberately the lightest of the three reviewer drivers: a
single best-effort call, never retried, never gating a report. If it fails for any
reason the report is written exactly as if grading had produced nothing — `verdict` and
`verdict_reason` (the deterministic ones) are entirely unaffected either way.
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

from . import state
from .artifacts import SubstantiveVerdict
from .audit_driver import _kill_tree
from .config import Config


class VerdictDriverError(RuntimeError):
    pass


def default_cmd() -> str:
    exe = shutil.which("claude")
    if not exe:
        return ""
    reader = "type" if os.name == "nt" else "cat"
    return f'{reader} "{{prompt}}" | "{exe}" -p --output-format text --allowedTools "" > "{{out}}"'


def resolve_cmd(cfg: Config) -> str:
    return cfg.verdict_cmd.strip() or default_cmd()


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_substantive_verdict:
        return False, "substantive-verdict gate is closed; set SH_ALLOW_SUBSTANTIVE_VERDICT=1"
    cmd = resolve_cmd(cfg)
    if not cmd:
        return False, "no reviewer available for the substantive verdict"
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, "SH_VERDICT_CMD must contain both {prompt} and {out}"
    return True, ""


def parse_verdict_json(text: str) -> SubstantiveVerdict:
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise VerdictDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise VerdictDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("verdict") or "").strip():
        raise VerdictDriverError("output JSON has no 'verdict'")
    try:
        return SubstantiveVerdict(**data)
    except Exception as e:
        raise VerdictDriverError(f"output does not match the schema: {e}") from e


def accept_verdict(cfg: Config, pid: str, raw: str, *, reader: str = "",
                   tool_policy: str = "unrecorded") -> dict:
    """Seal a whole-paper read produced OUTSIDE this module's subprocess.

    Completes the set: `stages.audit.accept_lens` and `stages.grade.accept_grade` already
    give the audit and grading phases a channel that does not depend on a CLI being
    reachable. The whole-paper read had none at all — `run_report` called `run()` or got
    nothing — so on a host where the CLI is rate-limited or absent, every report was
    permanently missing the one judgement the threshold table structurally cannot make,
    and the self-audit's `whole_paper_judged_independently` failed for a reason that had
    nothing to do with the review's diligence.

    Sealed the same way, and for the same reason: an opinion nobody can attribute is
    worth less than no opinion. Sits at `reports/<pid>.substantive.json` with a sidecar;
    `load_accepted` is what `run_report` prefers over calling the subprocess.
    """
    verdict = parse_verdict_json(raw)
    out = state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"
    state.write_json(out, verdict.model_dump())
    record = {"paper_id": pid, "written_by": "manual_accept", "reader": reader or "unnamed",
              "tool_policy": tool_policy, "verdict": verdict.verdict,
              "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def load_accepted(cfg: Config, pid: str) -> SubstantiveVerdict | None:
    """A sealed whole-paper read for `pid`, or None. Verifies the seal before trusting it —
    a file edited after acceptance is not the file that was accepted, exactly as
    `lens_is_accepted` and `grade_is_accepted` require."""
    out = state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"
    sidecar = out.with_suffix(".driver.json")
    if not (out.exists() and sidecar.exists()):
        return None
    try:
        rec = state.read_json(sidecar)
        if not isinstance(rec, dict) or rec.get("written_by") != "manual_accept":
            return None
        if hashlib.sha256(out.read_bytes()).hexdigest() != rec.get("content_sha256"):
            return None
        return SubstantiveVerdict(**state.read_json(out))
    except Exception:
        return None


def run(cfg: Config, prompt_text: str, *, timeout_s: int = 300) -> SubstantiveVerdict | None:
    """Best-effort. Returns None on ANY failure — an unavailable, timed-out, or
    malformed response degrades to no opinion, never a placeholder one."""
    ok, _why = available(cfg)
    if not ok:
        return None
    with tempfile.TemporaryDirectory(prefix="sh-verdict-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        cmd = resolve_cmd(cfg).replace("{prompt}", str(prompt)).replace("{out}", str(out))
        group_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})
        sandbox = Path(tempfile.mkdtemp(prefix="sh-verdict-sandbox-"))
        try:
            try:
                proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", **group_kwargs)
            except OSError:
                return None
            try:
                stdout, _stderr = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.communicate()
                return None
            if not out.exists():
                return None
            try:
                return parse_verdict_json(out.read_text(encoding="utf-8"))
            except VerdictDriverError:
                return None
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":       # self-check: python -m harness.verdict_driver
    cfg = Config.load()
    closed = Config(allow_substantive_verdict=False)
    assert available(closed)[0] is False
    good = ('{"verdict":"SOUND_WITH_MINOR_CONCERNS","reason":"r",'
            '"strongest_contribution":"c","weakest_link":"w","weaknesses_are":"LOCAL"}')
    v = parse_verdict_json(good)
    assert v.verdict == "SOUND_WITH_MINOR_CONCERNS" and v.weaknesses_are == "LOCAL"
    for bad in ("", "no json", '{"nope":1}'):
        try:
            parse_verdict_json(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except VerdictDriverError:
            pass
    assert run(closed, "prompt") is None, "gate closed must degrade to None, never raise"
    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_substantive_verdict}, indent=2))
