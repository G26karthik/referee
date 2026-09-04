"""One model-written, whole-paper opinion per review — see `harness/prompts/verdict.py`
for what it is and is not. Deliberately the lightest of the three reviewer drivers: a
single best-effort call, never retried, never gating a report. If it fails for any
reason the report is written exactly as if grading had produced nothing — `verdict` and
`verdict_reason` (the deterministic ones) are entirely unaffected either way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
