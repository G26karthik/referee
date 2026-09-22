"""How this harness asks another agent to do a reasoning task.

`python -m harness.delegation` runs the self-check.

**Current state: ONE live channel.** The harness never spawns a `claude` CLI subprocess —
see `harness/tasks.py` for the live protocol. The controlling Claude Code session
dispatches its own isolated subagents, one per delegable unit (mode `SESSION_SUBAGENT`);
`harness/tasks.py` builds the work (prompt + output path), and `harness.tasks.seal`
validates and seals whatever comes back. `CLI_SUBPROCESS` and its `audit_driver` /
`grade_driver` / `verdict_driver` / `reimplement_driver` / `artifact_review_driver`
writer tokens remain in the vocabulary below SOLELY so a sidecar sealed before this
change still reads back as the mode that actually produced it. Nothing in this codebase
produces that mode any more.

**The separation this module still draws.** A reasoning task has two halves and they
belong to two different owners:

    THE HARNESS owns the CONTRACT and the PROVENANCE. What prompt, what schema, what
                isolation is required, where the answer goes, how the answer is validated,
                and what the artifact must record about how it was produced.

    THE ENVIRONMENT owns the EXECUTION. Whether the delegate is an isolated subagent of
                the controlling session or a human at a terminal.

**Why the modes are not interchangeable, and why `MANUAL` is not a catch-all.** An
isolated subagent dispatched autonomously by the controller and a human pasting JSON into
a file are not the same thing. One is autonomous delegated execution with a fresh context
per task; the other is a person, with no isolation claim at all and no guarantee the four
lenses were even independent. A corpus that mixes them and reports one number is
reporting neither.

What every mode must state honestly is what it CANNOT prove. Only `CLI_SUBPROCESS` — read
from an old seal, never produced now — could ever demonstrate an enforced tool policy,
because only it built the argv. `SESSION_SUBAGENT` gets real context isolation and cannot
prove a filesystem sandbox. `MANUAL` proves nothing. `tool_policy` therefore defaults to
`unrecorded` in every mode but the first, and `ISOLATION_CLAIM` below is the closed
vocabulary of what each mode is entitled to say.
"""
from __future__ import annotations

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


# --------------------------------------------------------------------------- #
# THE PER-ROLE SKELETON
# --------------------------------------------------------------------------- #
def resolve_model(override: str, declared_default: str) -> str:
    """An operator's model override, or the role's own declared default. Shared by every
    role's `role_model(cfg)`, which reads `override` off its own `Config` field and
    `declared_default` off its own `prompts.<role>.*_SPEC["model"]`."""
    return (override or "").strip() or str(declared_default or "")


def modes_available(*, session_can_delegate: bool = True) -> tuple[str, ...]:
    """Which delegation modes this environment actually offers, most capable first.

    Booleans only. `session_can_delegate` is asserted BY THE CONTROLLER and cannot be
    detected from inside a Python process: a harness cannot discover whether the thing
    that launched it is able to dispatch a subagent. That is precisely why it is a
    parameter — the environment answers it, the harness records the answer, and nothing
    here guesses. It defaults True because the one live channel — the controlling Claude
    Code session dispatching its own isolated subagents — is the normal case this harness
    now runs under; a caller that genuinely has no controller may pass False.

    MANUAL is always available because a human is always available. It is last because it
    proves the least, not because it is worst: a careful human beats a rushed agent, and
    the harness has no way to tell which it got.
    """
    out = []
    if session_can_delegate:
        out.append("SESSION_SUBAGENT")
    out.append("MANUAL")
    return tuple(out)


def choose(available: tuple[str, ...] = (), *, prefer: str = "") -> str:
    """The mode to use, given what is available and what the operator asked for.

    `prefer` wins when it is available, because which mechanism to spend is an operator's
    decision and not this harness's. Otherwise the most capable available mode wins, since
    a mode that can prove more about its own production yields a more checkable artifact.

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
    for fn in (modes_available, provenance_record):
        for name, p in inspect.signature(fn).parameters.items():
            assert str(p.annotation) in ("str", "bool"), f"{fn.__name__}.{name}"

    # --- the per-role skeleton: an override wins, a blank falls to the declared default -
    assert resolve_model("haiku", "sonnet") == "haiku"
    assert resolve_model("", "sonnet") == "sonnet"
    assert resolve_model("   ", "sonnet") == "sonnet", "a blank override is not an override"
    assert resolve_model("", "") == ""

    # --- what each environment offers --------------------------------------------------
    assert modes_available(session_can_delegate=False) == ("MANUAL",), (
        "a human is always available")
    assert modes_available() == ("SESSION_SUBAGENT", "MANUAL"), (
        "the controlling session's own subagents are the default, live channel")
    both = modes_available(session_can_delegate=True)
    assert both == ("SESSION_SUBAGENT", "MANUAL")

    # --- choosing, and the operator's preference winning -------------------------------
    assert choose(both) == "SESSION_SUBAGENT", "most capable by default"
    assert choose(both, prefer="MANUAL") == "MANUAL", (
        "whose budget to spend is the operator's decision, not this harness's")
    assert choose(both, prefer="manual") == "MANUAL", "case-folded"
    assert choose(("MANUAL",), prefer="SESSION_SUBAGENT") == "MANUAL", (
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
    print("harness.delegation self-check ok")


if __name__ == "__main__":
    _self_check()
