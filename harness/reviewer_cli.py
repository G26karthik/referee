"""The mechanics of ONE delegated CLI-subprocess reviewer call — argv, tool confinement,
pinned settings, result-envelope parsing, kill-tree teardown, prompt fingerprinting for
replay, and failure classification.

Contrast with `harness/delegation.py`, which decides WHICH delegation mechanism an
environment offers and which one gets used (a CLI subprocess, an isolated session
subagent, or a human) and owns none of the mechanics below. That module's own test
(`tests/test_delegation_modes.py::
test_the_harness_cannot_detect_whether_the_controller_can_delegate`) asserts its imports
never include `os`, `sys` or `subprocess`, so it cannot sniff its environment or spawn a
process — deciding a mechanism and executing one are different jobs, in different files.
This module is where the execution mechanics for the `CLI_SUBPROCESS` mechanism live.

Originally implemented once inside `harness/audit_driver.py` and then imported from there,
unchanged, by six of the other seven `*_driver.py` files — `audit_driver` had accidentally
become the de facto shared library this module now is. Nothing here decides what a
delegated answer is WORTH; that stays in each driver's own module (`harness/audit_driver.py`
in particular still owns the lens-report-specific parsing and the finding-schema stripping
that build on top of what this module hands back).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from . import failures


class ReviewerCLIError(RuntimeError):
    """The configured command did not produce a usable lens report.

    Self-classifying. Every instance carries `kind` / `retry` / `reset_hint` from
    `harness.failures.classify` over its own message, so a raise site does not have to
    know or care which failure taxonomy bucket its text falls into — and so a failure
    mode nobody anticipated still arrives at the controller with a retry policy attached
    rather than as an undifferentiated string. See `harness/failures.py` for why the
    three policies (`now` / `later` / `never`) are not interchangeable.
    """

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = failures.classify(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


class RateLimited(ReviewerCLIError):
    """A refusal that a LATER attempt could satisfy and an immediate one cannot: the
    operator's own account over its usage limit, a 429, an overloaded or 5xx upstream.

    A subclass of `ReviewerCLIError` so every existing `except ReviewerCLIError` still
    catches it, and a distinct class so `fill()` can route it away from `failed` (a
    quality problem with the reviewer's output that a retry might well fix) for the
    controller to treat differently — a `later` failure must not consume a retry attempt.
    """


class NonRetryable(ReviewerCLIError):
    """A refusal that will fail identically forever: no reviewer installed, an
    unauthenticated account, a flag the CLI does not accept, an unreadable PDF.

    Kept apart from `RateLimited` because the operator's next action is the opposite one.
    Waiting fixes a rate limit and never fixes a missing `claude` on PATH — and spending
    three attempts to discover that hides the actual fix behind a retry log.
    """


def driver_error(message: str) -> ReviewerCLIError:
    """Build the RIGHT exception class for a failure message. One classification point.

    The defect this replaces: a hand-carved `_RATE_LIMIT_RE` at two call sites, covering
    exactly one of the eleven ways a delegated reviewer can fail. Everything else — a
    revoked credential, an unrecognised flag, an upstream outage — arrived as the generic
    class and burned the whole retry budget against a wall that would not move.
    """
    kind, retry, hint = classify_delegated_failure(message)
    cls = {"later": RateLimited, "never": NonRetryable}.get(retry, ReviewerCLIError)
    return cls(message, kind=kind, retry=retry, reset_hint=hint)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the WHOLE process tree a `shell=True` command started, not just the shell.

    `run_lens` launches the reviewer inside its own process group (POSIX) or process
    group (Windows, via `CREATE_NEW_PROCESS_GROUP`) precisely so this can reach every
    descendant: `taskkill /T` walks the tree by PID on Windows, `killpg` signals every
    process sharing the session's process group ID on POSIX. `proc.kill()` alone reaches
    only the immediate shell — the actual reviewer process is its CHILD, not it.
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass                                  # already gone
    try:
        proc.kill()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# CONFINEMENT — the flags that DENY, not only the ones that allow
# --------------------------------------------------------------------------- #
# Every tool this harness knows how to name, split by what granting it would let a
# delegated reviewer DO. The split is the point: a deny list has to be derived from the
# grant, or it drifts the moment a lens is given one more tool and nobody updates a
# hand-written list of everything else.
#
# ponytail: this enumeration is a snapshot of the installed CLI's tool names, verified
# against `claude --help` at design time. A tool the CLI adds later is in neither tuple,
# so it is neither granted nor explicitly denied — `--restricted` plus a `default`
# permission mode is what still refuses it, and `KNOWN_TOOLS` being incomplete is
# recorded on the sidecar (the deny list is printed verbatim) rather than presented as
# exhaustive. The rejected alternative was to shell out to `claude --help` on every
# invocation to enumerate them, which spends a subprocess per lens to re-derive
# something that does not change between lenses of one run.
CODE_RUNNING_TOOLS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit", "Task",
    "SlashCommand",
)
READING_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "WebFetch", "WebSearch")
KNOWN_TOOLS: tuple[str, ...] = CODE_RUNNING_TOOLS + READING_TOOLS


def denied_tools(allowed: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Every known tool this agent was NOT granted, named explicitly for `--disallowedTools`.

    Derived from the grant rather than written down beside it, so the two cannot
    disagree. `test_no_tool_is_both_granted_and_denied_to_the_same_agent` and
    `test_every_known_tool_is_either_granted_or_explicitly_denied` are the whole
    contract, and together they are what makes "the allow list denies nothing" false.
    """
    granted = {str(t).strip() for t in allowed if str(t).strip()}
    return tuple(t for t in KNOWN_TOOLS if t not in granted)


def pinned_settings_json(denied: tuple[str, ...] = ()) -> str:
    """The settings document `--settings` pins, as canonical bytes.

    Replaces the operator's own settings rather than merging with them. What was inherited
    otherwise, measured on the development host: a permission mode, a default model, a
    `PreToolUse` hook, ten plugins, four MCP servers, and a user-level `CLAUDE.md` that
    Claude Code loads regardless of cwd. For an audit lens that is uncontrolled variance
    between two runs of the same review; for the blinded grader it is a channel by which
    the operator's own notes reach the reader that is supposed to see only one candidate.

    `defaultMode: "default"` means "ask before using a tool", and a `-p` one-shot session
    has nobody to ask — so the mode that looks permissive on a desktop is the fail-closed
    one here. That is deliberate and is the reason this is not `bypassPermissions`.

    Not written to the repository. The bytes are a function of this module plus the deny
    list, so the `settings_sha256` recorded on a sidecar identifies exactly which document
    ran; a checked-in config file would have to be kept in sync with `denied_tools` by
    hand, and a settings file that has drifted from the deny list it is supposed to
    express is worse than no file.
    """
    doc = {
        "$comment": ("single-harness delegated review: the operator's ambient settings are "
                     "REPLACED, not merged. See harness.reviewer_cli.pinned_settings_json."),
        "enableAllProjectMcpServers": False,
        "enabledPlugins": {},
        "hooks": {},
        "permissions": {
            "additionalDirectories": [],
            "allow": [],
            "defaultMode": "default",
            "deny": list(denied),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True)


@dataclasses.dataclass(frozen=True)
class Confinement:
    """What was passed to ONE delegated agent, as facts about flags.

    This replaces a two-valued `tool_policy` string that was computed from whether
    `SH_AUDIT_CMD` happened to be empty — i.e. from which branch of an `if` the code took,
    not from anything about the agent that ran. A provenance field that cannot be wrong is
    not evidence, and `"restricted"` as the NAME of a code path while `--restricted` was
    never passed is the exact shape of claim this whole subsystem exists to avoid making.

    `enforced` is a property, not a stored flag, so it cannot be set to True beside an
    operator-supplied command line.
    """

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
        """True only when THIS module built the argv, so the flags above are observations.

        An operator command wins unconditionally (`resolve_cmd`) and may be anything at
        all; characterising its confinement would be describing a command line we did not
        write. So it is recorded as unenforced rather than assumed to match.
        """
        return self.template == "built_in"

    def flags(self, settings_path: str = "") -> str:
        """The flag suffix for the built-in template. Empty for an operator command."""
        if not self.enforced:
            return ""
        out = ""
        if self.output_format:
            out += f" --output-format {self.output_format}"
        # `--allowedTools ""` is emitted even when nothing is granted: an empty allow list
        # is the grader's actual policy and omitting the flag would inherit whatever the
        # pinned settings' allow list said instead of overriding it.
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
        """The structured record that goes on the sidecar. JSON-serialisable, all facts."""
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
        """One line for a human, and for the manuscript checker's set of policy strings.

        Two representations of one fact, deliberately: `manuscript/check_claims.py`
        collects every sidecar's `tool_policy` into a SET and tests it for the substring
        "unrecorded", and a dict is neither hashable nor searchable that way. So the
        string stays under the original key and the structured record sits beside it under
        `tool_policy_detail`. The alternative — replacing the string outright — would have
        made the honesty check that currently forces the manuscript to admit this path
        never ran crash instead of running.
        """
        if not self.enforced:
            return ("unrecorded (operator-supplied command line; this module did not build "
                    "the argv and cannot characterise its confinement)")
        bits = [f"tools={','.join(self.allowed_tools) or 'none'}",
                f"denied={len(self.disallowed_tools)}",
                f"add_dir={len(self.add_dir)}"]
        for name, on in (("restricted", self.restricted), ("strict_mcp", self.strict_mcp),
                         ("bare", self.bare), ("settings", bool(self.settings_sha256))):
            if on:
                bits.append(name)
        return "enforced: " + " ".join(bits)


def operator_confinement(role: str) -> Confinement:
    """The honest record for a command line this module did not build."""
    return Confinement(role=role, template="operator_supplied")


def write_pinned_settings(directory: Path, denied: tuple[str, ...]) -> tuple[Path, str]:
    """Materialise the pinned settings document. Returns (path, sha256).

    Written OUTSIDE the reviewer's cwd. The cwd is an empty scratch directory precisely
    so that a relative `Read` finds nothing, and dropping our own policy file into it
    would put the first readable file in the sandbox there — small, but it is the one
    property this sandbox has.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = pinned_settings_json(denied)
    path = directory / "settings.pinned.json"
    path.write_text(body, encoding="utf-8")
    return path, hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# REPLAY — the fingerprint of the prompt that actually ran
# --------------------------------------------------------------------------- #
def prompt_fingerprint(prompt: Path) -> str:
    """sha256 of the prompt bytes, or '' if it cannot be read.

    `stages/audit.run_audit` rewrites every prompt on every run, so "the prompt that
    produced this lens file" was not recoverable after any edit to `prompts/audit.py` —
    and `lens_is_accepted` still reported the lens sealed and complete. A seal over an
    output whose input has been replaced certifies a correspondence that no longer exists.
    """
    try:
        return hashlib.sha256(prompt.read_bytes()).hexdigest()
    except OSError:
        return ""


def keep_prompt_copy(prompt: Path, sha256: str) -> Path | None:
    """Keep a content-addressed copy beside the regenerated prompt. Best effort.

    Best effort on purpose: failing a lens because a diagnostic copy could not be written
    would let a full disk decide a review's outcome. The sidecar still records
    `prompt_sha256`, so a missing copy costs replay convenience and never the audit trail.
    """
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

    Three-valued in effect, and the middle value matters: a sidecar with NO
    `prompt_sha256` is a record written before this field existed, and calling that a
    mismatch would retroactively invalidate every artifact in the repository. It reports
    `unpinned` and is the caller's decision, not this function's.
    """
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


# --------------------------------------------------------------------------- #
# THE CLI's OWN RESULT ENVELOPE
# --------------------------------------------------------------------------- #
# `--output-format json` wraps the model's answer in a result object carrying the session
# id, the model, the turn count and the cost. `--output-format text` throws all of that
# away, which is why nothing in this repository records which model answered any of its
# 28 lens readings. Both shapes are accepted here: an operator-supplied command may still
# ask for text, and a lens file hand-written by a person is neither.
_ENVELOPE_MARKERS = ("type", "subtype", "is_error", "session_id", "sessionId",
                     "total_cost_usd", "duration_ms", "num_turns")


def unwrap_envelope(text: str) -> tuple[str, dict]:
    """(payload, envelope) — the CLI's json result wrapper unwrapped, or (text, {}).

    Conservative in the direction that matters: a lens report is recognised by carrying
    `findings`, and anything carrying `findings` is returned untouched even if it also
    happens to carry an envelope-looking key. Mistaking a report for an envelope would
    silently drop every finding in it.
    """
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
    """What the CLI itself reported about the call it just made.

    `envelope_keys` is recorded verbatim and is the point of this function. Every other
    field here is read out of a key name this module GUESSED from the CLI's documented
    output, and if a future CLI renames one, a sidecar with `model_reported: ""` is
    indistinguishable from a sidecar written by a CLI that reported no model — unless the
    keys that WERE present are on the record. A blank provenance field with no account of
    why it is blank is the failure mode this whole subsystem exists to avoid.
    """
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


# --------------------------------------------------------------------------- #
# DELEGATION-SPECIFIC FAILURE SHAPES
# --------------------------------------------------------------------------- #
# `harness.failures` classifies what a reviewer says about the world. These two rules are
# about what a reviewer says about the COMMAND WE ISSUED, which that table has no pattern
# for, and both were landing in the wrong bucket:
#
#   a tool-permission refusal matched "permission denied" -> `unauthenticated`/never, so
#   the operator's recorded next action was to re-authenticate a credential that works;
#
#   "model X is not available on your plan" matched nothing at all -> `unknown`/now, so
#   `--model opus` on an account without opus burned the whole retry budget three times
#   over against a flag that will never be accepted.
#
# Both are `bad_invocation`/never: the argv this harness built is not one this account
# will act on, re-issuing it gives the same answer, and the fix is the tool policy or the
# role's model — not waiting and not re-logging-in. Invariant 14: an attempt is spent only
# where spending it could change the answer.
_DELEGATION_RULES: tuple[tuple[str, str, str], ...] = (
    (r"requested permissions? to use|permission to use the\b|tool use (was )?(denied|blocked)"
     r"|not allowed to use|disallowed[_ ]tools?|is not in the allowed tools"
     r"|tool .{0,24} is not permitted", "bad_invocation", "never"),
    (r"model[^\n]{0,60}\b(is )?(not available|not found|unavailable|not enabled)"
     r"|does not have access to (the )?model|invalid model|unknown model|model_not_found"
     r"|not entitled to", "bad_invocation", "never"),
)
_DELEGATION_COMPILED = tuple((re.compile(p, re.I), k, r) for p, k, r in _DELEGATION_RULES)

# One of THIS harness's own gate refusals, as opposed to a reviewer saying the words "not
# set" about something else entirely. `failures._RULES` maps a bare "not set" to
# `gate_closed`/never, which is right for `available()`'s own message and wrong for a
# reviewer's: it abandons a paper permanently on a phrase that can appear in any
# diagnostic. The drivers raise their own gate refusals directly, never through
# `driver_error`, so anything reaching this point came from the reviewer's mouth.
_HARNESS_GATE_RE = re.compile(r"gate (is )?(closed|shut)|set SH_[A-Z_]+", re.I)


def classify_delegated_failure(text: str) -> tuple[str, str, str]:
    """(kind, retry, reset_hint) for a delegated reviewer's OWN words.

    Delegation-specific rules first, then `harness.failures.classify`, then one narrowing:
    a `gate_closed` verdict that does not actually name a gate falls back to
    `unknown`/`now`. That trades one wasted attempt on "ANTHROPIC_API_KEY is not set" for
    not abandoning a paper whose transient error happened to contain the words — which is
    the direction `harness/failures.py`'s own docstring already argues for.
    """
    t = str(text or "")
    for rx, kind, policy in _DELEGATION_COMPILED:
        if rx.search(t):
            return kind, policy, ""
    kind, policy, hint = failures.classify(t)
    if kind == "gate_closed" and not _HARNESS_GATE_RE.search(t):
        return "unknown", "now", ""
    return kind, policy, hint


# --------------------------------------------------------------------------- #
# THE CONFINED CALL — the subprocess mechanics every `*_driver.py`'s `call()`/`run_lens()`
# used to reimplement on its own (policy staging, process-group spawn, timeout+kill-tree).
# Originally copy-pasted into eight files when `reviewer_cli.py` was split out of
# `audit_driver.py` (see this module's own docstring) — the ARGV each driver builds still
# differs per role, so only the mechanics below it are shared.
# --------------------------------------------------------------------------- #
def stage_settings(conf: Confinement, policy_dir: Path) -> tuple[Confinement, str]:
    """Write `conf`'s pinned settings into `policy_dir` if `conf` is enforced.

    Returns `(conf, "")` unchanged when `conf` is not enforced (an operator-supplied
    command line this module did not build — see `Confinement.enforced`). Raises OSError
    on write failure; every caller already has its own message and cleanup for that, so
    this does not swallow it.
    """
    if not conf.enforced:
        return conf, ""
    sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
    return dataclasses.replace(conf, settings_sha256=sha), str(sp)


def spawn_and_wait(cmd: str, cwd: Path, timeout_s: float
                   ) -> tuple[subprocess.CompletedProcess | None, bool]:
    """Run ONE confined shell command to completion or timeout. Returns (result, timed_out).

    Own process group so a timeout can kill the WHOLE tree, not just the immediate shell —
    see `_kill_tree`. `result` is None only when it timed out (the tree was killed and
    reaped; there is nothing left to report). Raises OSError if the process could not even
    start — every caller already has its own message for that, so it is left to propagate
    rather than folded into the two-tuple.
    """
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
        proc.communicate()                       # reap the process now that it is dead
        return None, True
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr), False


# --------------------------------------------------------------------------- #
# THE ARGV TEMPLATE — the one-shot piped invocation every built-in driver command shares
# --------------------------------------------------------------------------- #
def cli_pipe_command(exe: str, extra_flags: str) -> str:
    """The one-shot piped invocation template every built-in driver command shares:
    `<reader> "{prompt}" | "<exe>" -p<extra_flags> > "{out}"`, `type` on Windows and `cat`
    elsewhere because `run_lens`/`run_candidate`/etc. launch it with shell=True. The prompt
    is piped rather than interpolated because it carries untrusted paper text.

    Needs `os.name`, which is exactly why this lives here and not in `harness/delegation.py`
    — see this module's own docstring.
    """
    reader = "type" if os.name == "nt" else "cat"
    return f'{reader} "{{prompt}}" | "{exe}" -p{extra_flags} > "{{out}}"'
