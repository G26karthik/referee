"""What a result would be compared against, and whether that comparison exists.

`python -m harness.comparison` runs the self-check.

**The assumption this module exists to remove.** Every execution in this system ended at
`local_exec.reconcile`, and `reconcile` knows exactly one comparison: a measured number
against a quantity the paper printed. That is the right comparison for "is 61.4 the number
their code produces". It is the WRONG comparison, and in most cases not a comparison at
all, for the other questions a referee actually asks:

  * "is the gain attributable to the augmentation, or to the loss-weight change made at
    the same time?" compares one ARM against ANOTHER ARM. Neither arm is in the paper.
  * "is the control the claim needs actually present?" compares against EXISTENCE. There
    is no number on either side.
  * "does the evaluation protocol match the claim drawn from it?" compares an observed
    procedure against a SPECIFIED one.

Because `reconcile` could only do the first, `discovery._routes` offered an executable
route only where a printed quantity had been parsed — so the three questions above could
not reach a route at all, and were reported as "no verification route this system has
would settle the question". That sentence is false for all three: a route exists and this
harness could not carry out the comparison at the end of it. Naming the comparison
separates those two facts.

**The comparison is a property of the ROUTE, not of the question.** A question decides
which routes are admissible (`harness.questions.KIND` and `discovery.ROUTES_FOR_QUESTION`);
the route decides what the thing it produces gets compared against. Keeping them apart is
what stops "this is an attribution question" from silently becoming "so its number may be
reconciled against a cell".

**`RECONCILABLE` is one entry long and that is the honest state of this system.** Only
AGAINST_PRINTED_VALUE has arithmetic behind it in `local_exec.reconcile`. The other three
are named, routed to, and refused before anything starts — which is the same rule
`stages.probe.admissible_if_it_succeeds` applies to provenance, arriving one step earlier:
a process whose result could not be compared to anything must not be started.
"""
from __future__ import annotations

from .artifacts import COMPARISON_KINDS, COMPARISON_STATES, Comparison

# route -> what a result obtained by that route is compared against. A route absent from
# this table produces no comparison, which is not the same as an unestablished one: a
# target on the NONE route was never going to produce anything to compare.
COMPARISON_FOR_ROUTE = {
    "AUTHOR_CODE_EXECUTION": "AGAINST_PRINTED_VALUE",
    "INDEPENDENT_RECONSTRUCTION": "AGAINST_PRINTED_VALUE",
    "ARITHMETIC_RECHECK": "AGAINST_PRINTED_VALUE",
    "ARTIFACT_INSPECTION": "AGAINST_EXISTENCE",
    "PAPER_INTERNAL_CHECK": "AGAINST_SPECIFICATION",
    "NONE": "",
}

# The comparison kind this system has arithmetic for. ONE: AGAINST_PRINTED_VALUE is
# `local_exec.reconcile`'s. The remaining two are still routed to and then refused, rather
# than being quietly performed as though they were this one.
#
# A between-arms comparison kind (BETWEEN_ARMS, `harness.between_arms`'s) existed here
# once, for the FOCUSED_VALIDATION_EXPERIMENT route; both were removed in the 2026-09-20
# destructive simplification pass after the route never once reached execution across the
# whole measured corpus (every attempt blocked at `no_arms` before a design could even be
# built) — see CLAUDE.md's Known Limitations for the measurement.
RECONCILABLE = ("AGAINST_PRINTED_VALUE",)

_UNSUPPORTED_DETAIL = {
    "AGAINST_EXISTENCE":
        "this comparison asks whether something the claim requires is present, which is "
        "a reading of the artifact rather than an arithmetic against a printed value. "
        "No route in this system produces that answer as admissible evidence yet.",
    "AGAINST_SPECIFICATION":
        "this comparison asks whether an observed procedure matches a specified one, "
        "which is not an arithmetic against a printed value. No route in this system "
        "produces that answer as admissible evidence yet.",
}


def kind_for_route(route: str = "") -> str:
    """The comparison a result obtained by `route` would be subject to, or ''.

    EXACT lookup, no case folding and no stripping, for the same reason
    `provenance.admits` is exact: the answer decides whether a process may start, and a
    token this table does not recognise must fall to "no comparison" rather than be
    repaired into the one kind that can reach a verdict.
    """
    return COMPARISON_FOR_ROUTE.get(route or "", "")


def reconcilable(kind: str = "") -> bool:
    """Does `local_exec.reconcile` have arithmetic for this comparison kind?"""
    return (kind or "") in RECONCILABLE


def needs_printed_value(route: str = "") -> bool:
    """Does a result from this route require a quantity the paper printed?

    Read by `harness.discovery._routes`: a route whose comparison is against a printed
    value is not offered for an object that has none, and a route whose comparison is not
    against one is offered regardless. That single distinction is the whole of what
    `has_value` used to decide for every route at once.
    """
    return kind_for_route(route) == "AGAINST_PRINTED_VALUE"


def derive(route: str = "", *, printed_value_available: bool = False) -> Comparison:
    """The comparison for one route, and whether this run could carry it out.

    Vocabulary strings and a boolean only.

    Three states, and none of them is a finding about the paper: `established` (the
    comparison can be performed), `no_reference` (the paper printed nothing at this
    address to compare against), `unsupported` (this system has no arithmetic for this
    kind of comparison at all).
    """
    kind = kind_for_route(route)
    if not kind:
        return Comparison(kind="", state="unmapped",
                          reason=f"route '{route or 'unset'}' produces nothing to compare.")
    if kind == "AGAINST_PRINTED_VALUE":
        if printed_value_available:
            return Comparison(kind=kind, state="established",
                              measured="the quantity the executed program reports",
                              reference="the quantity the paper printed at the cited address",
                              reason="")
        return Comparison(
            kind=kind, state="no_reference",
            measured="the quantity the executed program reports",
            reference="the quantity the paper printed at the cited address",
            reason=("the paper prints no single unambiguous quantity at the cited address, "
                    "so a run of this route would produce a number with nothing to "
                    "reconcile it against."))
    return Comparison(kind=kind, state="unsupported",
                      measured="what this route would observe",
                      reference="what the claim requires to be there",
                      reason=_UNSUPPORTED_DETAIL[kind])


def admits_verdict(comparison: Comparison | None) -> bool:
    """May a process be started for a spec carrying this comparison?

    True only when the comparison is BOTH established and one this system can actually
    perform. `None` is True and that is deliberate: a spec built before this layer existed
    — a hand-written `spec.json`, a fixture — must keep working exactly as it did, and the
    gates that already stood in front of it (provenance, identity, capability, resources,
    commit) are unchanged.
    """
    if comparison is None:
        return True
    return comparison.established and reconcilable(comparison.kind)


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    # --- the signature discipline, as everywhere else in the pure layer ----------------
    for name, p in inspect.signature(derive).parameters.items():
        assert str(p.annotation) in ("str", "bool", "int"), f"{name}: {p.annotation}"

    # --- the tables are closed --------------------------------------------------------
    assert set(COMPARISON_FOR_ROUTE.values()) <= set(COMPARISON_KINDS) | {""}
    assert set(RECONCILABLE) <= set(COMPARISON_KINDS)
    assert set(_UNSUPPORTED_DETAIL) == set(COMPARISON_KINDS) - set(RECONCILABLE)
    from .artifacts import VERIFICATION_ROUTES
    assert set(COMPARISON_FOR_ROUTE) == set(VERIFICATION_ROUTES), (
        "every route must say what its result is compared against, including NONE")

    # --- exactly one kind reaches an arithmetic ---------------------------------------
    assert reconcilable("AGAINST_PRINTED_VALUE")
    for k in ("AGAINST_EXISTENCE", "AGAINST_SPECIFICATION", "", "x"):
        assert not reconcilable(k), k

    # --- the printed-value requirement is a property of the ROUTE ---------------------
    assert needs_printed_value("AUTHOR_CODE_EXECUTION")
    assert needs_printed_value("INDEPENDENT_RECONSTRUCTION")
    assert not needs_printed_value("ARTIFACT_INSPECTION")

    # --- derivation -------------------------------------------------------------------
    ok = derive("AUTHOR_CODE_EXECUTION", printed_value_available=True)
    assert ok.kind == "AGAINST_PRINTED_VALUE" and ok.established and admits_verdict(ok)

    none_printed = derive("AUTHOR_CODE_EXECUTION", printed_value_available=False)
    assert none_printed.state == "no_reference" and not admits_verdict(none_printed)
    assert "nothing to reconcile it against" in none_printed.reason

    # NO STATE IS BOTH ESTABLISHED AND UNREADABLE. `established` means the comparison can
    # be performed, so AGAINST_PRINTED_VALUE being the only reconcilable kind makes it the
    # only kind that can reach it. A comparison reported established and then refused
    # produces exactly the empty explanation this split was written to remove.
    for route in COMPARISON_FOR_ROUTE:
        for printed in (True, False):
            c = derive(route, printed_value_available=printed)
            assert c.established == admits_verdict(c), (route, printed, c.state)
            assert c.established or c.reason, (route, printed)

    for route in ("ARTIFACT_INSPECTION", "PAPER_INTERNAL_CHECK"):
        c = derive(route)
        assert c.state == "unsupported" and not admits_verdict(c), route
        assert c.reason, route

    assert derive("NONE").state == "unmapped"
    assert derive("not-a-route").state == "unmapped"
    assert set(c.state for c in
               (ok, none_printed, derive("NONE"))) <= set(COMPARISON_STATES)

    # --- an absent comparison changes nothing -----------------------------------------
    assert admits_verdict(None), "a spec built before this layer must behave as it did"
    print("harness.comparison self-check ok")


if __name__ == "__main__":
    _self_check()
