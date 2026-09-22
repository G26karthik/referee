"""What survives of the old CLI-subprocess mechanics once the harness stopped spawning
`claude` processes: prompt-fingerprinting for replay, the CLI's own JSON result-envelope
unwrap, and classification of a delegated reviewer's OWN failure text. All three are
genuinely about VALIDATING an answer rather than producing one, which is why they outlived
the argv/confinement/spawn machinery that used to sit beside them — nothing in this
repository builds a `claude` CLI command line or spawns a subprocess any more (see
`harness/tasks.py` for the live protocol, `harness/delegation.py` for the mode vocabulary):

  - `prompt_fingerprint`/`keep_prompt_copy`/`prompt_is_unchanged` — a sealed reading is only
    trustworthy if the prompt on disk still matches the one that produced it. Read by
    `harness.audit.unit_is_accepted` and written by `harness.tasks.seal`.
  - `unwrap_envelope`/`envelope_provenance` — tolerant of a `--output-format json` envelope
    wrapping the answer, a shape a hand-run reviewer CLI could still produce even though
    nothing in this harness invokes one automatically any more.
  - `classify_delegated_failure` — a delegated reviewer's OWN words about why it failed
    ("model X is not available on your plan", a tool-permission refusal) need to be told
    apart from `harness.failures`' generic classification, so a `never`-retryable
    invocation error does not eat a retry budget meant for a transient failure.

Imported by `harness/agent.py`, `harness/tasks.py`, `harness/stages/artifact.py`,
`harness/artifact_review_driver.py` and `harness/reimplement_driver.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import failures

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
# away. Both shapes are still accepted here even though nothing in this harness invokes a
# CLI automatically any more: a hand-run reviewer, or a future delegate, may still produce
# one, and a session-subagent's bare JSON answer passes through untouched either way.
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
# about what a reviewer says about the CALL ITSELF, which that table has no pattern for,
# and both were landing in the wrong bucket:
#
#   a tool-permission refusal matched "permission denied" -> `unauthenticated`/never, so
#   the operator's recorded next action was to re-authenticate a credential that works;
#
#   "model X is not available on your plan" matched nothing at all -> `unknown`/now, so
#   a role asking for a model an account cannot reach burned a retry budget three times
#   over against a flag that will never be accepted.
#
# Both are `bad_invocation`/never: re-issuing the identical request gives the same answer,
# and the fix is the role's model or tool request, not waiting and not re-logging-in.
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
# `gate_closed`/never, which is right for a harness-authored refusal and wrong for a
# reviewer's: it abandons a paper permanently on a phrase that can appear in any
# diagnostic.
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
