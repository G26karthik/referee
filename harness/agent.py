"""ONE shared model/agent runner — role -> confinement -> spawn -> envelope -> validated
parse -> seal -> cache — parameterized by role instead of copy-pasted per role.

Wired into the live pipeline for three roles: the four audit lenses (PART 5a, was
`harness/audit_driver.py`), the blinded second-reader grader (PART 5b, was
`harness/grade_driver.py`) and the whole-paper opinion (PART 5c, was
`harness/verdict_driver.py`). `harness/pipeline.py` calls `agent.available`/
`agent.fill_lenses`/`agent.fill_grades`; `run.py` calls the lens/grade entry points
directly.

The mechanics every role's dispatch needs — confinement, pinned-settings staging,
process-group spawn + kill-tree, `--output-format json` envelope parsing, failure
classification, delegation-mode vocabulary, and the seal/verify-seal pair — are NOT
reimplemented here: this module imports them from `harness/delegation.py` (mode
vocabulary + provenance), `harness/sealing.py` (write-then-verify content-hash sealing)
and `harness/reviewer_cli.py` (confinement/spawn/envelope mechanics), which stay real,
independently load-bearing modules in their own right — `harness/artifact_review_driver.py`
and `harness/reimplement_driver.py` (the LIVE implementations of the two roles below)
import all three directly too. What genuinely DIFFERS per role here — which prompt module
builds the prompt, which response schema, which confinement policy, which model tier,
which extra sidecar fields — is data on a `RoleSpec`, or a small per-role function, never
a second copy of the shared dispatch skeleton.

A governed-reconstruction role and an authors'-code-audit role were built here
(PART 5d/5e, mirroring `harness/reimplement_driver.py` and
`harness/artifact_review_driver.py`) but never wired into any production call site —
`harness/routes.py` and `harness/stages/artifact.py` call those two driver modules
directly instead. Confirmed dead by a whole-repo grep before removal: zero callers of
`run_reimplementation`/`accept_reimplementation`/`run_artifact_review`/
`accept_artifact_review` outside this module's own (now-removed) self-check. Removed
2026-09-21 rather than carried as an unreachable second implementation of governance-
sensitive code.

THINGS WORTH FLAGGING, none of them silently changed:

1. **`HARNESS_OWNED_FINDING_KEYS` stays an OPT-OUT list, exactly as `audit_driver.py` had
   it.** `_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)`
   trusts every field of the `Finding` model FROM THE LENS unless it is explicitly named
   as harness-owned. A new `Finding` field added later is trusted by default, not refused
   by default — the opposite of what a from-scratch design would choose. Preserved
   faithfully here because inverting it (opt-in: refuse anything not explicitly named
   lens-owned) is a bigger decision than this consolidation, and changing trust semantics
   silently inside a "just move the code" pass is exactly the kind of thing this whole
   system exists to catch someone else doing. Left for the parent session to decide.

2. **`delegation.py`'s whole-module AST import-restriction test cannot survive the merge
   literally.** `tests/test_delegation_modes.py` asserts `delegation.py`'s own imports are
   a subset of `{shutil, inspect, __future__}` — proof that mode-SELECTION cannot detect
   or touch execution mechanics. `agent.py` necessarily imports `os`/`subprocess`/`re`/etc.
   for the mechanics half (folded in from `reviewer_cli.py`), so no whole-module import
   restriction can apply to this file; that specific guarantee is gone at the module
   level. What IS preserved, and self-checked below: the PURE decision functions this file
   inherits from `delegation.py` (`modes_available`, `provenance_record`, `work_order`,
   `resolve_model`, `resolve_reviewer_exe`, `check_command`) still admit only vocabulary
   strings and booleans in their own signatures — the same property `delegation.py`'s own
   self-check asserts — so a decision cannot be smuggled in as a new parameter type even
   though the file around it now also contains the execution engine. If the module-level
   guarantee matters enough to keep verbatim, the fix is to keep a small sibling module
   holding only this section, import-restricted, with `agent.py` importing from it — a
   real option, not implemented here because the task was to consolidate into one file.

3. **`prompt_sha256` pinning was extended, not just preserved.** `run_lens`/`run_candidate`
   already fingerprinted every unit unconditionally in the reference `audit_driver.py`
   (part, synthesis, and whole-paper units alike) — nothing was missing there. The real gap
   was one level up, in the single-artifact ("whole-paper") roles' MANUAL-accept path:
   `verdict_driver.accept_verdict` had NO `prompt_sha256` parameter at all — only its own
   `run()` (the CLI-subprocess path) ever recorded one. A real corpus file confirms the
   consequence: `projects/acl/reports/acl.substantive.driver.json` (a `session_subagent`
   whole-paper verdict) carries no `prompt_sha256`, so a stale opinion about a changed set
   of findings could never be told apart from a fresh one by that field. `accept_verdict`
   below now takes an optional `prompt_sha256=""` and records it when given — additive, so
   an old sidecar with none is still read (matching every sibling's "absent on either side
   is never a mismatch" rule), but the capability to pin one now exists on every role's
   every write path, not only the
   subprocess one.

`python -m harness.agent` runs the self-check.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import delegation, failures, reviewer_cli, sealing, state
from .schema import Finding, Grade, LensReport, SubstantiveVerdict
from .config import Config
from .prompts import audit as AUDIT_P
from .prompts import grade as GRADE_P
from .prompts import verdict as VERDICT_P

# =============================================================================================
# PART 1 -- DELEGATION VOCABULARY  (harness/delegation.py, imported rather than re-inlined)
# =============================================================================================
DELEGATION_MODES = delegation.DELEGATION_MODES
WRITTEN_BY = delegation.WRITTEN_BY
ISOLATION_CLAIM = delegation.ISOLATION_CLAIM

cli_available = delegation.cli_available
resolve_model = delegation.resolve_model
resolve_reviewer_exe = delegation.resolve_reviewer_exe
check_command = delegation.check_command
modes_available = delegation.modes_available
choose = delegation.choose
provenance_record = delegation.provenance_record
mode_of = delegation.mode_of
summarise = delegation.summarise


def work_order(*, task: str, prompt_path: str, output_path: str, mode: str,
              schema_hint: str = "") -> dict:
    """`delegation.work_order`, with `validated_by` naming this module's own parsers
    instead of the pre-consolidation `audit_driver`/`stages.audit` -- everything else
    (the isolation claim, the must-not-read list) is `delegation.py`'s own contract."""
    wo = delegation.work_order(task=task, prompt_path=prompt_path, output_path=output_path,
                               mode=mode, schema_hint=schema_hint)
    wo["validated_by"] = ("harness.agent's role parsers, then a stage's own accept_* call "
                          "-- a malformed answer is refused, not repaired")
    return wo


# =============================================================================================
# PART 2 -- SEALING  (harness/sealing.py, imported rather than re-inlined)
# =============================================================================================
seal = sealing.seal
verify_seal = sealing.verify_seal


def _verify_and_read(out: Path, writers: tuple[str, ...], *, prompt_sha256: str = ""
                     ) -> tuple[dict, dict] | None:
    """(sidecar_record, payload) or None -- the ONE `sealing.verify_seal` call site every
    role's `load_*` function uses. A `prompt_sha256` mismatch is refused the same way on
    every role: absent on EITHER side is never treated as a mismatch, so a sidecar sealed
    before this field existed, or a caller not yet passing one, is not refused over a
    field that did not exist then."""
    ok, _why = verify_seal(out, accepted_writers=writers)
    if not ok:
        return None
    try:
        rec = state.read_json(out.with_suffix(".driver.json"))
        if not isinstance(rec, dict):
            return None
        recorded_prompt = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        return rec, state.read_json(out)
    except Exception:
        return None


def _seal_role(out: Path, payload: dict, record: dict) -> dict:
    """The write half every role's own `_seal`/`seal` used to reimplement independently
    (verdict, reimplement and artifact_review each had a byte-for-byte identical version
    of this function). `mode`/`reviewer`/`tool_policy` are re-derived FROM `record` rather
    than recomputed a second time, because `record` already carries whatever
    `provenance_record` produced (or, for a live subprocess call, states its mode
    directly, since it IS `CLI_SUBPROCESS`)."""
    return seal(out, payload, mode=record.get("delegation_mode", "MANUAL"),
               reviewer=record.get("reviewer") or record.get("reader", ""),
               tool_policy=record.get("tool_policy", "unrecorded"), extra=record)


# =============================================================================================
# PART 3 -- CLI MECHANICS  (harness/reviewer_cli.py, imported rather than re-inlined)
# =============================================================================================
class AgentError(RuntimeError):
    """A delegated call did not produce a usable result. Self-classifying: every instance
    carries `kind`/`retry`/`reset_hint` from `harness.failures.classify` over its own
    message, so a raise site never has to know which retry-policy bucket its text falls
    into. This module's own exception identity, distinct from
    `reviewer_cli.ReviewerCLIError`, so a caller catching `AgentError` is catching this
    module's roles specifically."""

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = failures.classify(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


class RateLimited(AgentError):
    """A refusal a LATER attempt could satisfy: an account limit, a 429, an overloaded
    upstream. Routed away from `failed` in `fill_many` so it never consumes a retry."""


class NonRetryable(AgentError):
    """A refusal that will fail identically forever: no reviewer installed, an
    unauthenticated account, a flag the CLI does not accept, an unreadable PDF."""


def driver_error(message: str) -> AgentError:
    """Build the right exception class for a failure message, using
    `reviewer_cli.classify_delegated_failure`'s classification."""
    kind, retry, hint = reviewer_cli.classify_delegated_failure(message)
    cls = {"later": RateLimited, "never": NonRetryable}.get(retry, AgentError)
    return cls(message, kind=kind, retry=retry, reset_hint=hint)


CODE_RUNNING_TOOLS = reviewer_cli.CODE_RUNNING_TOOLS
READING_TOOLS = reviewer_cli.READING_TOOLS
KNOWN_TOOLS = reviewer_cli.KNOWN_TOOLS
denied_tools = reviewer_cli.denied_tools
pinned_settings_json = reviewer_cli.pinned_settings_json
Confinement = reviewer_cli.Confinement
operator_confinement = reviewer_cli.operator_confinement
write_pinned_settings = reviewer_cli.write_pinned_settings
prompt_fingerprint = reviewer_cli.prompt_fingerprint
keep_prompt_copy = reviewer_cli.keep_prompt_copy
prompt_is_unchanged = reviewer_cli.prompt_is_unchanged
unwrap_envelope = reviewer_cli.unwrap_envelope
envelope_provenance = reviewer_cli.envelope_provenance
classify_delegated_failure = reviewer_cli.classify_delegated_failure
stage_settings = reviewer_cli.stage_settings
spawn_and_wait = reviewer_cli.spawn_and_wait
cli_pipe_command = reviewer_cli.cli_pipe_command


def _spawn_and_classify(cmd: str, cwd: Path, out: Path, timeout_s: float):
    """Spawn, wait, and raise the right `AgentError` if `out` was not written -- the shared
    tail of every role's own call. Returns the completed process only when `out` exists;
    reading it is the caller's job."""
    try:
        p, timed_out = spawn_and_wait(cmd, cwd, timeout_s)
    except OSError as e:
        raise AgentError(f"could not start the command: {e}") from e
    if timed_out:
        raise AgentError(f"timed out after {timeout_s}s") from None
    if not out.exists():
        tail = (p.stderr or p.stdout or "").strip()[-300:]
        err = driver_error(tail or f"the command exited {p.returncode} with no output")
        if err.kind != "unknown":
            raise err
        raise AgentError(f"the command exited {p.returncode} without writing {out.name}"
                         + (f" -- {tail}" if tail else ""))
    return p


# =============================================================================================
# PART 4 -- ROLE SPECS, and the generic confinement/available/spawn/seal pipeline
# =============================================================================================
@dataclasses.dataclass(frozen=True)
class RoleSpec:
    """The DATA that used to be five copy-pasted `available()`/`default_cmd()`/
    `resolve_cmd()`/`role_model()` functions, one set per driver file."""

    key: str
    allow_attr: str             # Config attribute: the gate flag
    cmd_attr: str                # Config attribute: the operator's command override
    model_attr: str              # Config attribute: model override ("" for "lens" -- each
                                  # lens declares its own model in `prompts.audit.LENSES`)
    timeout_attr: str            # Config attribute: timeout seconds
    cmd_env_name: str            # env var name for placeholder/empty-command messages
    gate_closed_message: str     # the exact sentence printed when the gate is shut
    writers: tuple[str, ...]     # accepted writers when reading a sealed/cached artifact
    role_spec: dict              # `prompts.<role>.ROLE_SPEC` ({} for "lens", which has none)


ROLES: dict[str, RoleSpec] = {
    "lens": RoleSpec(
        key="lens", allow_attr="allow_auto_audit", cmd_attr="audit_cmd", model_attr="",
        timeout_attr="audit_timeout_s", cmd_env_name="SH_AUDIT_CMD",
        gate_closed_message=(
            "auto-audit gate is closed: SH_ALLOW_AUTO_AUDIT is not set. Set it (in the "
            "environment or .env.sandbox) to let the controller fill lenses by running a "
            "reviewer"),
        writers=("audit_driver", "session_subagent", "manual_accept"), role_spec={}),
    "grade": RoleSpec(
        key="grade", allow_attr="allow_grading", cmd_attr="grade_cmd",
        model_attr="grade_model", timeout_attr="grade_timeout_s",
        cmd_env_name="SH_GRADE_CMD",
        gate_closed_message=(
            "grading gate is closed: SH_ALLOW_GRADING is not set. Set it (in the "
            "environment or .env.sandbox) to let the controller independently grade "
            "findings"),
        writers=("grade_driver", "session_subagent", "manual_accept"),
        role_spec=GRADE_P.ROLE_SPEC),
    "verdict": RoleSpec(
        key="verdict", allow_attr="allow_substantive_verdict", cmd_attr="verdict_cmd",
        model_attr="verdict_model", timeout_attr="verdict_timeout_s",
        cmd_env_name="SH_VERDICT_CMD",
        gate_closed_message="substantive-verdict gate is closed; set SH_ALLOW_SUBSTANTIVE_VERDICT=1",
        writers=("verdict_driver", "audit_driver", "session_subagent", "manual_accept"),
        role_spec=VERDICT_P.ROLE_SPEC),
}
# `reimplement` and `artifact_review` roles were removed 2026-09-21 (dead code -- see the
# module docstring): the LIVE governed-reconstruction and authors'-code-audit roles are
# `harness/reimplement_driver.py` and `harness/artifact_review_driver.py`, called directly
# by `harness/routes.py` and `harness/stages/artifact.py`, not through this dispatch table.

_OPERATOR_LABEL = {"grade": "grader", "verdict": "assessor"}
_BLIND_ROLE_LABEL = {"grade": "grader", "verdict": "assessor"}


def _operator_role_label(role_key: str, kw: dict) -> str:
    if role_key == "lens":
        lens = kw.get("lens")
        return f"lens:{lens}" if lens else "lens:unnamed"
    return _OPERATOR_LABEL.get(role_key, role_key)


def _lens_confinement(lens: str | None, pdf_dir: str = "", *,
                      settings_sha256: str = "") -> Confinement:
    """A lens's confinement, from its own declaration in `prompts.audit.LENSES`. `--add-dir`
    grants read access to the paper's own directory ONLY (`SOURCE_FIDELITY` in the prompt);
    a lens with no `pdf_dir` gets no directory at all. NOT `--bare`: a lens legitimately
    needs `Read`, unlike the zero-tool blinded roles below."""
    spec = AUDIT_P.LENSES.get(lens or "", {})
    allowed = tuple(spec.get("tools", ("Read",))) if lens else ("Read",)
    return Confinement(
        role=f"lens:{lens}" if lens else "lens:unnamed",
        allowed_tools=allowed, disallowed_tools=denied_tools(allowed),
        add_dir=(pdf_dir,) if pdf_dir else (),
        model=str(spec.get("model", "")) if lens else "",
        restricted=True, strict_mcp=True, bare=False,
        settings_sha256=settings_sha256, output_format="json")


def _built_in_confinement(cfg: Config, role_key: str, *, settings_sha256: str = "",
                          **kw) -> Confinement:
    """The ENFORCED confinement for a role, ignoring whether the operator supplied a
    command line -- `default_cmd` always builds this, exactly as every original driver's
    `default_cmd` built its own confinement from a Config with the command field cleared,
    regardless of what the caller's own `cfg` held."""
    if role_key == "lens":
        return _lens_confinement(kw.get("lens"), kw.get("pdf_dir", ""),
                                 settings_sha256=settings_sha256)
    # grade / verdict: zero tools, `--bare`, no directories -- read from each role's own
    # ROLE_SPEC, never hardcoded (grade's blinding, verdict's "everything it needs is in
    # the prompt" reasoning).
    spec = ROLES[role_key].role_spec
    allowed = tuple(spec.get("tools", ()))
    return Confinement(
        role=_BLIND_ROLE_LABEL[role_key], allowed_tools=allowed,
        disallowed_tools=denied_tools(allowed), add_dir=(),
        model=role_model(cfg, role_key), restricted=True, strict_mcp=True,
        bare=bool(spec.get("bare", True)), settings_sha256=settings_sha256,
        output_format="json")


def confinement_for(cfg: Config, role_key: str, *, settings_sha256: str = "",
                    **kw) -> Confinement:
    """The confinement record for whichever command `resolve_cmd` would return for this
    role. An operator-supplied command wins unconditionally and is recorded as
    unenforced."""
    role = ROLES[role_key]
    if (getattr(cfg, role.cmd_attr) or "").strip():
        return operator_confinement(_operator_role_label(role_key, kw))
    return _built_in_confinement(cfg, role_key, settings_sha256=settings_sha256, **kw)


def role_model(cfg: Config, role_key: str, **kw) -> str:
    """WHICH model answers for this role. Never the CLI's ambient default."""
    if role_key == "lens":
        lens = kw.get("lens")
        spec = AUDIT_P.LENSES.get(lens or "", {})
        return str(spec.get("model", "")) if lens else ""
    role = ROLES[role_key]
    override = getattr(cfg, role.model_attr) if role.model_attr else ""
    return resolve_model(override, role.role_spec.get("model", ""))


def default_cmd(cfg: Config | None, role_key: str, *, exe: str = "", settings: str = "",
                **kw) -> str:
    """The built-in invocation for this role, or '' when no reviewer can be found. Always
    builds the ENFORCED confinement (see `_built_in_confinement`), regardless of what
    `cfg`'s own operator-command field holds -- `resolve_cmd` is what checks that field.

    `lens`'s own `cfg_exe` step is skipped (matching the reference `audit_driver.default_cmd`,
    whose caller already supplies `exe=cfg.reviewer_exe` and whose own PATH-discovery
    fallback would otherwise run twice) -- every other role passes `cfg.reviewer_exe`
    through. See `harness/delegation.py`'s own historical docstring on
    `resolve_reviewer_exe` for why this one asymmetry is preserved at the call site rather
    than by weakening the shared function.
    """
    cfg = cfg or Config()
    cfg_exe = "" if role_key == "lens" else cfg.reviewer_exe
    exe = resolve_reviewer_exe(exe, cfg_exe)
    if not exe:
        return ""
    extra = _built_in_confinement(cfg, role_key, **kw).flags(settings)
    return cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, role_key: str, *, settings: str = "", **kw) -> str:
    """The command that would run: the operator's if set, else the built-in one."""
    role = ROLES[role_key]
    override = (getattr(cfg, role.cmd_attr) or "").strip()
    if override:
        return override
    return default_cmd(cfg, role_key, exe=cfg.reviewer_exe, settings=settings, **kw)


def available(cfg: Config, role_key: str, **kw) -> tuple[bool, str]:
    """Whether this role can run right now, and if not, the reason to show the operator."""
    role = ROLES[role_key]
    if not getattr(cfg, role.allow_attr):
        return False, role.gate_closed_message
    return check_command(resolve_cmd(cfg, role_key, **kw), role.cmd_env_name)


def _invoke_once(cfg: Config, role_key: str, prompt_text: str, timeout_s: float,
                 **kw) -> tuple[str, dict]:
    """Run ONE confined delegate call in a fresh, throwaway tempdir. Returns
    (raw_text, record). Raises `AgentError`/`RateLimited`/`NonRetryable` on failure --
    used by every best-effort, single-artifact role (`run_verdict` today)."""
    with tempfile.TemporaryDirectory(prefix=f"sh-{role_key}-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix=f"sh-{role_key}-policy-"))
        sandbox = Path(tempfile.mkdtemp(prefix=f"sh-{role_key}-sandbox-"))
        try:
            conf = confinement_for(cfg, role_key, **kw)
            try:
                conf, settings_path = stage_settings(conf, policy_dir)
            except OSError as e:
                raise AgentError(f"could not write the pinned tool policy: {e}") from e
            cmd = (resolve_cmd(cfg, role_key, settings=settings_path, **kw)
                   .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
            p = _spawn_and_classify(cmd, sandbox, out, timeout_s)
            raw = out.read_text(encoding="utf-8")
            return raw, {"command": cmd, "returncode": p.returncode,
                        "tool_policy": conf.summary(), "tool_policy_detail": conf.policy(),
                        "raw_sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


def run_batch_unit(cfg: Config, role_key: str, out: Path, prompt: Path, *, timeout_s: float,
                   parse: Callable[[str], tuple[Any, dict]],
                   to_payload: Callable[[Any], dict],
                   extra_fields: Callable[[Any, dict], dict],
                   role_kwargs: dict | None = None) -> dict:
    """Run one PERSISTENT, discoverable unit -- a lens reading or a grade candidate,
    staged/promoted in the same directory as the final artifact rather than a throwaway
    tempdir, because `fill_many` dispatches many of these per paper and each one's
    `<name>.rejected.txt` / `<name>.raw.txt` sidecar has to survive the call to be
    diagnosable afterward. This is the shared skeleton `audit_driver.run_lens` and
    `grade_driver.run_candidate` each reimplemented independently; `parse`/`to_payload`/
    `extra_fields` are the only per-role plug-ins.
    """
    role_kwargs = role_kwargs or {}
    ok, why = available(cfg, role_key, **role_kwargs)
    if not ok:
        raise AgentError(why)
    if not prompt.exists():
        raise AgentError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    staged = out.with_suffix(".staged")
    rejected = out.with_name(f"{out.stem}.rejected.txt")
    raw_kept = out.with_name(f"{out.stem}.raw.txt")
    for p_ in (staged, rejected, raw_kept):
        p_.unlink(missing_ok=True)

    prompt_sha = prompt_fingerprint(prompt)
    prompt_copy = keep_prompt_copy(prompt, prompt_sha)
    policy_dir = Path(tempfile.mkdtemp(prefix=f"sh-{role_key}-policy-"))
    conf = confinement_for(cfg, role_key, **role_kwargs)
    try:
        conf, settings_path = stage_settings(conf, policy_dir)
    except OSError as e:
        shutil.rmtree(policy_dir, ignore_errors=True)
        raise AgentError(f"could not write the pinned tool policy: {e}") from e
    cmd = (resolve_cmd(cfg, role_key, settings=settings_path, **role_kwargs)
           .replace("{prompt}", str(prompt)).replace("{out}", str(staged)))
    started = time.time()
    sandbox = Path(tempfile.mkdtemp(prefix=f"sh-{role_key}-"))
    try:
        try:
            p = _spawn_and_classify(cmd, sandbox, staged, timeout_s)
        except AgentError:
            staged.unlink(missing_ok=True)
            raise
        raw = staged.read_text(encoding="utf-8")
        try:
            parsed, meta = parse(raw)
        except AgentError:
            classified = driver_error(raw.strip()[:300])
            staged.replace(rejected)
            if classified.retry in ("later", "never"):
                raise classified from None
            raise
        staged.replace(raw_kept)                 # kept on success too, not only on failure
        record = {
            **extra_fields(parsed, meta),
            "tool_policy_detail": conf.policy(), "prompt_sha256": prompt_sha,
            "prompt_copy": str(prompt_copy) if prompt_copy else "",
            "raw_sha256": hashlib.sha256(raw_kept.read_bytes()).hexdigest(),
            "raw_response": str(raw_kept), "command": cmd, "returncode": p.returncode,
            "seconds": round(time.time() - started, 1),
            "envelope": meta.get("envelope") or {},
        }
        return seal(out, to_payload(parsed), mode="CLI_SUBPROCESS",
                   tool_policy=conf.summary(), extra=record)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(policy_dir, ignore_errors=True)


def fill_many(cfg: Config, role_key: str, awaiting: list[str], *, concurrency: int,
             dispatch: Callable[[str], dict], log: Callable[[str, dict], None] | None = None
             ) -> dict:
    """Attempt every pending unit on a bounded thread pool. `dispatch(unit_id)` runs one
    unit and returns its sealed record, or raises `AgentError`/`RateLimited`/
    `NonRetryable`; `log(unit_id, rec)`, when given, is called only on success -- lens and
    grade log different fields to different case-log paths, so that stays a callback
    rather than a shared format string. THE BUCKETS ARE THE RETRY POLICIES, not the
    exception classes: `rate_limited` holds every `later` failure, `blocked` every `never`
    one, and `failed` only what a second attempt could plausibly fix -- see
    `harness/failures.py`. Replaces `audit_driver.fill` and `grade_driver.fill`, which
    shared this exact shape.
    """
    filled, failed, rate_limited, blocked, kinds = [], {}, {}, {}, {}

    def _one(unit_id: str):
        try:
            return unit_id, dispatch(unit_id), None
        except AgentError as e:
            return unit_id, None, e

    workers = max(1, min(len(awaiting), concurrency))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for unit_id, rec, err in pool.map(_one, awaiting):
            if err is None:
                filled.append(unit_id)
                if log is not None:
                    log(unit_id, rec)
            else:
                kinds[unit_id] = {"kind": err.kind, "retry": err.retry,
                                  "reset_hint": err.reset_hint}
                {"later": rate_limited, "never": blocked}.get(err.retry, failed)[unit_id] = str(err)
    return {"filled": filled, "failed": failed, "rate_limited": rate_limited,
           "blocked": blocked, "kinds": kinds}


# =============================================================================================
# PART 5a -- LENS ROLE  (the four audit lenses; was harness/audit_driver.py)
# =============================================================================================
# INVARIANT 2, at the artifact boundary. See the module docstring, point 1: this stays an
# OPT-OUT list, faithfully.
HARNESS_OWNED_FINDING_KEYS: tuple[str, ...] = (
    "verified_observation", "evidence_class", "scientific_class", "verification_state",
    "calc_class", "origin_consistency", "finding_class", "counted_severity",
    "grade_state", "binding_cap", "derivation", "grader_evidence_class",
    "grader_verified_observation", "evidence_ceiling", "evidence_sources", "grade",
    "source_part", "merged_from", "cross_section",
)
POINTER_OWNED_KEYS: tuple[str, ...] = ("evidence_class", "verified_observation")
# `schema_version` decides whether `audit.pass_b_state` treats this report's degeneracy
# fields as checkable (`"legacy"` skips the verification_state == "incomplete" -> NOTE cap
# entirely, invariant 2) -- a lens naming its own version is exactly the self-certification
# invariant 2 forbids, so it is harness-owned and forced below, not merely stripped-if-absent.
HARNESS_OWNED_REPORT_KEYS: tuple[str, ...] = ("merged_duplicates", "schema_version")
# `evidence_origin` is deliberately ABSENT: `stages/audit._coerce` compares the lens's own
# claimed origin against the one derived from the reference's shape, and stripping this key
# here would silently disable that check while leaving it looking like it still runs.
_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)
_LENS_ALLOWED = frozenset(LensReport.model_fields) - frozenset(HARNESS_OWNED_REPORT_KEYS)
_POINTER_ALLOWED = frozenset(("role", "evidence_quote", "evidence_ref"))


def strip_harness_keys(data: dict) -> tuple[dict, int, int]:
    """(clean, n_harness_stripped, n_unknown_dropped) -- a lens report reduced to its own
    words. Two counts: a stripped harness key is an ATTEMPTED FORGERY, a dropped unknown
    key is a reviewer being chatty."""
    if not isinstance(data, dict):
        return {}, 0, 0
    stripped = unknown = 0
    clean: dict = {}
    for key, value in data.items():
        if key in _LENS_ALLOWED:
            clean[key] = value
        else:
            unknown += 1
    out_findings = []
    for f in (clean.get("findings") or []):
        if not isinstance(f, dict):
            out_findings.append(f)
            continue
        kept: dict = {}
        for key, value in f.items():
            if key in HARNESS_OWNED_FINDING_KEYS:
                stripped += 1
            elif key in _FINDING_ALLOWED:
                kept[key] = value
            else:
                unknown += 1
        sides = kept.get("additional_evidence")
        if isinstance(sides, list):
            clean_sides = []
            for side in sides:
                if not isinstance(side, dict):
                    unknown += 1
                    continue
                keep_side: dict = {}
                for key, value in side.items():
                    if key in POINTER_OWNED_KEYS:
                        stripped += 1
                    elif key in _POINTER_ALLOWED:
                        keep_side[key] = value
                    else:
                        unknown += 1
                clean_sides.append(keep_side)
            kept["additional_evidence"] = clean_sides
        out_findings.append(kept)
    if "findings" in clean:
        clean["findings"] = out_findings
    return clean, stripped, unknown


def parse_lens_json(text: str, lens: str) -> LensReport:
    return parse_lens_report(text, lens)[0]


def parse_lens_report(text: str, lens: str) -> tuple[LensReport, dict]:
    """(report, meta). Tolerant of a JSON object wrapped in prose or a fenced code block,
    and of the CLI's own result envelope. Not tolerant of a missing `findings` key or of a
    quote with no reference -- an unlocatable finding is refused, not persisted."""
    outer = (text or "").strip()
    if not outer:
        raise AgentError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise driver_error(raw[:300] or "the reviewer reported an error and returned no result")
    if not raw:
        raise AgentError("the reviewer returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AgentError("output JSON is not an object")
    if "findings" not in data:
        raise AgentError("output JSON has no 'findings' key")
    if not isinstance(data["findings"], list):
        raise AgentError("'findings' is not a list")

    for i, f in enumerate(data["findings"]):
        if not isinstance(f, dict):
            raise AgentError(f"findings[{i}] is not an object")
        if (f.get("evidence_quote") or "").strip() and not (f.get("evidence_ref") or "").strip():
            raise AgentError(
                f"findings[{i}] ({f.get('finding_id') or 'unnamed'}) carries an "
                f"evidence_quote but no evidence_ref; a quote with no location cannot be "
                f"verified and must not be persisted")
        for j, side in enumerate(f.get("additional_evidence") or []):
            if not isinstance(side, dict):
                raise AgentError(f"findings[{i}].additional_evidence[{j}] is not an object")
            if not (side.get("evidence_quote") or "").strip():
                raise AgentError(
                    f"findings[{i}].additional_evidence[{j}] carries no evidence_quote; a "
                    f"further location with nothing quoted from it names nothing")
            if not (side.get("evidence_ref") or "").strip():
                raise AgentError(
                    f"findings[{i}].additional_evidence[{j}] carries an evidence_quote but "
                    f"no evidence_ref; the second half of a cross-section concern has to be "
                    f"as locatable as the first")

    data, stripped, unknown = strip_harness_keys(data)
    meta["harness_keys_stripped"] = stripped
    meta["unknown_keys_dropped"] = unknown
    data["lens"] = lens
    # Harness-owned, forced regardless of what strip_harness_keys removed or the lens wrote
    # (invariant 2): a lens naming a stale or absent version would otherwise put its own
    # pass-B degeneracy check into `audit.pass_b_state`'s uncapped "legacy" branch, deciding
    # for itself whether its own falsification/steelman fields are checkable.
    data["schema_version"] = 2
    try:
        return LensReport(**data), meta
    except Exception as e:
        raise AgentError(f"output does not match the lens schema: {e}") from e


def run_lens(cfg: Config, pid: str, lens: str, prompt: Path, out: Path, *,
            pdf_dir: str = "", unit_id: str = "") -> dict:
    """Run the configured reviewer for one lens reading (whole-paper, part, or synthesis
    -- `unit_id` names which). `prompt_sha256` is fingerprinted unconditionally by
    `run_batch_unit` regardless of unit shape, exactly as the reference `audit_driver.run_lens`
    already did."""
    return run_batch_unit(
        cfg, "lens", out, prompt, timeout_s=cfg.audit_timeout_s,
        parse=lambda raw: parse_lens_report(raw, lens),
        to_payload=lambda parsed: parsed.model_dump(),
        extra_fields=lambda parsed, meta: {
            "lens": lens, "unit_id": unit_id or lens, "paper_id": pid,
            "findings": len(parsed.findings),
            "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
            "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
        },
        role_kwargs={"lens": lens, "pdf_dir": pdf_dir})


def fill_lenses(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str],
               pdf_dir: str = "", units: list[dict] | None = None) -> dict:
    """Attempt every pending lens/part/synthesis unit. `units` maps a reading-unit id to
    the lens it belongs to and the file it writes, for a paper read in parts; without it
    the unit id IS the lens name and the output is `audit/<lens>.json`."""
    audit_dir = state.project_dir(cfg, pid) / "audit"
    by_unit = {u["unit_id"]: u for u in (units or [])}

    def _out_path(unit_id: str) -> Path:
        spec = by_unit.get(unit_id, {})
        return Path(spec["out"]) if spec.get("out") else audit_dir / f"{unit_id}.json"

    def dispatch(unit_id: str) -> dict:
        spec = by_unit.get(unit_id, {})
        lens = spec.get("lens", unit_id)
        return run_lens(cfg, pid, lens, Path(prompts[unit_id]), _out_path(unit_id),
                        pdf_dir=pdf_dir, unit_id=unit_id)

    def log(unit_id: str, rec: dict) -> None:
        spec = by_unit.get(unit_id, {})
        lens = spec.get("lens", unit_id)
        detail = rec.get("tool_policy_detail") or {}
        state.append_log(cfg, pid, artifact_type="audit_auto", phase="audit",
                        headers={"lens": lens, "unit": unit_id, "findings": rec["findings"],
                                 "seconds": rec["seconds"],
                                 "model_requested": detail.get("model_requested", ""),
                                 "model_reported": (rec.get("envelope") or {}).get(
                                     "model_reported", ""),
                                 "confinement_enforced": bool(detail.get("enforced")),
                                 "harness_keys_stripped": rec.get("harness_keys_stripped", 0)},
                        path=str(_out_path(unit_id)))

    return fill_many(cfg, "lens", awaiting, concurrency=cfg.audit_concurrency,
                     dispatch=dispatch, log=log)


# =============================================================================================
# PART 5b -- GRADE ROLE  (the blinded second-reader grader; was harness/grade_driver.py)
# =============================================================================================
_WITHHELD = ["severity", "severity_rationale", "lens", "other_findings",
            "prior_grades", "verdict_thresholds", "derivation_table"]


def parse_grade_json(text: str) -> Grade:
    return parse_grade_report(text)[0]


def parse_grade_report(text: str) -> tuple[Grade, dict]:
    """(grade, meta). Not tolerant of a missing `verdict`. Unknown keys are DROPPED rather
    than kept -- a model-assigned numeric weight or score never survives into the sealed
    grade (invariant 11)."""
    outer = (text or "").strip()
    if not outer:
        raise AgentError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "desiderata_violations": [], "desiderata_passed": None,
                 "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise driver_error(raw[:300] or "the grader reported an error and returned no result")
    if not raw:
        raise AgentError("the grader returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AgentError("output JSON is not an object")
    if not str(data.get("verdict") or "").strip():
        raise AgentError("output JSON has no 'verdict'")

    violations, unknown_desiderata = GRADE_P.parse_violations(data.get("desiderata_violations"))
    meta["desiderata_violations"] = list(violations)
    meta["unknown_desiderata"] = list(unknown_desiderata)
    if data.get("desiderata_violations") is not None or violations:
        meta["desiderata_passed"] = len(violations) == 0
    if unknown_desiderata:
        meta["desiderata_passed"] = False

    clean = {k: v for k, v in data.items() if k in Grade.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return Grade(**clean), meta
    except Exception as e:
        raise AgentError(f"output does not match the grade schema: {e}") from e


def run_candidate(cfg: Config, pid: str, slug: str, prompt: Path, out: Path, *,
                  withheld: list[str]) -> dict:
    return run_batch_unit(
        cfg, "grade", out, prompt, timeout_s=cfg.grade_timeout_s,
        parse=parse_grade_report, to_payload=lambda parsed: parsed.model_dump(),
        extra_fields=lambda parsed, meta: {
            "slug": slug, "paper_id": pid, "verdict": parsed.verdict,
            "written_by": "grade_driver", "withheld": withheld,
            "desiderata_violations": meta.get("desiderata_violations") or [],
            "desiderata_passed": meta.get("desiderata_passed"),
            "unknown_desiderata": meta.get("unknown_desiderata") or [],
            "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
        })


def fill_grades(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str]) -> dict:
    grade_dir = state.project_dir(cfg, pid) / "audit" / "grade"

    def dispatch(slug: str) -> dict:
        return run_candidate(cfg, pid, slug, Path(prompts[slug]), grade_dir / f"{slug}.json",
                            withheld=_WITHHELD)

    def log(slug: str, rec: dict) -> None:
        detail = rec.get("tool_policy_detail") or {}
        state.append_log(cfg, pid, artifact_type="grade_auto", phase="grade",
                        headers={"slug": slug, "verdict": rec["verdict"],
                                 "seconds": rec["seconds"],
                                 "model_requested": detail.get("model_requested", ""),
                                 "model_reported": (rec.get("envelope") or {}).get(
                                     "model_reported", ""),
                                 "confinement_enforced": bool(detail.get("enforced")),
                                 "desiderata_passed": rec.get("desiderata_passed")},
                        path=str(grade_dir / f"{slug}.json"))

    return fill_many(cfg, "grade", awaiting, concurrency=cfg.grade_concurrency,
                     dispatch=dispatch, log=log)


# =============================================================================================
# PART 5c -- VERDICT ROLE  (the whole-paper opinion; was harness/verdict_driver.py)
# =============================================================================================
def parse_verdict_json(text: str) -> SubstantiveVerdict:
    return parse_verdict_report(text)[0]


def parse_verdict_report(text: str) -> tuple[SubstantiveVerdict, dict]:
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise AgentError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("verdict") or "").strip():
        raise AgentError("output JSON has no 'verdict'")
    clean = {k: v for k, v in data.items() if k in SubstantiveVerdict.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return SubstantiveVerdict(**clean), meta
    except Exception as e:
        raise AgentError(f"output does not match the schema: {e}") from e


def _verdict_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"


def _seal_verdict(cfg: Config, pid: str, verdict: SubstantiveVerdict, record: dict) -> dict:
    return _seal_role(_verdict_path(cfg, pid), verdict.model_dump(),
                      {**record, "paper_id": pid, "verdict": verdict.verdict})


def accept_verdict(cfg: Config, pid: str, raw: str, *, reader: str = "",
                   tool_policy: str = "unrecorded", mode: str = "MANUAL",
                   prompt_sha256: str = "") -> dict:
    """Seal a whole-paper read produced OUTSIDE this module's own subprocess -- a human, or
    a session subagent. `prompt_sha256`, when given, is now pinned here too: see the
    module docstring, point 3, for the real corpus file (`acl.substantive.driver.json`)
    this closes the gap for."""
    verdict = parse_verdict_json(raw)
    prov = provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    record = {"written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
             "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
             "isolation_claim": prov["isolation_claim"],
             "tool_policy_provable": prov["tool_policy_provable"]}
    if prompt_sha256:
        record["prompt_sha256"] = prompt_sha256
    return _seal_verdict(cfg, pid, verdict, record)


def load_verdict(cfg: Config, pid: str, *, prompt_sha256: str = "") -> SubstantiveVerdict | None:
    got = _verify_and_read(_verdict_path(cfg, pid), ROLES["verdict"].writers,
                          prompt_sha256=prompt_sha256)
    if got is None:
        return None
    _rec, data = got
    try:
        return SubstantiveVerdict(**data)
    except Exception:
        return None


def run_verdict(cfg: Config, prompt_text: str, *, timeout_s: int = 0,
                pid: str = "") -> SubstantiveVerdict | None:
    """Best-effort. Returns None on ANY failure -- this driver may never block a report."""
    ok, _why = available(cfg, "verdict")
    if not ok:
        return None
    timeout_s = timeout_s or cfg.verdict_timeout_s
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    try:
        raw, rec = _invoke_once(cfg, "verdict", prompt_text, timeout_s)
    except AgentError:
        return None
    try:
        verdict, meta = parse_verdict_report(raw)
    except AgentError:
        return None
    if pid:
        try:
            _seal_verdict(cfg, pid, verdict, {
                "written_by": "verdict_driver", "reader": "", "delegation_mode": "CLI_SUBPROCESS",
                **rec, "seconds": round(time.time() - started, 1), "prompt_sha256": prompt_sha,
                "envelope": meta.get("envelope") or {},
                "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0)})
        except OSError:
            pass
    return verdict


# =============================================================================================
# self-check: python -m harness.agent
# =============================================================================================
if __name__ == "__main__":
    import ast as _ast
    import inspect as _inspect
    import tempfile as _tf

    cfg = Config.load()

    # --- gate semantics, one per role ---------------------------------------------------
    assert available(Config(allow_auto_audit=False), "lens")[0] is False
    assert available(Config(allow_grading=False), "grade")[0] is False
    assert available(Config(allow_substantive_verdict=False), "verdict")[0] is False
    for _role, _msg_part in (("lens", "SH_ALLOW_AUTO_AUDIT"), ("grade", "SH_ALLOW_GRADING"),
                             ("verdict", "SH_ALLOW_SUBSTANTIVE_VERDICT")):
        assert _msg_part in ROLES[_role].gate_closed_message, _role

    # --- confinement differs correctly per role, driven from an injected exe -------------
    _EXE = "/nonexistent/claude"
    lens_cmd = default_cmd(Config(), "lens", exe=_EXE, settings="/s.json",
                          lens="overclaim", pdf_dir="/papers")
    grade_cmd = default_cmd(Config(), "grade", exe=_EXE, settings="/s.json")
    verdict_cmd = default_cmd(Config(), "verdict", exe=_EXE, settings="/s.json")

    assert '--allowedTools "Read"' in lens_cmd, lens_cmd
    assert '--allowedTools ""' in grade_cmd, grade_cmd
    assert '--allowedTools ""' in verdict_cmd, verdict_cmd

    assert "--bare" not in lens_cmd, "a lens legitimately needs Read"
    assert "--bare" in grade_cmd and "--bare" in verdict_cmd
    assert "--add-dir" in lens_cmd and "/papers" in lens_cmd
    for _c in (grade_cmd, verdict_cmd):
        assert "--add-dir" not in _c, _c
    for _c in (lens_cmd, grade_cmd, verdict_cmd):
        assert "{prompt}" in _c and "{out}" in _c and "--model" in _c and "--restricted" in _c
        assert "--strict-mcp-config" in _c and "--output-format json" in _c

    # --- ROLE_SPEC is READ, never hardcoded -- the 2026-09-19 fix this must preserve -----
    for _role_key, _module in (("grade", GRADE_P), ("verdict", VERDICT_P)):
        _conf = _built_in_confinement(Config(), _role_key)
        assert set(_conf.allowed_tools) == set(_module.ROLE_SPEC.get("tools", ())), _role_key
        if "bare" in _module.ROLE_SPEC:
            assert _conf.bare == _module.ROLE_SPEC["bare"], _role_key
    # An operator-supplied command line is recorded as UNENFORCED for every role.
    for _role_key, _attr in (("lens", "audit_cmd"), ("grade", "grade_cmd"),
                             ("verdict", "verdict_cmd")):
        assert confinement_for(Config(**{_attr: "x {prompt} {out}"}), _role_key).enforced is False
        assert confinement_for(Config(), _role_key).enforced is True

    # --- opt-in/opt-out trust boundary: HARNESS_OWNED_FINDING_KEYS is OPT-OUT, preserved -
    _forged = {"lens": "overclaim", "schema_version": 2, "findings": [
        {"finding_id": "o-1", "severity": "MAJOR", "title": "t", "statement": "s",
         "evidence_quote": "q", "evidence_ref": "p1",
         "verified_observation": "THE HARNESS CONFIRMED THIS",
         "evidence_class": "cell_verified", "counted_severity": "FATAL",
         "finding_class": "CONFIRMED_FINDING", "totally_made_up_key": "yes"}], "notes": "n"}
    _rep, _meta = parse_lens_report(json.dumps(_forged), "overclaim")
    _f = _rep.findings[0]
    assert _f.verified_observation == "", "a lens may not certify its own reasoning"
    assert _f.evidence_class == "unverified" and _f.counted_severity == ""
    assert _f.finding_class == "UNGRADED"
    assert _meta["harness_keys_stripped"] == 4, _meta
    assert _meta["unknown_keys_dropped"] == 1, _meta
    for _k in HARNESS_OWNED_FINDING_KEYS:
        assert _k in Finding.model_fields, _k          # every harness-owned key is real
    assert "evidence_origin" not in HARNESS_OWNED_FINDING_KEYS

    # --- seal / verify_seal round-trip, and cache invalidation ---------------------------
    with _tf.TemporaryDirectory() as td:
        out = Path(td) / "thing.json"
        seal(out, {"a": 1}, mode="CLI_SUBPROCESS", extra={"prompt_sha256": "a" * 64})
        ok, why = verify_seal(out, accepted_writers=("audit_driver",))
        assert ok, why
        assert _verify_and_read(out, ("audit_driver",), prompt_sha256="a" * 64) is not None
        assert _verify_and_read(out, ("audit_driver",), prompt_sha256="b" * 64) is None, (
            "a changed prompt_sha256 must invalidate the cache")
        out.write_text('{"a": 999}', encoding="utf-8")
        assert _verify_and_read(out, ("audit_driver",)) is None, (
            "an edited artifact is not the artifact that was sealed")

    # --- verdict's cache now uses the ONE verify_seal call site, not a hand-rolled check -
    with _tf.TemporaryDirectory() as td:
        _cfg = Config(projects_dir=Path(td), allow_substantive_verdict=True)
        assert load_verdict(_cfg, "p") is None
        _v = SubstantiveVerdict(verdict="STRONG", reason="r", strongest_contribution="c",
                               weakest_link="w", weaknesses_are="LOCAL")
        accept_verdict(_cfg, "p", json.dumps(_v.model_dump()), reader="alice",
                       prompt_sha256="c" * 64)
        assert load_verdict(_cfg, "p") is not None
        assert load_verdict(_cfg, "p", prompt_sha256="c" * 64) is not None
        assert load_verdict(_cfg, "p", prompt_sha256="d" * 64) is None, (
            "accept_verdict can now pin a prompt hash, closing the gap "
            "projects/acl/reports/acl.substantive.driver.json shows in the field")
        _out = _verdict_path(_cfg, "p")
        state.write_json(_out, {"verdict": "TAMPERED"})
        assert load_verdict(_cfg, "p") is None

    # --- a role that should default to haiku actually does -------------------------------
    # No SURVIVING prompt module currently declares a haiku default: the two that did
    # (prompts.literature.QUERY_ROLE_SPEC, prompts.claimlink.ROLE_SPEC) were deleted
    # 2026-09-20 with their routes. What is asserted here is that the MACHINERY honours
    # whatever a spec declares, generically -- so a future haiku-tier role is not silently
    # promoted to the ambient default the way `grade_driver.default_cmd()` once was.
    assert resolve_model("", "haiku") == "haiku"
    assert resolve_model("opus", "haiku") == "opus", "an operator override still wins"
    for _role_key in ("grade", "verdict"):
        assert ROLES[_role_key].role_spec.get("model") != "haiku", (
            f"{_role_key} is sonnet today -- if this ever flips to haiku the assertion "
            f"below (role_model reads it) must still hold")
        assert role_model(Config(), _role_key) == ROLES[_role_key].role_spec["model"]

    # --- end-to-end against a FAKE reviewer executable, for three roles -------------------
    def _stub_cmd(td_: Path, name: str, canned: dict) -> str:
        stub = td_ / f"{name}.py"
        stub.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path(sys.argv[-1]).write_text({json.dumps(json.dumps(canned))}, "
            f"encoding='utf-8')\n", encoding="utf-8")
        return f'"{sys.executable}" "{stub}" {{prompt}} {{out}}'

    with _tf.TemporaryDirectory() as _td:
        _td = Path(_td)

        _lens_prompt = _td / "overclaim.md"
        _lens_prompt.write_text("read the paper", encoding="utf-8")
        _lens_stub = _stub_cmd(_td, "lens_stub", {"lens": "overclaim", "findings": [
            {"finding_id": "o-1", "severity": "MINOR", "title": "t", "statement": "s",
             "evidence_quote": "q", "evidence_ref": "p1"}]})
        _cfg_lens = Config(projects_dir=_td / "projects", allow_auto_audit=True,
                          audit_cmd=_lens_stub)
        _lrec = run_lens(_cfg_lens, "p", "overclaim", _lens_prompt, _td / "out" / "overclaim.json")
        assert _lrec["findings"] == 1 and _lrec["written_by"] == "audit_driver"
        assert len(_lrec["prompt_sha256"]) == 64

        _grade_prompt = _td / "c-01.md"
        _grade_prompt.write_text("grade this", encoding="utf-8")
        _grade_stub = _stub_cmd(_td, "grade_stub", {"verdict": "CONFIRMED", "severity": "MAJOR"})
        _cfg_grade = Config(projects_dir=_td / "projects", allow_grading=True,
                           grade_cmd=_grade_stub)
        _grec = run_candidate(_cfg_grade, "p", "c-01", _grade_prompt,
                             _td / "grade_out" / "c-01.json", withheld=[])
        assert _grec["verdict"] == "CONFIRMED" and _grec["written_by"] == "grade_driver"

        _verdict_stub = _stub_cmd(_td, "verdict_stub", {
            "verdict": "SOUND_WITH_MINOR_CONCERNS", "reason": "r",
            "strongest_contribution": "c", "weakest_link": "w", "weaknesses_are": "LOCAL"})
        _cfg_verdict = Config(projects_dir=_td / "projects", allow_substantive_verdict=True,
                             verdict_cmd=_verdict_stub)
        _v = run_verdict(_cfg_verdict, "prompt text", pid="p")
        assert _v is not None and _v.verdict == "SOUND_WITH_MINOR_CONCERNS"
        _v_loaded = load_verdict(_cfg_verdict, "p")
        assert _v_loaded is not None and _v_loaded.verdict == _v.verdict

    # --- a command writing no output raises, and buckets correctly in fill_many ----------
    with _tf.TemporaryDirectory() as _td:
        _td = Path(_td)
        _prompt = _td / "overclaim.md"
        _prompt.write_text("prompt", encoding="utf-8")
        _silent = Config(projects_dir=_td, allow_auto_audit=True,
                        audit_cmd=f'"{sys.executable}" -c "pass" {{prompt}} {{out}}')
        try:
            run_lens(_silent, "p", "overclaim", _prompt, _td / "overclaim.json")
            raise AssertionError("a command writing no output must raise")
        except AgentError as e:
            assert "without writing" in str(e), str(e)

        _limiter = _td / "limiter.py"
        _limiter.write_text(
            "import sys\nsys.stdout.write(\"You've hit your session limit \\u00b7 resets "
            "3:20pm (Asia/Kolkata)\")\n", encoding="utf-8")
        _limited = Config(projects_dir=_td, allow_auto_audit=True,
                         audit_cmd=f'"{sys.executable}" "{_limiter}" {{prompt}} {{out}}')
        try:
            run_lens(_limited, "p", "overclaim", _prompt, _td / "overclaim2.json")
            raise AssertionError("a rate-limit response must raise")
        except RateLimited as e:
            assert "3:20pm" in str(e), str(e)
        except AgentError as e:
            raise AssertionError(f"raised the generic class, not RateLimited: {e}") from e

    # --- parsing: envelope unwrap, prose/fence tolerance --------------------------------
    body = ('Here you go:\n```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
           '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
           '"evidence_ref":"p1"}],"unasked_question":"u","notes":"n"}\n```')
    _rep2 = parse_lens_json(body, "overclaim")
    assert _rep2.lens == "overclaim" and len(_rep2.findings) == 1
    _inner = '{"lens":"overclaim","findings":[]}'
    _env = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": _inner, "session_id": "s-1", "total_cost_usd": 0.5,
                      "num_turns": 3, "modelUsage": {"claude-opus-4": {"in": 1}}})
    _payload, _envelope = unwrap_envelope(_env)
    assert _payload == _inner and _envelope.get("session_id") == "s-1"
    _prov = envelope_provenance(_envelope)
    assert _prov["model_reported"] == "claude-opus-4" and _prov["cost_usd"] == 0.5
    assert "session_id" in _prov["envelope_keys"]

    # --- delegation-specific failure classification -------------------------------------
    assert classify_delegated_failure(
        "Claude requested permissions to use Bash, but you have not granted it"
    )[:2] == ("bad_invocation", "never")
    assert classify_delegated_failure("the random seed was not set")[:2] == ("unknown", "now")
    for _text, _want in (("You've hit your session limit · resets 3:20pm", "later"),
                        ("HTTP 429 Too Many Requests", "later"),
                        ("the request timed out", "now")):
        assert classify_delegated_failure(_text)[1] == _want, _text

    # --- delegation vocabulary: pure decision functions admit only str/bool ------------
    for _fn in (modes_available, provenance_record, work_order):
        for _name, _p in _inspect.signature(_fn).parameters.items():
            assert str(_p.annotation) in ("str", "bool"), f"{_fn.__name__}.{_name}"
    assert set(WRITTEN_BY) | {"UNAVAILABLE"} == set(DELEGATION_MODES)
    assert len(set(WRITTEN_BY.values())) == len(WRITTEN_BY)
    cli = provenance_record(mode="CLI_SUBPROCESS", reviewer="claude", tool_policy="enforced: x")
    assert cli["tool_policy_provable"] is True
    for _mode in ("SESSION_SUBAGENT", "MANUAL"):
        _rec = provenance_record(mode=_mode, tool_policy="enforced: x")
        assert _rec["tool_policy"] == "unrecorded", _mode

    # --- what this layer must be INCAPABLE of: cannot decide the report's outcome -------
    # Read off the syntax tree, not by grepping the text -- a source-text guard over this
    # module's own self-check would match the forbidden names in its own assertion list.
    # "verdict" is deliberately NOT in the forbidden-assigned set: unlike audit_driver.py
    # alone, this merged module legitimately transports a `SubstantiveVerdict` MODEL
    # OUTPUT end to end (parse -> seal -> return) under a local name `verdict`, exactly as
    # it legitimately transports `Grade.verdict`/`LensReport.findings` -- that is moving a
    # model's own answer, not this layer computing one. What must still never be assigned
    # is a REPORT-LEVEL decision this layer has no business producing.
    _src = _inspect.getsource(sys.modules[__name__])
    _tree = _ast.parse(_src)
    _imported: set[str] = set()
    _assigned: set[str] = set()
    _called: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.ImportFrom):
            _imported.add(_node.module or "")
            _imported.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
        elif isinstance(_node, (_ast.Assign, _ast.AnnAssign)):
            for _t in (_node.targets if isinstance(_node, _ast.Assign) else [_node.target]):
                if isinstance(_t, _ast.Name):
                    _assigned.add(_t.id)
                elif isinstance(_t, _ast.Attribute):
                    _assigned.add(_t.attr)
        elif isinstance(_node, _ast.Call):
            _fn_node = _node.func
            if isinstance(_fn_node, _ast.Name):
                _called.add(_fn_node.id)
            elif isinstance(_fn_node, _ast.Attribute):
                _called.add(_fn_node.attr)
    for _forbidden in ("grading", "taxonomy", "stages", "planner", "priority", "report"):
        assert _forbidden not in _imported, (
            f"the delegation layer must not reach into harness.{_forbidden}")
    for _decision in ("severity", "counted_severity", "triage", "claim_status",
                     "finding_class", "binding_cap"):
        assert _decision not in _assigned, f"this layer assigned {_decision!r}"
    for _affordance in ("input", "getpass", "confirm", "prompt_user", "approve",
                       "getch", "ask"):
        assert _affordance not in _called, (
            f"{_affordance!r} is a human checkpoint inside an autonomous stage")
    assert "stdin=subprocess.DEVNULL" in _inspect.getsource(spawn_and_wait)
    assert "spawn_and_wait" in _inspect.getsource(_spawn_and_classify)
    assert "_spawn_and_classify" in _inspect.getsource(run_batch_unit)
    assert "_spawn_and_classify" in _inspect.getsource(_invoke_once)
    _policy_keys = set(_lens_confinement("overclaim", "/papers").policy())
    for _leak in ("severity", "counted_severity", "verdict", "triage", "confidence",
                 "finding_class"):
        assert _leak not in _policy_keys, _leak

    print(json.dumps({"self_check": "ok", "roles": sorted(ROLES)}, indent=2))
