"""One model-written, whole-paper opinion per review — see `harness/prompts/verdict.py`
for what it is and is not. Deliberately the lightest of the three reviewer drivers: a
single best-effort call, never retried, never gating a report. If it fails for any
reason the report is written exactly as if grading had produced nothing — `verdict` and
`verdict_reason` (the deterministic ones) are entirely unaffected either way.

WHAT THIS DRIVER DID NOT DO, and now does. `run()` persisted NOTHING: it returned a
`SubstantiveVerdict` in memory that the renderer dropped straight into the report, and
`load_accepted` refused anything whose `written_by` was not `manual_accept`. So a
subprocess-produced whole-paper opinion was:

  - UNATTRIBUTED — no record of the command, the model, the prompt or the time, for the
    only model output in this system that can flip `run.py` to exit 3 via the CONTESTED
    flag. "A reviewer wrote this" with nothing behind it is the shape of claim every
    other artifact in this repository is built to avoid making.
  - RE-BILLED EVERY RUN — `run_report` called `run()` whenever the gate was open and no
    sealed file existed, and no sealed file could ever exist, because only a human
    `accept_verdict` could write one.
  - IRREPRODUCIBLE — the prompt lived in a `TemporaryDirectory` and was deleted when the
    call returned.

It now writes `reports/<pid>.substantive.json` plus a sidecar with
`written_by: "verdict_driver"`, and `load_accepted` accepts both writers. Reuse is pinned
to `prompt_sha256`: a report whose findings changed produces a different prompt, and a
stale opinion about a different set of findings is worse than paying for a fresh one.
"""
from __future__ import annotations

import dataclasses
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
from .audit_driver import (KNOWN_TOOLS, Confinement, _kill_tree, denied_tools,
                           envelope_provenance, operator_confinement, unwrap_envelope,
                           write_pinned_settings)
from .config import Config
from .prompts import verdict as VP

# Every writer a validated path can produce: this module's own subprocess, plus every
# mode `harness.delegation` admits. Read off that vocabulary rather than written out, so a
# mode cannot be sealed by `accept_verdict` and then refused by `load_accepted` — which
# would present as "no whole-paper read exists" for an artifact that does.
WRITERS = ("verdict_driver",) + tuple(
    __import__("harness.delegation", fromlist=["WRITTEN_BY"]).WRITTEN_BY.values())


class VerdictDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    """WHICH model forms the opinion: `SH_VERDICT_MODEL`, else the declared role default."""
    return (cfg.verdict_model or "").strip() or str(VP.ROLE_SPEC.get("model", ""))


def verdict_confinement(cfg: Config, *, settings_sha256: str = "") -> Confinement:
    """Zero tools, no directories, `--bare` — see `prompts.verdict.ROLE_SPEC` for why.

    Not the grader's reason. This reader is shown everything the review produced; the
    point is that everything it saw is IN THE PROMPT, which is on disk, so a human can
    read the same input. A reader that could also open the repository or the web would be
    judging partly from material nobody recorded.
    """
    if cfg.verdict_cmd.strip():
        return operator_confinement("assessor")
    return Confinement(
        role="assessor", allowed_tools=(), disallowed_tools=denied_tools(()),
        add_dir=(), model=role_model(cfg), restricted=True, strict_mcp=True, bare=True,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "") -> str:
    """The built-in assessor invocation, or '' when none can be found.

    Takes a `Config` where it used to take nothing, for the same reason
    `grade_driver.default_cmd` does: without one it emitted no `--model` and the opinion
    came from whatever the operator's ambient settings said, unrecorded.
    """
    cfg = cfg or Config()
    exe = ((exe or "").strip() or (cfg.reviewer_exe or "").strip()
           or (shutil.which("claude") or ""))
    if not exe:
        return ""
    reader = "type" if os.name == "nt" else "cat"
    extra = verdict_confinement(Config(verdict_cmd="",
                                       verdict_model=cfg.verdict_model)).flags(settings)
    return f'{reader} "{{prompt}}" | "{exe}" -p{extra} > "{{out}}"'


def resolve_cmd(cfg: Config, *, settings: str = "") -> str:
    return cfg.verdict_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe, settings=settings)


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
    """A `SubstantiveVerdict`, or `VerdictDriverError`. The thin API;
    see `parse_verdict_report`."""
    return parse_verdict_report(text)[0]


def parse_verdict_report(text: str) -> tuple[SubstantiveVerdict, dict]:
    """(verdict, meta) — the opinion plus the CLI's own account of the call that made it.

    Unknown keys are dropped rather than kept, for the reason `grade_driver` gives: a
    model-supplied number inside a sealed artifact is a number that looks measured.
    """
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                  "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise VerdictDriverError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise VerdictDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise VerdictDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("verdict") or "").strip():
        raise VerdictDriverError("output JSON has no 'verdict'")
    clean = {k: v for k, v in data.items() if k in SubstantiveVerdict.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return SubstantiveVerdict(**clean), meta
    except Exception as e:
        raise VerdictDriverError(f"output does not match the schema: {e}") from e


def _seal(cfg: Config, pid: str, verdict: SubstantiveVerdict, record: dict) -> dict:
    """Write the opinion and its sidecar. One writer, so the two channels cannot diverge.

    `accept_verdict` (a human pasted a response) and `run()` (a subprocess produced one)
    used to be structurally different: the first sealed a file, the second sealed nothing.
    Sharing this makes `written_by` the only difference between them, which is the only
    difference there should ever have been.
    """
    out = state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"
    state.write_json(out, verdict.model_dump())
    record = dict(record)
    record.update({
        "paper_id": pid, "verdict": verdict.verdict,
        "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "ts": state.now(),
    })
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def accept_verdict(cfg: Config, pid: str, raw: str, *, reader: str = "",
                   tool_policy: str = "unrecorded", mode: str = "MANUAL") -> dict:
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
    # The mode, recorded, as `accept_lens` and `accept_grade` record it. An opinion an
    # isolated subagent produced autonomously and an opinion a person typed are different
    # provenance, and this artifact is the one the self-audit reads to decide whether the
    # paper was judged as a whole — so which of the two it was has to survive.
    from . import delegation
    prov = delegation.provenance_record(mode=mode, reviewer=reader,
                                        tool_policy=tool_policy)
    return _seal(cfg, pid, verdict, {"written_by": prov["written_by"],
                                     "delegation_mode": prov["delegation_mode"],
                                     "reader": prov["reviewer"],
                                     "tool_policy": prov["tool_policy"],
                                     "isolation_claim": prov["isolation_claim"],
                                     "tool_policy_provable": prov["tool_policy_provable"]})


def load_accepted(cfg: Config, pid: str, *, prompt_sha256: str = "") -> SubstantiveVerdict | None:
    """A sealed whole-paper read for `pid`, or None. Verifies the seal before trusting it —
    a file edited after acceptance is not the file that was accepted, exactly as
    `lens_is_accepted` and `grade_is_accepted` require.

    `written_by` now admits `verdict_driver` as well as `manual_accept`. It did not, which
    is what made a subprocess opinion unpersistable and therefore re-billed on every run:
    `run()` could produce one and nothing could ever load one back.

    `prompt_sha256`, when given, must match what the sidecar recorded. A report whose
    findings changed produces a different prompt, and a cached opinion about a different
    set of findings is not a cheaper answer, it is a wrong one — which for this artifact
    means a CONTESTED flag and an exit code derived from a judgement of something else. A
    sidecar with no recorded prompt hash (written before this existed, or by a human who
    pasted a response) is accepted, because refusing it would discard every sealed opinion
    in the repository over a field that did not exist when it was written.
    """
    out = state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"
    sidecar = out.with_suffix(".driver.json")
    if not (out.exists() and sidecar.exists()):
        return None
    try:
        rec = state.read_json(sidecar)
        if not isinstance(rec, dict) or rec.get("written_by") not in WRITERS:
            return None
        if hashlib.sha256(out.read_bytes()).hexdigest() != rec.get("content_sha256"):
            return None
        recorded_prompt = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        return SubstantiveVerdict(**state.read_json(out))
    except Exception:
        return None


def run(cfg: Config, prompt_text: str, *, timeout_s: int = 300,
        pid: str = "") -> SubstantiveVerdict | None:
    """Best-effort. Returns None on ANY failure — an unavailable, timed-out, or
    malformed response degrades to no opinion, never a placeholder one.

    `pid` is what makes the result ATTRIBUTABLE and REUSABLE: given one, the opinion is
    sealed at `reports/<pid>.substantive.json` with a sidecar carrying the argv, the
    confinement flags, the prompt's sha256 and whatever the CLI reported about the model
    and session, and `load_accepted` finds it on the next run instead of paying again.

    Defaulted to "" so the existing call site keeps working unchanged, and that default is
    a KNOWN GAP rather than a design: without a `pid` this call still produces an opinion
    that reaches the CONTESTED flag with no record of where it came from. The sealing is
    here; the caller has to pass the paper id for it to happen.

    Sealing failures are swallowed for the same reason every other failure here is: this
    driver may never block a report. An opinion that was produced and could not be written
    down is still an opinion, and the returned value says so; what is lost is the reuse.
    """
    ok, _why = available(cfg)
    if not ok:
        return None
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="sh-verdict-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-verdict-policy-"))
        conf = verdict_confinement(cfg)
        settings_path = ""
        try:
            if conf.enforced:
                sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
                settings_path = str(sp)
                conf = dataclasses.replace(conf, settings_sha256=sha)
        except OSError:
            # This driver never raises — see `run`'s docstring. An unwritable policy
            # degrades to no opinion, which is the same outcome as any other failure here.
            shutil.rmtree(policy_dir, ignore_errors=True)
            return None
        cmd = (resolve_cmd(cfg, settings=settings_path)
               .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
        group_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})
        sandbox = Path(tempfile.mkdtemp(prefix="sh-verdict-sandbox-"))
        try:
            try:
                # `stdin=DEVNULL` — see `audit_driver.run_lens`.
                proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                        stdin=subprocess.DEVNULL,
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
                verdict, meta = parse_verdict_report(out.read_text(encoding="utf-8"))
            except VerdictDriverError:
                return None
            if pid:
                try:
                    _seal(cfg, pid, verdict, {
                        "written_by": "verdict_driver", "reader": "",
                        "command": cmd, "returncode": proc.returncode,
                        "seconds": round(time.time() - started, 1),
                        "tool_policy": conf.summary(),
                        "tool_policy_detail": conf.policy(),
                        "prompt_sha256": prompt_sha,
                        "raw_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                        "envelope": meta.get("envelope") or {},
                        "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
                    })
                except OSError:
                    pass
            return verdict
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


if __name__ == "__main__":       # self-check: python -m harness.verdict_driver
    import inspect as _inspect
    import tempfile as _tf

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

    # --- the assessor's confinement, from an injected exe so this always runs ----------
    _EXE = "/nonexistent/claude"
    _built = default_cmd(Config(), exe=_EXE, settings="/policy/s.json")
    for _flag in ("--restricted", "--strict-mcp-config", "--settings", "--bare",
                  "--disallowedTools", "--model", "--output-format json"):
        assert _flag in _built, (_flag, _built)
    assert '--allowedTools ""' in _built and "--add-dir" not in _built
    _c = verdict_confinement(Config())
    assert _c.allowed_tools == () and set(_c.disallowed_tools) == set(KNOWN_TOOLS)
    assert _c.bare is True and _c.model and _c.enforced is True
    assert verdict_confinement(Config(verdict_cmd="x {prompt} {out}")).enforced is False
    assert role_model(Config(verdict_model="haiku")) == "haiku"
    assert role_model(Config(verdict_model="")) == VP.ROLE_SPEC["model"]

    # --- the envelope, and unknown keys never reaching the sealed opinion --------------
    _env = json.dumps({"type": "result", "is_error": False, "result": good,
                       "session_id": "s-2", "model": "claude-y"})
    _v, _m = parse_verdict_report(_env)
    assert _v.verdict == "SOUND_WITH_MINOR_CONCERNS"
    assert _m["envelope"]["model_reported"] == "claude-y"
    _num = json.loads(good)
    _num["confidence_score"] = 0.5
    _v2, _m2 = parse_verdict_report(json.dumps(_num))
    assert _m2["unknown_keys_dropped"] == 1
    assert "confidence_score" not in json.dumps(_v2.model_dump())

    # --- an opinion is never unattributed, and never re-billed -------------------------
    with _tf.TemporaryDirectory() as _td:
        _cfg = Config(projects_dir=Path(_td), allow_substantive_verdict=True)
        assert load_accepted(_cfg, "p") is None
        _rec = _seal(_cfg, "p", v, {"written_by": "verdict_driver",
                                    "prompt_sha256": "a" * 64})
        assert _rec["written_by"] == "verdict_driver" and _rec["paper_id"] == "p"
        assert len(_rec["content_sha256"]) == 64
        # Both writers load. Before this, only `manual_accept` did — which is exactly what
        # made a subprocess opinion unpersistable and therefore paid for on every run.
        assert load_accepted(_cfg, "p") is not None
        assert "verdict_driver" in WRITERS and "manual_accept" in WRITERS
        # A cached opinion about a DIFFERENT prompt is refused rather than reused.
        assert load_accepted(_cfg, "p", prompt_sha256="a" * 64) is not None
        assert load_accepted(_cfg, "p", prompt_sha256="b" * 64) is None
        # A sidecar written before the prompt was fingerprinted is still accepted:
        # refusing it would discard every sealed opinion over a field that did not exist.
        _seal(_cfg, "p", v, {"written_by": "manual_accept"})
        assert load_accepted(_cfg, "p", prompt_sha256="b" * 64) is not None
        # An edited opinion file is not the file that was sealed.
        _out = Path(_td) / "p" / "reports" / "p.substantive.json"
        _out.write_text(json.dumps({"verdict": "STRONG"}), encoding="utf-8")
        assert load_accepted(_cfg, "p") is None

    # --- this opinion is PRINTED and counted by nothing --------------------------------
    # Its only consequences are the CONTESTED flag and exit 3, which live in
    # `stages/report.py` and `run.py`. Nothing here may reach a threshold, so nothing
    # here imports the modules that hold one.
    # Read off the syntax tree, not by grepping the text: a source-text guard over this
    # module's own self-check matches the forbidden names in its own assertion list.
    import ast as _ast

    _tree = _ast.parse(_inspect.getsource(sys.modules[__name__]))
    _imported: set[str] = set()
    _called: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.ImportFrom):
            _imported.add(_node.module or "")
            _imported.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
        elif isinstance(_node, _ast.Call):
            _f = _node.func
            if isinstance(_f, _ast.Name):
                _called.add(_f.id)
            elif isinstance(_f, _ast.Attribute):
                _called.add(_f.attr)
    for _forbidden in ("grading", "taxonomy", "stages", "report", "planner"):
        assert _forbidden not in _imported, _forbidden
    for _affordance in ("input", "getpass", "confirm", "approve", "prompt_user"):
        assert _affordance not in _called, _affordance
    assert "stdin=subprocess.DEVNULL" in _inspect.getsource(run)

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_substantive_verdict}, indent=2))
