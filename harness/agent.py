"""ONE shared model/agent runner — role -> confinement -> spawn -> envelope -> validated
parse -> seal -> cache — parameterized by role instead of copy-pasted per role.

Consolidates seven modules that had accreted around one idea (shell out to a reviewer,
confine it, validate what comes back, seal the provenance) into one pipeline plus five
small per-role plug-ins:

  * `harness/audit_driver.py`            (1017 lines) -- the four audit lenses
  * `harness/grade_driver.py`            ( 477 lines) -- the blinded second-reader grader
  * `harness/verdict_driver.py`          ( 406 lines) -- the whole-paper opinion
  * `harness/reimplement_driver.py`      ( 599 lines) -- the governed reconstruction writer
  * `harness/artifact_review_driver.py`  ( 639 lines) -- the authors'-code auditor
  * `harness/reviewer_cli.py`            ( 555 lines) -- the confinement/spawn/envelope mechanics
  * `harness/delegation.py`              ( 398 lines) -- delegation-mode vocabulary + provenance
  * `harness/sealing.py`                 ( 151 lines) -- write-then-verify content-hash sealing

None of those seven files is modified or deleted here; this module is additional and not
yet wired into the pipeline. What was genuinely SHARED across the five drivers (the
`Confinement` dataclass, pinned-settings staging, process-group spawn + kill-tree,
`--output-format json` envelope parsing, failure classification, delegation-mode
vocabulary, and the seal/verify-seal pair) now has exactly one implementation. What
genuinely DIFFERS per role — which prompt module builds the prompt, which response
schema, which confinement policy, which model tier, which extra sidecar fields — is data
on a `RoleSpec`, or a small (10-40 line) per-role function, never a second copy of the
100+-line dispatch skeleton.

THREE THINGS WORTH FLAGGING TO WHOEVER WIRES THIS IN NEXT, none of them silently changed:

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
   was one level up, in the three single-artifact ("whole-paper") roles' MANUAL-accept
   paths: `verdict_driver.accept_verdict`, `reimplement_driver.accept_reimplementation` and
   `artifact_review_driver.accept` had NO `prompt_sha256` parameter at all — only their own
   `run()` (the CLI-subprocess path) ever recorded one. A real corpus file confirms the
   consequence: `projects/acl/reports/acl.substantive.driver.json` (a `session_subagent`
   whole-paper verdict) carries no `prompt_sha256`, so a stale opinion about a changed set
   of findings could never be told apart from a fresh one by that field. `accept_verdict`,
   `accept_reimplementation` and `accept_artifact_review` below all take an optional
   `prompt_sha256=""` and record it when given — additive, so an old sidecar with none is
   still read (matching every sibling's "absent on either side is never a mismatch" rule),
   but the capability to pin one now exists on every role's every write path, not only the
   subprocess one.

`python -m harness.agent` runs the self-check.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import artifact_evidence, failures, state
from .artifacts import (ARTIFACT_IDENTITY_BASES, ARTIFACT_IDENTITY_STATES, ArtifactFact,
                        ArtifactInspection, ArtifactSnapshot, Finding, Grade, LensReport,
                        PaperDoc, ReimplementationBinding, ReimplementationConformance,
                        ReimplementationReadiness, SubstantiveVerdict)
from .config import Config
from .prompts import artifact_review as ARTIFACT_P
from .prompts import audit as AUDIT_P
from .prompts import grade as GRADE_P
from .prompts import reimplement as REIMPLEMENT_P
from .prompts import verdict as VERDICT_P

# =============================================================================================
# PART 1 -- DELEGATION VOCABULARY  (was harness/delegation.py)
# =============================================================================================
# HOW a reasoning task was actually carried out, closed and ordered by how much the
# resulting artifact can prove about its own production. See the module docstring, point 2,
# for what changed and did not change about this section's isolation from execution.
DELEGATION_MODES = (
    "CLI_SUBPROCESS",    # a fresh reviewer CLI process this module spawned and confined
    "SESSION_SUBAGENT",  # an isolated subagent of the controlling session, one per task
    "MANUAL",            # a human, or an agent this harness knows nothing about
    "UNAVAILABLE",       # nothing can be delegated to from here
)

WRITTEN_BY = {
    "CLI_SUBPROCESS": "audit_driver",
    "SESSION_SUBAGENT": "session_subagent",
    "MANUAL": "manual_accept",
}

ISOLATION_CLAIM = {
    "CLI_SUBPROCESS":
        "one process per task, argv built by this harness: allowed tools restricted, "
        "remaining tools explicitly denied, host plugins and MCP servers disabled, an "
        "empty working directory, and read access added for the papers directory only. "
        "Enforced and recorded, because this harness built the command line.",
    "SESSION_SUBAGENT":
        "one isolated subagent context per task, dispatched by the controlling session. "
        "Context isolation is real: no task sees another's context or output. A "
        "filesystem sandbox and a tool allow-list are NOT provable from here, because "
        "this harness did not build the delegate's environment. Reported as unrecorded.",
    "MANUAL":
        "none that this harness can establish. A human or an unknown agent produced the "
        "artifact; whether four readings were independent, what tools were used, and "
        "what else was read are all outside what any check here can see.",
    "UNAVAILABLE": "nothing was delegated.",
}

_MAY_PROVE_TOOL_POLICY = ("CLI_SUBPROCESS",)


def cli_available(command: str = "") -> tuple[bool, str]:
    """Is a reviewer CLI reachable from this process?"""
    if (command or "").strip():
        return True, "the operator configured a reviewer command"
    exe = shutil.which("claude")
    if exe:
        return True, f"found a reviewer CLI on PATH at {exe}"
    return False, "no reviewer command is configured and no `claude` is on PATH"


def resolve_model(override: str, declared_default: str) -> str:
    """An operator's model override, or the role's own declared default."""
    return (override or "").strip() or str(declared_default or "")


def resolve_reviewer_exe(explicit: str = "", cfg_exe: str = "") -> str:
    """An explicit override, else the operator's `SH_REVIEWER_EXE`, else `claude` on PATH."""
    return ((explicit or "").strip() or (cfg_exe or "").strip()
            or (shutil.which("claude") or ""))


def check_command(cmd: str, cmd_field: str) -> tuple[bool, str]:
    """The empty-command and {prompt}/{out}-placeholder checks every role's `available()`
    performs on whatever `resolve_cmd()` returned, once its own gate has already passed."""
    if not cmd:
        return False, (f"no reviewer is available: {cmd_field} is empty, SH_REVIEWER_EXE "
                       f"is empty and the `claude` CLI is not on PATH, so there is nothing "
                       f"to delegate to")
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, (f"{cmd_field} must contain both {{prompt}} and {{out}} "
                       f"placeholders; got: {cmd!r}")
    return True, ""


def modes_available(*, cli_command: str = "", cli_gate_open: bool = False,
                    session_can_delegate: bool = False) -> tuple[str, ...]:
    """Which delegation modes this environment offers, most capable first. Booleans and
    strings only — `session_can_delegate` is asserted by the CONTROLLER; nothing here
    detects it."""
    out = []
    if cli_gate_open and cli_available(cli_command)[0]:
        out.append("CLI_SUBPROCESS")
    if session_can_delegate:
        out.append("SESSION_SUBAGENT")
    out.append("MANUAL")
    return tuple(out)


def choose(available: tuple[str, ...] = (), *, prefer: str = "") -> str:
    """The mode to use, given what is available and what the operator asked for."""
    avail = tuple(m for m in available if m in WRITTEN_BY)
    if not avail:
        return "UNAVAILABLE"
    p = (prefer or "").strip().upper()
    if p in avail:
        return p
    for mode in ("CLI_SUBPROCESS", "SESSION_SUBAGENT", "MANUAL"):
        if mode in avail:
            return mode
    return "UNAVAILABLE"


def work_order(*, task: str, prompt_path: str, output_path: str, mode: str,
               schema_hint: str = "") -> dict:
    """What a delegate must be told, in a form any environment can fulfil. Deliberately a
    plain dict, not a validated one: `parse_*`/`accept_*` below validate the answer."""
    return {
        "task": task, "mode": mode, "read": prompt_path, "write": output_path,
        "isolation_required": ISOLATION_CLAIM.get(mode, ISOLATION_CLAIM["MANUAL"]),
        "must_not_read": [
            "any other task's prompt or output for this paper",
            "this harness's own source, tests or working contract",
            "any other paper's project directory",
            "any archived earlier review of the same paper",
        ],
        "output_contract": (schema_hint
                            or "exactly one JSON object, no markdown fence, no prose"),
        "validated_by": "harness.agent's role parsers, then a stage's own accept_* call — "
                        "a malformed answer is refused, not repaired",
    }


def provenance_record(*, mode: str, reviewer: str = "", tool_policy: str = "") -> dict:
    """The provenance fields an artifact must carry, with what it cannot prove removed.
    Only `CLI_SUBPROCESS` built the delegate's own command line, so only it may report an
    enforced tool policy; every other mode is forced to `unrecorded` HERE."""
    m = (mode or "").strip().upper()
    if m not in WRITTEN_BY:
        m = "MANUAL"
    policy = (tool_policy or "").strip() if m in _MAY_PROVE_TOOL_POLICY else ""
    return {
        "written_by": WRITTEN_BY[m], "delegation_mode": m,
        "reviewer": (reviewer or "").strip() or "unnamed",
        "tool_policy": policy or "unrecorded", "isolation_claim": ISOLATION_CLAIM[m],
        "tool_policy_provable": m in _MAY_PROVE_TOOL_POLICY,
    }


def mode_of(record: dict | None) -> str:
    """The mode an existing artifact was produced under, from its sidecar."""
    if not isinstance(record, dict):
        return "MANUAL"
    m = str(record.get("delegation_mode") or "").strip().upper()
    if m in WRITTEN_BY:
        return m
    by = str(record.get("written_by") or "").strip()
    for mode, token in WRITTEN_BY.items():
        if by == token:
            return mode
    return "MANUAL"


def summarise(records: list[dict]) -> dict:
    """How a set of artifacts was produced, mode by mode -- so a corpus can never be
    described as one homogeneous run when it is not."""
    counts: dict[str, int] = {}
    for rec in records or []:
        m = mode_of(rec)
        counts[m] = counts.get(m, 0) + 1
    present = sorted(counts)
    return {
        "by_mode": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "modes_present": present, "homogeneous": len(present) <= 1,
        "total": sum(counts.values()),
        "tool_policy_provable_for": counts.get("CLI_SUBPROCESS", 0),
    }


# =============================================================================================
# PART 2 -- SEALING  (was harness/sealing.py)
# =============================================================================================
def seal(path: Path, payload: dict, *, mode: str, reviewer: str = "",
         tool_policy: str = "unrecorded", extra: dict | None = None) -> dict:
    """Write `payload` as the artifact at `path`, then a `.driver.json` sidecar recording
    who produced it and a content hash a reader can re-check. `extra` is merged in AFTER
    the provenance fields, so a caller may override a provenance-derived field (grade
    forces `written_by: "grade_driver"` this way, exactly as it did as a standalone
    module)."""
    state.write_json(path, payload)
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    record = dict(provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy))
    record["content_sha256"] = content_sha256
    record["ts"] = state.now()
    record.update(extra or {})
    state.write_json(path.with_suffix(".driver.json"), record)
    return record


def verify_seal(path: Path, *, accepted_writers: tuple[str, ...]) -> tuple[bool, str]:
    """(ok, reason) -- the read-side hash+writer check every role's cache lookup shares.
    `_verify_and_read` below is the ONE call site every role's `load_*` uses; a caller
    needing more (a commit match, an independent-verification cross-check) layers it on
    top of what this returns, exactly as this function's own docstring in the reference
    implementation asked callers to."""
    if not path.exists():
        return False, "no output file"
    sidecar = path.with_suffix(".driver.json")
    if not sidecar.exists():
        return False, ("no provenance sidecar (.driver.json) -- most likely side-written "
                       "instead of produced through the validated staging path")
    try:
        rec = state.read_json(sidecar)
    except Exception:
        return False, "provenance sidecar is not valid JSON"
    if not isinstance(rec, dict) or rec.get("written_by") not in accepted_writers:
        return False, (f"provenance sidecar written_by="
                       f"{rec.get('written_by') if isinstance(rec, dict) else None!r} "
                       f"not recognized")
    want = rec.get("content_sha256")
    if not want:
        return False, "provenance sidecar has no content_sha256"
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        return False, "output file content changed after its provenance sidecar was written"
    return True, ""


def _verify_and_read(out: Path, writers: tuple[str, ...], *, prompt_sha256: str = ""
                     ) -> tuple[dict, dict] | None:
    """(sidecar_record, payload) or None -- the ONE `verify_seal` call site every role's
    `load_*` function uses (replacing five, including `verdict_driver.load_accepted`'s
    hand-rolled hash check, which never called `sealing.verify_seal` at all). A
    `prompt_sha256` mismatch is refused the same way on every role: absent on EITHER side
    is never treated as a mismatch, so a sidecar sealed before this field existed, or a
    caller not yet passing one, is not refused over a field that did not exist then.
    """
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
# PART 3 -- CLI MECHANICS  (was harness/reviewer_cli.py)
# =============================================================================================
class AgentError(RuntimeError):
    """A delegated call did not produce a usable result. Self-classifying: every instance
    carries `kind`/`retry`/`reset_hint` from `harness.failures.classify` over its own
    message, so a raise site never has to know which retry-policy bucket its text falls
    into. Replaces five near-identical per-driver error classes
    (`AuditDriverError`/`GradeDriverError`/`VerdictDriverError`/
    `ReimplementationDriverError`/`ArtifactReviewDriverError`) that differed only in name;
    a caller distinguishing roles catches `AgentError` and reads `.kind`/`.retry`, not the
    exception's class."""

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
    """Build the right exception class for a failure message. One classification point."""
    kind, retry, hint = classify_delegated_failure(message)
    cls = {"later": RateLimited, "never": NonRetryable}.get(retry, AgentError)
    return cls(message, kind=kind, retry=retry, reset_hint=hint)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the WHOLE process tree a `shell=True` command started, not just the shell."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


# Every tool this harness knows how to name, split by what granting it would let a
# delegated reviewer DO. A tool the CLI adds later is in neither tuple, so it is neither
# granted nor explicitly denied -- `--restricted` plus a `default` permission mode still
# refuses it.
CODE_RUNNING_TOOLS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit", "Task",
    "SlashCommand",
)
READING_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "WebFetch", "WebSearch")
KNOWN_TOOLS: tuple[str, ...] = CODE_RUNNING_TOOLS + READING_TOOLS


def denied_tools(allowed: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Every known tool this agent was NOT granted, named explicitly for
    `--disallowedTools`. Derived from the grant rather than written down beside it, so the
    two cannot disagree."""
    granted = {str(t).strip() for t in allowed if str(t).strip()}
    return tuple(t for t in KNOWN_TOOLS if t not in granted)


def pinned_settings_json(denied: tuple[str, ...] = ()) -> str:
    """The settings document `--settings` pins, as canonical bytes -- REPLACES the
    operator's ambient settings rather than merging with them (a permission mode, a
    default model, hooks, plugins, MCP servers, a user-level `CLAUDE.md`)."""
    doc = {
        "$comment": ("single-harness delegated review: the operator's ambient settings are "
                     "REPLACED, not merged. See harness.agent.pinned_settings_json."),
        "enableAllProjectMcpServers": False, "enabledPlugins": {}, "hooks": {},
        "permissions": {"additionalDirectories": [], "allow": [], "defaultMode": "default",
                        "deny": list(denied)},
    }
    return json.dumps(doc, indent=2, sort_keys=True)


@dataclasses.dataclass(frozen=True)
class Confinement:
    """What was passed to ONE delegated agent, as facts about flags -- not a two-valued
    label computed from which branch of an `if` the caller happened to take."""

    role: str
    template: str = "built_in"                    # built_in | operator_supplied
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    add_dir: tuple[str, ...] = ()
    model: str = ""
    restricted: bool = False
    strict_mcp: bool = False
    bare: bool = False
    settings_sha256: str = ""
    output_format: str = ""

    @property
    def enforced(self) -> bool:
        """True only when THIS module built the argv. An operator command wins
        unconditionally and may be anything; it is recorded as unenforced rather than
        assumed to match."""
        return self.template == "built_in"

    def flags(self, settings_path: str = "") -> str:
        if not self.enforced:
            return ""
        out = ""
        if self.output_format:
            out += f" --output-format {self.output_format}"
        out += ' --allowedTools "{}"'.format(",".join(self.allowed_tools))
        if self.disallowed_tools:
            out += ' --disallowedTools "{}"'.format(",".join(self.disallowed_tools))
        if self.model:
            out += f" --model {self.model}"
        if self.restricted:
            out += " --restricted"
        if self.strict_mcp:
            out += " --strict-mcp-config"
        if self.bare:
            out += " --bare"
        if settings_path:
            out += f' --settings "{settings_path}"'
        for d in self.add_dir:
            out += f' --add-dir "{d}"'
        return out

    def policy(self) -> dict:
        return {
            "role": self.role, "template": self.template, "enforced": self.enforced,
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "add_dir": list(self.add_dir), "model_requested": self.model,
            "restricted": self.restricted, "strict_mcp": self.strict_mcp,
            "bare": self.bare, "settings_sha256": self.settings_sha256,
            "output_format": self.output_format,
        }

    def summary(self) -> str:
        if not self.enforced:
            return ("unrecorded (operator-supplied command line; this module did not build "
                    "the argv and cannot characterise its confinement)")
        bits = [f"tools={','.join(self.allowed_tools) or 'none'}",
                f"denied={len(self.disallowed_tools)}", f"add_dir={len(self.add_dir)}"]
        for name, on in (("restricted", self.restricted), ("strict_mcp", self.strict_mcp),
                         ("bare", self.bare), ("settings", bool(self.settings_sha256))):
            if on:
                bits.append(name)
        return "enforced: " + " ".join(bits)


def operator_confinement(role: str) -> Confinement:
    """The honest record for a command line this module did not build."""
    return Confinement(role=role, template="operator_supplied")


def write_pinned_settings(directory: Path, denied: tuple[str, ...]) -> tuple[Path, str]:
    """Materialise the pinned settings document OUTSIDE the reviewer's cwd. Returns
    (path, sha256)."""
    directory.mkdir(parents=True, exist_ok=True)
    body = pinned_settings_json(denied)
    path = directory / "settings.pinned.json"
    path.write_text(body, encoding="utf-8")
    return path, hashlib.sha256(body.encode("utf-8")).hexdigest()


def prompt_fingerprint(prompt: Path) -> str:
    """sha256 of the prompt bytes on disk, or '' if it cannot be read."""
    try:
        return hashlib.sha256(prompt.read_bytes()).hexdigest()
    except OSError:
        return ""


def keep_prompt_copy(prompt: Path, sha256: str) -> Path | None:
    """Keep a content-addressed copy beside the regenerated prompt. Best effort."""
    if not sha256:
        return None
    try:
        copy = prompt.with_name(f"{prompt.stem}.{sha256[:12]}{prompt.suffix or '.md'}")
        if not copy.exists():
            copy.write_bytes(prompt.read_bytes())
        return copy
    except OSError:
        return None


def prompt_is_unchanged(sidecar: Path, prompt: Path) -> tuple[bool, str]:
    """Does the prompt on disk still hash to what the sidecar says produced this output?
    A sidecar with no `prompt_sha256` reports `unpinned` rather than a mismatch -- it was
    written before this field existed."""
    try:
        rec = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "the provenance sidecar could not be read"
    if not isinstance(rec, dict):
        return False, "the provenance sidecar is not an object"
    recorded = str(rec.get("prompt_sha256") or "")
    if not recorded:
        return True, "unpinned (written before the prompt was fingerprinted)"
    actual = prompt_fingerprint(prompt)
    if not actual:
        return False, f"the prompt is gone; this output was produced against {recorded[:12]}"
    if actual != recorded:
        return False, (f"the prompt changed after this output was sealed: "
                       f"{recorded[:12]} produced it, {actual[:12]} is on disk now")
    return True, ""


_ENVELOPE_MARKERS = ("type", "subtype", "is_error", "session_id", "sessionId",
                     "total_cost_usd", "duration_ms", "num_turns")


def unwrap_envelope(text: str) -> tuple[str, dict]:
    """(payload, envelope) -- the CLI's `--output-format json` result wrapper unwrapped,
    or (text, {}). A lens report is recognised by carrying `findings` and returned
    untouched even if it also happens to carry an envelope-looking key."""
    raw = (text or "").strip()
    if not raw:
        return raw, {}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return raw, {}
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return raw, {}
    if not isinstance(data, dict) or "findings" in data:
        return raw, {}
    if not isinstance(data.get("result"), str):
        return raw, {}
    if not any(m in data for m in _ENVELOPE_MARKERS):
        return raw, {}
    return str(data["result"]).strip(), data


def envelope_provenance(env: dict) -> dict:
    """What the CLI itself reported about the call it just made. `envelope_keys` is
    recorded verbatim so a blank field is distinguishable from a key this module guessed
    wrong."""
    if not isinstance(env, dict):
        return {}
    model = str(env.get("model") or "")
    usage_by_model = env.get("modelUsage")
    if not model and isinstance(usage_by_model, dict):
        names = sorted(str(k) for k in usage_by_model)
        model = names[0] if len(names) == 1 else ",".join(names)
    cost = env.get("total_cost_usd")
    turns = env.get("num_turns")
    return {
        "model_reported": model,
        "cli_version": str(env.get("version") or env.get("cli_version") or ""),
        "session_id": str(env.get("session_id") or env.get("sessionId") or ""),
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "num_turns": turns if isinstance(turns, int) else None,
        "usage": env.get("usage") if isinstance(env.get("usage"), dict) else {},
        "is_error": bool(env.get("is_error")),
        "envelope_keys": sorted(str(k) for k in env),
    }


_DELEGATION_RULES: tuple[tuple[str, str, str], ...] = (
    (r"requested permissions? to use|permission to use the\b|tool use (was )?(denied|blocked)"
     r"|not allowed to use|disallowed[_ ]tools?|is not in the allowed tools"
     r"|tool .{0,24} is not permitted", "bad_invocation", "never"),
    (r"model[^\n]{0,60}\b(is )?(not available|not found|unavailable|not enabled)"
     r"|does not have access to (the )?model|invalid model|unknown model|model_not_found"
     r"|not entitled to", "bad_invocation", "never"),
)
_DELEGATION_COMPILED = tuple((re.compile(p, re.I), k, r) for p, k, r in _DELEGATION_RULES)
_HARNESS_GATE_RE = re.compile(r"gate (is )?(closed|shut)|set SH_[A-Z_]+", re.I)


def classify_delegated_failure(text: str) -> tuple[str, str, str]:
    """(kind, retry, reset_hint) for a delegated reviewer's OWN words -- delegation-specific
    rules first, then `harness.failures.classify`, then one narrowing: a `gate_closed`
    verdict that does not actually name a gate falls back to `unknown`/`now`."""
    t = str(text or "")
    for rx, kind, policy in _DELEGATION_COMPILED:
        if rx.search(t):
            return kind, policy, ""
    kind, policy, hint = failures.classify(t)
    if kind == "gate_closed" and not _HARNESS_GATE_RE.search(t):
        return "unknown", "now", ""
    return kind, policy, hint


def stage_settings(conf: Confinement, policy_dir: Path) -> tuple[Confinement, str]:
    """Write `conf`'s pinned settings into `policy_dir` if `conf` is enforced. Returns
    `(conf, "")` unchanged for an operator-supplied command line. Raises OSError on write
    failure."""
    if not conf.enforced:
        return conf, ""
    sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
    return dataclasses.replace(conf, settings_sha256=sha), str(sp)


def spawn_and_wait(cmd: str, cwd: Path, timeout_s: float
                   ) -> tuple[subprocess.CompletedProcess | None, bool]:
    """Run ONE confined shell command to completion or timeout. Returns (result,
    timed_out). Own process group so a timeout can kill the WHOLE tree."""
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True})
    proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", **group_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return None, True
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr), False


def cli_pipe_command(exe: str, extra_flags: str) -> str:
    """The one-shot piped invocation template every built-in role command shares. The
    prompt is piped rather than interpolated because it carries untrusted paper text."""
    reader = "type" if os.name == "nt" else "cat"
    return f'{reader} "{{prompt}}" | "{exe}" -p{extra_flags} > "{{out}}"'


def _spawn_and_classify(cmd: str, cwd: Path, out: Path, timeout_s: float
                        ) -> subprocess.CompletedProcess:
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
    "reimplement": RoleSpec(
        key="reimplement", allow_attr="allow_reimplementation_driver",
        cmd_attr="reimplementation_cmd", model_attr="reimplementation_model",
        timeout_attr="reimplementation_timeout_s", cmd_env_name="SH_REIMPLEMENTATION_CMD",
        gate_closed_message="reimplementation-driver gate is closed; set SH_ALLOW_REIMPLEMENTATION_DRIVER=1",
        writers=("reimplement_driver", "audit_driver", "session_subagent", "manual_accept"),
        role_spec=REIMPLEMENT_P.ROLE_SPEC),
    "artifact_review": RoleSpec(
        key="artifact_review", allow_attr="allow_artifact_review",
        cmd_attr="artifact_review_cmd", model_attr="artifact_review_model",
        timeout_attr="artifact_review_timeout_s", cmd_env_name="SH_ARTIFACT_REVIEW_CMD",
        gate_closed_message=(
            "authors'-code audit gate is closed: SH_ALLOW_ARTIFACT_REVIEW is not set. "
            "With it closed the artifact route still runs on deterministic probes alone"),
        writers=("artifact_review_driver", "audit_driver", "session_subagent",
                "manual_accept"),
        role_spec=ARTIFACT_P.ROLE_SPEC),
}

_OPERATOR_LABEL = {"grade": "grader", "verdict": "assessor", "reimplement": "reimplementer",
                   "artifact_review": "authors'-code auditor"}
_BLIND_ROLE_LABEL = {"grade": "grader", "verdict": "assessor", "reimplement": "reimplementer"}


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


def _artifact_review_confinement(cfg: Config, repo_dir: str = "", *,
                                 settings_sha256: str = "") -> Confinement:
    """The authors'-code auditor's confinement: `Read`+`Grep` over the checkout and
    nothing else -- no execution, by construction. Tools are READ FROM
    `prompts.artifact_review.ROLE_SPEC`, never hardcoded (the 2026-09-19 fix this
    consolidation must not regress)."""
    allowed = tuple(ARTIFACT_P.ROLE_SPEC.get("tools", ("Read", "Grep")))
    return Confinement(
        role="artifact_review", allowed_tools=allowed, disallowed_tools=denied_tools(allowed),
        add_dir=(repo_dir,) if repo_dir else (), model=role_model(cfg, "artifact_review"),
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
    if role_key == "artifact_review":
        return _artifact_review_confinement(cfg, kw.get("repo_dir", ""),
                                            settings_sha256=settings_sha256)
    # grade / verdict / reimplement: zero tools, `--bare`, no directories -- read from
    # each role's own ROLE_SPEC, never hardcoded (grade's blinding, verdict's/reimplement's
    # "everything it needs is in the prompt" reasoning).
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
    used by every best-effort, single-artifact role (verdict, reimplement's two calls,
    artifact_review); each of those previously reimplemented this exact shape on its own.
    """
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
HARNESS_OWNED_REPORT_KEYS: tuple[str, ...] = ("merged_duplicates",)
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
# PART 5d -- REIMPLEMENT ROLE  (governed reconstruction writer; was reimplement_driver.py)
# =============================================================================================
REQUIRED_KINDS = ("method", "training", "dataset", "metric", "comparison_target")
_LINE_REF = re.compile(r"^lines?\s+(\d+)(?:\s*[-:]\s*(\d+))?$", re.I)
_FUNCTION_REF = re.compile(r"^(?:function|def)\s+([A-Za-z_]\w*)$", re.I)


def _implementation_locator_matches(script: str, impl_ref: str, impl_quote: str) -> bool:
    """Verify the quote at the claimed implementation locator, not anywhere in the file."""
    if not impl_ref or not impl_quote:
        return False
    lines = script.splitlines()
    m = _LINE_REF.fullmatch(impl_ref.strip())
    if m:
        start = int(m.group(1))
        end = int(m.group(2) or start)
        if start < 1 or end < start or end > len(lines):
            return False
        return impl_quote in "\n".join(lines[start - 1:end])
    m = _FUNCTION_REF.fullmatch(impl_ref.strip())
    if m:
        try:
            import ast
            tree = ast.parse(script)
            node = next((n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name == m.group(1)), None)
            if node is None:
                return False
            end = int(getattr(node, "end_lineno", node.lineno))
            return impl_quote in "\n".join(lines[node.lineno - 1:end])
        except (SyntaxError, StopIteration):
            return False
    return False


def ingredients_table(readiness: ReimplementationReadiness) -> str:
    rows = ["| ingredient | found at | the paper's own words |", "|---|---|---|"]
    for i in readiness.ingredients:
        if not i.required:
            continue
        quote = (i.quote or "").replace("|", "\\|")[:200]
        rows.append(f"| {i.kind} | {('`' + i.ref + '`') if i.ref else '**MISSING**'} | {quote} |")
    return "\n".join(rows)


def build_reimplementation_brief(readiness: ReimplementationReadiness, *, paper_title: str = "",
                                 claim: str = "", table_ref: str = "",
                                 claimed_cell_value: str = "", paper_text: str = "") -> str:
    return REIMPLEMENT_P.build(paper_title, claim, table_ref, claimed_cell_value,
                              ingredients_table(readiness), paper_text)


def _verification_brief(readiness: ReimplementationReadiness, script: str,
                        bindings: list[dict]) -> str:
    return f"""{REIMPLEMENT_P.SECURITY}

You are the independent verifier, not the generator. Check the proposed reconstruction
against every paper-owned required ingredient below. Reject any invented method,
training, dataset, metric, or comparison detail, and reject a locator whose quoted text
is not present at that exact implementation location.

=== PAPER INGREDIENTS ===
{ingredients_table(readiness)}

=== PROPOSED SCRIPT ===
```python
{script}
```

=== PROPOSED BINDINGS ===
{json.dumps(bindings, ensure_ascii=False)}

Print only JSON:
{{"approved": true_or_false,
 "approved_kinds": ["method","training","dataset","metric","comparison_target"],
 "notes": "short reason"}}
"""


def _parse_verification(text: str) -> tuple[bool, str]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return False, "verifier returned no JSON object"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return False, f"verifier JSON invalid: {e}"
    kinds = {str(x) for x in (data.get("approved_kinds") or [])}
    approved = bool(data.get("approved")) and kinds == set(REQUIRED_KINDS)
    return approved, str(data.get("notes") or "")


def parse_reimplementation_report(text: str) -> tuple[str, list[dict], str, dict]:
    """(script, bindings, notes, meta). Unknown keys dropped, as every role does."""
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {}}
    if envelope and envelope.get("is_error"):
        raise AgentError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("script") or "").strip():
        raise AgentError("output JSON has no non-empty 'script'")
    bindings = data.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
    clean_bindings = [b for b in bindings if isinstance(b, dict) and b.get("kind")]
    return str(data["script"]), clean_bindings, str(data.get("notes") or ""), meta


def conformance(readiness: ReimplementationReadiness, script: str, bindings: list[dict], *,
                generated_by: str = "", verified_by: str = "") -> ReimplementationConformance:
    """Pure. Pairs each REQUIRED ingredient's PAPER locator with the driver's
    IMPLEMENTATION locator, and verifies `impl_quote` occurs verbatim in `script` -- this
    module's own check, never the delegate's own say-so."""
    by_kind = {i.kind: i for i in readiness.ingredients}
    driver_by_kind = {b.get("kind"): b for b in bindings}
    out: list[ReimplementationBinding] = []
    unbound: list[str] = []
    for kind in REQUIRED_KINDS:
        paper = by_kind.get(kind)
        paper_ref = paper.ref if paper else ""
        paper_quote = paper.quote if paper else ""
        d = driver_by_kind.get(kind) or {}
        impl_ref = str(d.get("impl_ref") or "").strip()
        impl_quote = str(d.get("impl_quote") or "").strip()
        verified = _implementation_locator_matches(script, impl_ref, impl_quote)
        bound = bool(paper_ref) and bool(impl_ref) and verified
        if not bound:
            unbound.append(kind)
        out.append(ReimplementationBinding(
            kind=kind, paper_ref=paper_ref, paper_quote=paper_quote,
            impl_ref=impl_ref, impl_quote=impl_quote, verified=verified, bound=bound))
    generator = (generated_by or "").strip()
    verifier = (verified_by or "").strip()
    independent = bool(generator and verifier and generator != verifier)
    established = not unbound and independent
    if established:
        reason = ("every required ingredient is bound to both a paper locator and a "
                 "verified implementation locator, and a separately attributed verifier "
                 "approved the proposed bindings; this reconstruction may reconcile "
                 "against the cited cell")
    elif not unbound and not independent:
        reason = ("all proposed snippets are present, but generated code cannot certify "
                 "its own scientific conformance. A separately attributed verifier must "
                 "approve the method, training, dataset, metric and comparison bindings")
    else:
        reason = ("not bound: " + ", ".join(unbound) + " -- the returned script does not "
                 "verifiably realize " + ("this ingredient" if len(unbound) == 1 else
                                          "these ingredients") + ", so no verdict may be "
                 "drawn from running it")
    return ReimplementationConformance(
        established=established, bindings=out, unbound=unbound, reason=reason,
        generated_by=generator, verified_by=verifier, independently_verified=independent)


def _reimpl_paths(cfg: Config, pid: str, target_id: str) -> tuple[Path, Path]:
    out = (state.project_dir(cfg, pid) / "runs" / pid / "reimplementation" /
          f"{target_id or 'default'}.json")
    return out, out.with_suffix(".driver.json")


def _seal_reimplementation(cfg: Config, pid: str, target_id: str, script: str,
                          conf: ReimplementationConformance, record: dict) -> dict:
    out, _sidecar = _reimpl_paths(cfg, pid, target_id)
    return _seal_role(out, {"script": script, "conformance": conf.model_dump()},
                      {**record, "paper_id": pid, "target_id": target_id,
                       "established": conf.established})


def accept_reimplementation(cfg: Config, pid: str, target_id: str, raw: str,
                           readiness: ReimplementationReadiness, *, reviewer: str = "",
                           generated_by: str = "", tool_policy: str = "unrecorded",
                           mode: str = "MANUAL", prompt_sha256: str = "") -> dict:
    """Seal a reconstruction produced OUTSIDE this module's own subprocess. `prompt_sha256`
    is now an available parameter here too -- see the module docstring, point 3."""
    script, bindings, _notes, _meta = parse_reimplementation_report(raw)
    conf = conformance(readiness, script, bindings, generated_by=generated_by,
                      verified_by=reviewer)
    prov = provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy)
    record = {"written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
             "reviewer": prov["reviewer"], "generated_by": generated_by,
             "independent_verification": conf.independently_verified,
             "tool_policy": prov["tool_policy"], "isolation_claim": prov["isolation_claim"],
             "tool_policy_provable": prov["tool_policy_provable"]}
    if prompt_sha256:
        record["prompt_sha256"] = prompt_sha256
    return _seal_reimplementation(cfg, pid, target_id, script, conf, record)


def load_reimplementation(cfg: Config, pid: str, target_id: str, *, prompt_sha256: str = ""
                         ) -> tuple[str, ReimplementationConformance] | None:
    out, _sidecar = _reimpl_paths(cfg, pid, target_id)
    got = _verify_and_read(out, ROLES["reimplement"].writers, prompt_sha256=prompt_sha256)
    if got is None:
        return None
    rec, data = got
    try:
        conf = ReimplementationConformance(**(data.get("conformance") or {}))
    except Exception:
        return None
    if conf.established and not (rec.get("independent_verification") and conf.independently_verified
                                and conf.generated_by and conf.verified_by
                                and conf.generated_by != conf.verified_by):
        return None
    return str(data.get("script") or ""), conf


def run_reimplementation(cfg: Config, prompt_text: str, readiness: ReimplementationReadiness,
                        *, timeout_s: int = 0, pid: str = "",
                        target_id: str = "") -> tuple[str, ReimplementationConformance] | None:
    """Best-effort, TWO calls: a generator and a separately-attributed verifier. Returns
    None on ANY failure of the generator call; a failed verifier call still produces a
    (script, conformance) with `established=False`, honestly."""
    ok, _why = available(cfg, "reimplement")
    if not ok:
        return None
    timeout_s = timeout_s or cfg.reimplementation_timeout_s
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    try:
        raw, gen_record = _invoke_once(cfg, "reimplement", prompt_text, timeout_s)
    except AgentError:
        return None
    try:
        script, bindings, notes, meta = parse_reimplementation_report(raw)
    except AgentError:
        return None

    approved, verifier_note, verify_record = False, "independent verifier did not complete", {}
    try:
        verify_raw, verify_record = _invoke_once(
            cfg, "reimplement", _verification_brief(readiness, script, bindings), timeout_s)
        approved, verifier_note = _parse_verification(verify_raw)
    except AgentError:
        pass
    conf = conformance(readiness, script, bindings, generated_by="reimplementation_generator",
                      verified_by=("reimplementation_verifier" if approved else ""))
    if pid:
        try:
            _seal_reimplementation(cfg, pid, target_id, script, conf, {
                "written_by": "reimplement_driver", "delegation_mode": "CLI_SUBPROCESS",
                "reviewer": "reimplementation_verifier" if approved else "",
                "generated_by": "reimplementation_generator",
                "independent_verification": conf.independently_verified,
                "generator": gen_record, "verifier": verify_record,
                "seconds": round(time.time() - started, 1), "prompt_sha256": prompt_sha,
                "notes": notes, "verifier_notes": verifier_note,
                "envelope": meta.get("envelope") or {}})
        except OSError:
            pass
    return script, conf


# =============================================================================================
# PART 5e -- ARTIFACT_REVIEW ROLE  (authors'-code auditor; was artifact_review_driver.py)
# =============================================================================================
_PROPOSAL_KEYS = ("kind", "title", "statement", "file", "code_quote", "paper_quote",
                 "paper_value", "artifact_value", "experiment_id", "identity_basis",
                 "identity_file", "identity_quote", "config_key", "counter_explanations")
HARNESS_OWNED_FACT_KEYS = ("authority", "refusal", "span", "snapshot", "paper_ref",
                          "fact_id", "probe", "settles", "identity_state", "identity_span")


def role_model_artifact_review(cfg: Config) -> str:
    return role_model(cfg, "artifact_review")


def _basis(value) -> str:
    """The auditor's claimed identity basis, or `auditor_assertion`. Never a sixth value."""
    got = str(value or "").strip().lower()
    return got if got in ARTIFACT_IDENTITY_BASES else "auditor_assertion"


def _reread_config(root: str | Path, rel_file: str, key: str, claimed: str) -> tuple[str, str]:
    """(the value the FILE states for `key`, how it was read). Never the auditor's copy.
    AMBIGUITY REFUSES rather than picks."""
    if not (rel_file and key):
        return claimed, "as the auditor reported it; no key was named to re-read"
    for reader, how in ((artifact_evidence.config_values, "re-read from the file as a "
                                                         "key/value line"),
                       (artifact_evidence.argparse_default, "re-read from the file as an "
                                                            "argparse default")):
        hits = reader(root, rel_file, key)
        if len(hits) == 1:
            return hits[0][0], how
        if len(hits) > 1:
            return claimed, (f"NOT re-read: `{key}` is set in {len(hits)} places in "
                            f"`{rel_file}`, so which one the paper means is not decidable "
                            f"from the file; the auditor's value is kept and the ambiguity "
                            f"is recorded")
    return claimed, f"NOT re-read: `{key}` was not found in `{rel_file}`"


def parse_concerns(text: str) -> tuple[list[dict], str, dict]:
    """(proposals, notes, meta). Proposals only -- nothing here carries an `authority` or a
    `span` until `locate_all` has been through it."""
    outer = (text or "").strip()
    if not outer:
        raise AgentError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise AgentError(f"the reader reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("concerns"), list):
        raise AgentError("output JSON has no 'concerns' list")
    out: list[dict] = []
    for row in data["concerns"]:
        if not isinstance(row, dict):
            meta["unknown_keys_dropped"] += 1
            continue
        for key in HARNESS_OWNED_FACT_KEYS:
            if key in row:
                meta["harness_keys_stripped"] += 1
        meta["unknown_keys_dropped"] += sum(
            1 for k in row if k not in _PROPOSAL_KEYS and k not in HARNESS_OWNED_FACT_KEYS)
        kind = str(row.get("kind") or "").strip().upper()
        out.append({
            "kind": kind if kind in ARTIFACT_P.CONCERN_KINDS else "SUSPICIOUS_IMPLEMENTATION",
            "title": str(row.get("title") or "").strip()[:120],
            "statement": str(row.get("statement") or "").strip()[:1200],
            "file": str(row.get("file") or "").strip(),
            "code_quote": str(row.get("code_quote") or "").strip(),
            "paper_quote": str(row.get("paper_quote") or "").strip(),
            "paper_value": str(row.get("paper_value") or "").strip()[:80],
            "artifact_value": str(row.get("artifact_value") or "").strip()[:80],
            "experiment_id": str(row.get("experiment_id") or "").strip()[:300],
            "identity_basis": _basis(row.get("identity_basis")),
            "identity_file": str(row.get("identity_file") or "").strip(),
            "identity_quote": str(row.get("identity_quote") or "").strip(),
            "config_key": str(row.get("config_key") or "").strip()[:120],
            "counter_explanations": [str(x).strip()[:300] for x in
                                    (row.get("counter_explanations") or [])
                                    if str(x).strip()][:4],
        })
    return out, str(data.get("notes") or "").strip()[:2000], meta


def locate_all(doc: PaperDoc, root: str | Path, snap: ArtifactSnapshot,
              proposals: list[dict]) -> tuple[list[ArtifactFact], dict]:
    """Relocate every proposal's code citation, then try to bind the ones claiming a
    mismatch. A proposal that fails relocation is DROPPED, not softened."""
    facts: list[ArtifactFact] = []
    meta = {"proposed": len(proposals), "relocated": 0, "dropped_unlocatable": 0,
           "paper_citations_relocated": 0, "bound_mismatches": 0,
           "endpoint_concerns": 0, "reread_from_file": 0,
           "identity": {k: 0 for k in ARTIFACT_IDENTITY_STATES}}
    for i, row in enumerate(proposals):
        span = artifact_evidence.relocate(root, row["file"], row["code_quote"])
        if span is None:
            meta["dropped_unlocatable"] += 1
            continue
        meta["relocated"] += 1
        fid = f"AF{i + 1:02d}"
        if row["paper_quote"] and row["paper_value"] and row["artifact_value"]:
            value, how = _reread_config(root, row["file"], row.get("config_key", ""),
                                       row["artifact_value"])
            if value != row["artifact_value"] or "re-read from" in how:
                meta["reread_from_file"] += 1
            fact = artifact_evidence.bind_mismatch(
                doc, snap, paper_quote=row["paper_quote"], paper_value=row["paper_value"],
                span=span, artifact_value=value, root=root,
                experiment_id=row["experiment_id"], probe=f"code_review:{row['kind']}",
                identity_basis=row.get("identity_basis", ""),
                identity_file=row.get("identity_file", ""),
                identity_quote=row.get("identity_quote", ""),
                counter_explanations=row["counter_explanations"] + [how])
            meta["identity"][fact.identity_state] = \
                meta["identity"].get(fact.identity_state, 0) + 1
            if fact.paper_ref:
                meta["paper_citations_relocated"] += 1
            if fact.authority == "PAPER_ARTIFACT_MISMATCH":
                meta["bound_mismatches"] += 1
            elif fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN":
                meta["endpoint_concerns"] += 1
            facts.append(fact.model_copy(update={"fact_id": fid}))
            continue
        facts.append(ArtifactFact(
            fact_id=fid, probe=f"code_review:{row['kind']}", snapshot=snap, span=span,
            authority="ARTIFACT_FACT" if snap.audited else "NONE",
            artifact_value=row["artifact_value"],
            counter_explanations=row["counter_explanations"],
            statement=(f"`{span.file}:{span.line}` in the checkout at "
                      f"{snap.commit[:10] or '(unpinned)'} contains the quoted code. A "
                      f"reader's account of what that means: {row['statement'][:400]} "
                      f"-- UNVERIFIED; the harness established the location, not the "
                      f"reading.")))
    return facts, meta


def _artifact_paths(cfg: Config, pid: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "artifact" / f"{pid}.inspection.json"
    return out, out.with_suffix(".driver.json")


def seal_artifact_inspection(cfg: Config, pid: str, inspection: ArtifactInspection,
                            record: dict) -> dict:
    out, _sidecar = _artifact_paths(cfg, pid)
    return _seal_role(out, inspection.model_dump(),
                      {**record, "paper_id": pid, "proposed": inspection.proposed,
                       "relocated": inspection.relocated, "discharged": inspection.discharged})


def accept_artifact_review(cfg: Config, pid: str, doc: PaperDoc, root: str | Path, raw: str, *,
                          url: str = "", statements: list[str] | None = None,
                          reader: str = "", tool_policy: str = "unrecorded",
                          mode: str = "MANUAL", facts: list[ArtifactFact] | None = None,
                          prompt_sha256: str = "") -> ArtifactInspection:
    """Relocate and seal a reading produced OUTSIDE this module's own subprocess.
    `prompt_sha256` is now an available parameter here too -- see module docstring, point 3.
    """
    proposals, notes, meta = parse_concerns(raw)
    snap = artifact_evidence.snapshot(root, url)
    located, lmeta = locate_all(doc, root, snap, proposals)
    inspection = artifact_evidence.inspect(doc, root, url=url, statements=statements,
                                          facts=list(facts or []) + located)
    inspection = inspection.model_copy(
        update={"proposed": lmeta["proposed"], "relocated": lmeta["relocated"]})
    prov = provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    record = {"written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
             "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
             "isolation_claim": prov["isolation_claim"],
             "tool_policy_provable": prov["tool_policy_provable"],
             "notes": notes, **lmeta,
             "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
             "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0)}
    if prompt_sha256:
        record["prompt_sha256"] = prompt_sha256
    seal_artifact_inspection(cfg, pid, inspection, record)
    return inspection


def load_artifact_inspection(cfg: Config, pid: str, *, commit: str = "",
                            prompt_sha256: str = "") -> ArtifactInspection | None:
    out, _sidecar = _artifact_paths(cfg, pid)
    got = _verify_and_read(out, ROLES["artifact_review"].writers, prompt_sha256=prompt_sha256)
    if got is None:
        return None
    _rec, data = got
    try:
        inspection = ArtifactInspection(**data)
    except Exception:
        return None
    if commit and (inspection.snapshot is None or inspection.snapshot.commit != commit):
        return None
    return inspection


def _record_artifact_failure(cfg: Config, pid: str, why: str, **extra) -> None:
    """Why the auditor produced nothing -- a silent None is not an acceptable record."""
    try:
        _out, sidecar = _artifact_paths(cfg, pid)
        state.write_json(sidecar, {"written_by": "artifact_review_driver", "paper_id": pid,
                                  "ran": False, "failure": why, "ts": state.now(), **extra})
    except OSError:
        pass


def run_artifact_review(cfg: Config, pid: str, doc: PaperDoc, root: str | Path,
                       prompt_text: str, *, url: str = "", statements: list[str] | None = None,
                       facts: list[ArtifactFact] | None = None) -> ArtifactInspection | None:
    """One best-effort call. Returns None on ANY failure, and never raises -- every failure
    path records WHY in the sidecar first, so "found nothing" and "never answered" stay
    distinguishable afterwards."""
    ok, why = available(cfg, "artifact_review")
    if not ok:
        _record_artifact_failure(cfg, pid, why)
        return None
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    try:
        raw, rec0 = _invoke_once(cfg, "artifact_review", prompt_text,
                                cfg.artifact_review_timeout_s, repo_dir=str(root))
    except AgentError as e:
        _record_artifact_failure(cfg, pid, f"the reader could not be started or did not "
                                          f"answer: {e}",
                                seconds=round(time.time() - started, 1))
        return None
    try:
        proposals, notes, meta = parse_concerns(raw)
    except AgentError as e:
        _record_artifact_failure(cfg, pid, f"the reader's output was unusable: {e}",
                                command=rec0.get("command", ""), raw_head=raw[:1500],
                                seconds=round(time.time() - started, 1))
        return None
    snap = artifact_evidence.snapshot(root, url)
    located, lmeta = locate_all(doc, root, snap, proposals)
    inspection = artifact_evidence.inspect(
        doc, root, url=url, statements=statements, facts=list(facts or []) + located
    ).model_copy(update={"proposed": lmeta["proposed"], "relocated": lmeta["relocated"]})
    try:
        seal_artifact_inspection(cfg, pid, inspection, {
            "written_by": "artifact_review_driver", "reader": "",
            "delegation_mode": "CLI_SUBPROCESS", **rec0,
            "seconds": round(time.time() - started, 1), "prompt_sha256": prompt_sha,
            "notes": notes, "envelope": meta.get("envelope") or {}, **lmeta,
            "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
            "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0)})
    except OSError:
        pass
    return inspection


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
    assert available(Config(allow_reimplementation_driver=False), "reimplement")[0] is False
    assert available(Config(allow_artifact_review=False), "artifact_review")[0] is False
    for _role, _msg_part in (("lens", "SH_ALLOW_AUTO_AUDIT"), ("grade", "SH_ALLOW_GRADING"),
                             ("verdict", "SH_ALLOW_SUBSTANTIVE_VERDICT"),
                             ("reimplement", "SH_ALLOW_REIMPLEMENTATION_DRIVER"),
                             ("artifact_review", "SH_ALLOW_ARTIFACT_REVIEW")):
        assert _msg_part in ROLES[_role].gate_closed_message, _role

    # --- confinement differs correctly per role, driven from an injected exe -------------
    _EXE = "/nonexistent/claude"
    lens_cmd = default_cmd(Config(), "lens", exe=_EXE, settings="/s.json",
                          lens="overclaim", pdf_dir="/papers")
    grade_cmd = default_cmd(Config(), "grade", exe=_EXE, settings="/s.json")
    verdict_cmd = default_cmd(Config(), "verdict", exe=_EXE, settings="/s.json")
    reimpl_cmd = default_cmd(Config(), "reimplement", exe=_EXE, settings="/s.json")
    artifact_cmd = default_cmd(Config(), "artifact_review", exe=_EXE, settings="/s.json",
                              repo_dir="/repo")

    assert '--allowedTools "Read"' in lens_cmd, lens_cmd
    assert '--allowedTools ""' in grade_cmd, grade_cmd
    assert '--allowedTools ""' in verdict_cmd, verdict_cmd
    assert '--allowedTools ""' in reimpl_cmd, reimpl_cmd
    assert '--allowedTools "Read,Grep"' in artifact_cmd, artifact_cmd

    assert "--bare" not in lens_cmd, "a lens legitimately needs Read"
    assert "--bare" in grade_cmd and "--bare" in verdict_cmd and "--bare" in reimpl_cmd
    assert "--bare" not in artifact_cmd, "the code auditor needs Read/Grep, like a lens"
    assert "--add-dir" in lens_cmd and "/papers" in lens_cmd
    assert "--add-dir" in artifact_cmd and "/repo" in artifact_cmd
    for _c in (grade_cmd, verdict_cmd, reimpl_cmd):
        assert "--add-dir" not in _c, _c
    for _c in (lens_cmd, grade_cmd, verdict_cmd, reimpl_cmd, artifact_cmd):
        assert "{prompt}" in _c and "{out}" in _c and "--model" in _c and "--restricted" in _c
        assert "--strict-mcp-config" in _c and "--output-format json" in _c

    # --- ROLE_SPEC is READ, never hardcoded -- the 2026-09-19 fix this must preserve -----
    for _role_key, _module in (("grade", GRADE_P), ("verdict", VERDICT_P),
                               ("reimplement", REIMPLEMENT_P), ("artifact_review", ARTIFACT_P)):
        _conf = _built_in_confinement(Config(), _role_key,
                                      repo_dir="/r" if _role_key == "artifact_review" else "")
        assert set(_conf.allowed_tools) == set(_module.ROLE_SPEC.get("tools", ())), _role_key
        if "bare" in _module.ROLE_SPEC:
            assert _conf.bare == _module.ROLE_SPEC["bare"], _role_key
    # An operator-supplied command line is recorded as UNENFORCED for every role.
    for _role_key, _attr in (("lens", "audit_cmd"), ("grade", "grade_cmd"),
                             ("verdict", "verdict_cmd"), ("reimplement", "reimplementation_cmd"),
                             ("artifact_review", "artifact_review_cmd")):
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

    # --- a commit mismatch invalidates a cached artifact inspection ----------------------
    with _tf.TemporaryDirectory() as td:
        _cfg = Config(projects_dir=Path(td), allow_artifact_review=True)
        state.create_project(_cfg, "", "T", pid="p")
        _insp = ArtifactInspection(proposed=0, relocated=0)
        seal_artifact_inspection(_cfg, "p", _insp, {"written_by": "artifact_review_driver",
                                                    "delegation_mode": "CLI_SUBPROCESS"})
        assert load_artifact_inspection(_cfg, "p") is not None
        assert load_artifact_inspection(_cfg, "p", commit="0" * 40) is None, (
            "an inspection is about ONE tree and may not be served for another")

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
    for _role_key in ("grade", "verdict", "reimplement"):
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
