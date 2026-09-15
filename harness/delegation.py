"""How this harness asks another agent to do a reasoning task, whatever is driving it.

`python -m harness.delegation` runs the self-check.

**The defect this exists to fix.** `audit_driver` hard-coded ONE delegation mechanism: a
fresh `claude -p` subprocess. That mechanism is not always the right one and is sometimes
not available at all. Its costs land on the ACCOUNT rather than on the controlling
session, so a controller that already has an isolated-subagent mechanism of its own paid
twice and hit an account-level session limit that had nothing to do with the review. The
harness had no way to express "delegate this" without also specifying "by spawning a CLI".

**The separation this module draws.** A reasoning task has two halves and they belong to
two different owners:

    THE HARNESS owns the CONTRACT and the PROVENANCE. What prompt, what schema, what
                isolation is required, where the answer goes, how the answer is validated,
                and what the artifact must record about how it was produced.

    THE ENVIRONMENT owns the EXECUTION. Whether the delegate is a CLI subprocess, an
                isolated subagent of the controlling session, or a human at a terminal.

So this module defines modes, capability detection and a WORK ORDER, and it executes
nothing. `audit_driver` fulfils a work order one way; a controlling session fulfils it
another; `stages/audit.accept_lens` seals either, recording which.

**Why the modes are not interchangeable, and why `MANUAL` is not a catch-all.** Before
this, anything not produced by the CLI was sealed `manual_accept` — so an isolated
subagent dispatched autonomously by the controller and a human pasting JSON into a file
were the same provenance token. They are not the same thing. One is autonomous delegated
execution with a fresh context per lens; the other is a person, with no isolation claim at
all and no guarantee the four lenses were even independent. A corpus that mixes them and
reports one number is reporting neither.

What every mode must state honestly is what it CANNOT prove. Only `CLI_SUBPROCESS` can
demonstrate an enforced tool policy, because only it built the argv. `SESSION_SUBAGENT`
gets real context isolation and cannot prove a filesystem sandbox. `MANUAL` proves
nothing. `tool_policy` therefore defaults to `unrecorded` in every mode but the first, and
`ISOLATION_CLAIM` below is the closed vocabulary of what each mode is entitled to say.
"""
from __future__ import annotations

import shutil

# HOW a reasoning task was actually carried out. Closed, and ordered by how much the
# resulting artifact can prove about its own production.
DELEGATION_MODES = (
    "CLI_SUBPROCESS",    # a fresh reviewer CLI process the harness spawned and confined
    "SESSION_SUBAGENT",  # an isolated subagent of the controlling session, one per task
    "MANUAL",            # a human, or an agent this harness knows nothing about
    "UNAVAILABLE",       # nothing can be delegated to from here
)

# The `written_by` token each mode seals into the provenance sidecar. Distinct per mode,
# deliberately: `lens_is_accepted` validates against this set, so a mode that is not
# listed here cannot produce an accepted artifact, and two modes cannot share a token.
WRITTEN_BY = {
    "CLI_SUBPROCESS": "audit_driver",
    "SESSION_SUBAGENT": "session_subagent",
    "MANUAL": "manual_accept",
}

# What each mode is entitled to CLAIM about the isolation its delegate ran under. Not
# what it hopes; what it can show. A claim is a sentence a reader can act on, so it says
# the limit as well as the guarantee.
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

# The one mode that may report an enforced tool policy. Everything else records
# `unrecorded`, and `provenance_record` enforces that rather than trusting a caller.
_MAY_PROVE_TOOL_POLICY = ("CLI_SUBPROCESS",)


def cli_available(command: str = "") -> tuple[bool, str]:
    """Is a reviewer CLI reachable from this process?

    `command` is the operator's configured command line, if any. An empty command falls
    back to looking for `claude` on PATH, which is what `audit_driver.default_cmd` does.
    """
    if (command or "").strip():
        return True, "the operator configured a reviewer command"
    exe = shutil.which("claude")
    if exe:
        return True, f"found a reviewer CLI on PATH at {exe}"
    return False, "no reviewer command is configured and no `claude` is on PATH"


def modes_available(*, cli_command: str = "", cli_gate_open: bool = False,
                    session_can_delegate: bool = False) -> tuple[str, ...]:
    """Which delegation modes this environment actually offers, most capable first.

    Booleans and strings only. `session_can_delegate` is asserted BY THE CONTROLLER and
    cannot be detected from inside a Python process: a harness cannot discover whether
    the thing that launched it is able to dispatch a subagent. That is precisely why it is
    a parameter — the environment answers it, the harness records the answer, and nothing
    here guesses.

    MANUAL is always available because a human is always available. It is last because it
    proves the least, not because it is worst: a careful human beats a rushed agent, and
    the harness has no way to tell which it got.
    """
    out = []
    if cli_gate_open and cli_available(cli_command)[0]:
        out.append("CLI_SUBPROCESS")
    if session_can_delegate:
        out.append("SESSION_SUBAGENT")
    out.append("MANUAL")
    return tuple(out)


def choose(available: tuple[str, ...] = (), *, prefer: str = "") -> str:
    """The mode to use, given what is available and what the operator asked for.

    `prefer` wins when it is available, because which mechanism to spend is an operator's
    decision and not this harness's: one controller pays for a CLI session, another pays
    for its own subagents, and the harness is not entitled to an opinion about whose
    budget to spend. Otherwise the most capable available mode wins, since a mode that can
    prove more about its own production yields a more checkable artifact.

    Returns UNAVAILABLE only for an empty list, which `modes_available` never produces —
    so reaching it means a caller built the list itself and got it wrong.
    """
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
    """What a delegate must be told, in a form any environment can fulfil.

    The harness emits this; something else carries it out. It names the prompt to read,
    the exact path to write, the isolation the mode claims, and the reading the delegate
    must NOT do — because for `SESSION_SUBAGENT` and `MANUAL` that last part is the only
    isolation mechanism there is, and an unstated contract is not a contract.

    Deliberately a plain dict: it crosses a boundary out of Python, into whatever is
    driving, and a pydantic model would suggest this side validates the answer. It does
    not. `parse_lens_json` and `accept_lens` validate the answer.
    """
    return {
        "task": task,
        "mode": mode,
        "read": prompt_path,
        "write": output_path,
        "isolation_required": ISOLATION_CLAIM.get(mode, ISOLATION_CLAIM["MANUAL"]),
        "must_not_read": [
            "any other task's prompt or output for this paper",
            "this harness's own source, tests or working contract",
            "any other paper's project directory",
            "any archived earlier review of the same paper",
        ],
        "output_contract": (schema_hint
                            or "exactly one JSON object, no markdown fence, no prose"),
        "validated_by": "harness.audit_driver.parse_lens_json, then harness.stages.audit"
                        ".accept_lens — a malformed answer is refused, not repaired",
    }


def provenance_record(*, mode: str, reviewer: str = "",
                      tool_policy: str = "") -> dict:
    """The provenance fields an artifact must carry, with what it cannot prove removed.

    A caller may pass a `tool_policy` it wishes were true. Only `CLI_SUBPROCESS` built the
    delegate's command line, so only `CLI_SUBPROCESS` may report one; every other mode has
    the field forced to `unrecorded` HERE rather than by asking callers to remember. That
    is the difference between a guarantee and a convention: the September corpus recorded
    `tool_policy: unrecorded` correctly, and it did so because nothing offered it a way to
    claim otherwise. This keeps that true now that a second autonomous mode exists.
    """
    m = (mode or "").strip().upper()
    if m not in WRITTEN_BY:
        m = "MANUAL"
    policy = (tool_policy or "").strip() if m in _MAY_PROVE_TOOL_POLICY else ""
    return {
        "written_by": WRITTEN_BY[m],
        "delegation_mode": m,
        "reviewer": (reviewer or "").strip() or "unnamed",
        "tool_policy": policy or "unrecorded",
        "isolation_claim": ISOLATION_CLAIM[m],
        "tool_policy_provable": m in _MAY_PROVE_TOOL_POLICY,
    }


def mode_of(record: dict | None) -> str:
    """The mode an existing artifact was produced under, from its sidecar.

    Reads `delegation_mode` when present and otherwise infers from `written_by`, so an
    artifact sealed before this module existed still reports a mode instead of a blank.
    An unrecognised sidecar reports MANUAL, which is the mode that claims least — failing
    toward "we cannot show anything about this" rather than toward the CLI's guarantees.
    """
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
    """How a set of artifacts was produced, mode by mode.

    Exists so a corpus can never be described as one homogeneous run when it is not.
    `homogeneous` is False the moment two modes appear, and the count per mode is the
    thing a report must print instead of a single sentence about "the delegation path".
    """
    counts: dict[str, int] = {}
    for rec in records or []:
        m = mode_of(rec)
        counts[m] = counts.get(m, 0) + 1
    present = sorted(counts)
    return {
        "by_mode": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "modes_present": present,
        "homogeneous": len(present) <= 1,
        "total": sum(counts.values()),
        "tool_policy_provable_for": counts.get("CLI_SUBPROCESS", 0),
    }


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    # --- the vocabularies are closed and do not overlap -------------------------------
    assert set(WRITTEN_BY) | {"UNAVAILABLE"} == set(DELEGATION_MODES)
    assert len(set(WRITTEN_BY.values())) == len(WRITTEN_BY), (
        "two delegation modes share a written_by token, so an artifact could not say "
        "which produced it")
    assert set(ISOLATION_CLAIM) == set(DELEGATION_MODES)
    # MANUAL and SESSION_SUBAGENT are the pair this module exists to keep apart
    assert WRITTEN_BY["SESSION_SUBAGENT"] != WRITTEN_BY["MANUAL"]

    # --- signatures admit strings and booleans only ------------------------------------
    for fn in (modes_available, provenance_record, work_order):
        for name, p in inspect.signature(fn).parameters.items():
            assert str(p.annotation) in ("str", "bool"), f"{fn.__name__}.{name}"

    # --- capability detection ----------------------------------------------------------
    assert cli_available("my-reviewer --flag")[0] is True
    ok, why = cli_available("")
    assert isinstance(ok, bool) and why

    # --- what each environment offers --------------------------------------------------
    assert modes_available() == ("MANUAL",), "a human is always available"
    assert modes_available(session_can_delegate=True) == ("SESSION_SUBAGENT", "MANUAL")
    assert modes_available(cli_command="x", cli_gate_open=True) \
        == ("CLI_SUBPROCESS", "MANUAL")
    both = modes_available(cli_command="x", cli_gate_open=True, session_can_delegate=True)
    assert both == ("CLI_SUBPROCESS", "SESSION_SUBAGENT", "MANUAL")
    # a shut gate removes the CLI even when the command exists
    assert "CLI_SUBPROCESS" not in modes_available(cli_command="x", cli_gate_open=False)

    # --- choosing, and the operator's preference winning -------------------------------
    assert choose(both) == "CLI_SUBPROCESS", "most capable by default"
    assert choose(both, prefer="SESSION_SUBAGENT") == "SESSION_SUBAGENT", (
        "whose budget to spend is the operator's decision, not this harness's")
    assert choose(both, prefer="session_subagent") == "SESSION_SUBAGENT", "case-folded"
    assert choose(("MANUAL",), prefer="CLI_SUBPROCESS") == "MANUAL", (
        "a preference for something unavailable is not honoured silently by inventing it")
    assert choose(()) == "UNAVAILABLE"
    assert choose(("NOT_A_MODE",)) == "UNAVAILABLE"

    # --- ONLY the CLI may claim an enforced tool policy --------------------------------
    cli = provenance_record(mode="CLI_SUBPROCESS", reviewer="claude",
                            tool_policy="enforced: tools=Read denied=12")
    assert cli["written_by"] == "audit_driver"
    assert cli["tool_policy"] == "enforced: tools=Read denied=12"
    assert cli["tool_policy_provable"] is True
    for mode in ("SESSION_SUBAGENT", "MANUAL"):
        rec = provenance_record(mode=mode, reviewer="whoever",
                                tool_policy="enforced: tools=Read denied=12")
        assert rec["tool_policy"] == "unrecorded", (
            f"{mode} cannot prove a tool policy and must not be allowed to claim one")
        assert rec["tool_policy_provable"] is False
        assert rec["written_by"] != "audit_driver"
    # an unknown mode falls to the one that claims least
    unknown = provenance_record(mode="SOMETHING_NEW", tool_policy="enforced: everything")
    assert unknown["delegation_mode"] == "MANUAL"
    assert unknown["tool_policy"] == "unrecorded"

    # --- reading a mode back off an artifact -------------------------------------------
    assert mode_of({"delegation_mode": "SESSION_SUBAGENT"}) == "SESSION_SUBAGENT"
    assert mode_of({"written_by": "audit_driver"}) == "CLI_SUBPROCESS", "legacy sidecar"
    assert mode_of({"written_by": "manual_accept"}) == "MANUAL"
    assert mode_of({"written_by": "session_subagent"}) == "SESSION_SUBAGENT"
    for junk in (None, {}, {"written_by": "who knows"}, "not a dict"):
        assert mode_of(junk if isinstance(junk, dict) else None) == "MANUAL"

    # --- a mixed corpus can never describe itself as one thing -------------------------
    mixed = summarise([{"written_by": "audit_driver"}, {"written_by": "audit_driver"},
                       {"delegation_mode": "SESSION_SUBAGENT"}])
    assert mixed["homogeneous"] is False
    assert mixed["by_mode"] == {"SESSION_SUBAGENT": 1, "CLI_SUBPROCESS": 2}
    assert mixed["total"] == 3 and mixed["tool_policy_provable_for"] == 2
    assert summarise([{"written_by": "audit_driver"}])["homogeneous"] is True
    assert summarise([])["homogeneous"] is True and summarise([])["total"] == 0

    # --- a work order states what must NOT be read ------------------------------------
    wo = work_order(task="lens:confound", prompt_path="a.md", output_path="b.json",
                    mode="SESSION_SUBAGENT")
    assert wo["mode"] == "SESSION_SUBAGENT" and wo["read"] == "a.md"
    assert "NOT provable" in wo["isolation_required"]
    assert any("other paper" in x for x in wo["must_not_read"])
    assert "refused, not repaired" in wo["validated_by"]
    print("harness.delegation self-check ok")


if __name__ == "__main__":
    _self_check()
