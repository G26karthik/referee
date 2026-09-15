"""Which target to pursue first, and why — as a function, not as a display sort.

`python -m harness.priority` runs the self-check.

Target order used to be `stages/report.finding_key`, a sort built to decide what appears
first in a rendered report: severity, then evidence strength, then a lens tiebreak. That
is a reasonable way to order a document and a poor way to spend a GPU-hour. It knows
nothing about whether the artifact exists, whether the experiment's identity is
established, what a route can establish, or what it costs.

**Lexicographic, and the order of the keys is the policy.**

    1. centrality        how much the paper's conclusion rests on this
    2. addressability    can the harness check it at all
    3. decisiveness      can the cheapest admissible route change the paper-level triage
    4. identity          is what would run bound to what was printed
    5. artifact          does the authors' own code exist for it
    6. cheapness         TIEBREAK ONLY

Cheapness sits at the bottom on purpose. "Prefer cheap, high-information targets when
scientifically equivalent" is exactly a tiebreak: equivalence is every key above being
equal. Putting cost any higher would let the system pick an easy peripheral target over a
hard central one and then report a reproduction rate — which is how a benchmark score
gets optimised instead of a paper getting reviewed.

**The signature is the invariant**, as in `harness.grading.derive`. Every parameter is a
closed-vocabulary string or a bool. No count, no number, no metric name, no paper
identity, so "if <paper>, prefer target 2" and "if the delta exceeds 5 points, promote"
are inexpressible rather than merely absent.
"""
from __future__ import annotations

_CENTRALITY = {"CENTRAL": 3, "SUPPORTING": 2, "PERIPHERAL": 1, "UNASSESSED": 0}

# What the cheapest admissible route could ESTABLISH, not what it costs. A route that can
# only raise a concern is worth less here than one that can settle a claim, because the
# purpose of spending the budget is to move something from unresolved to resolved.
_DECISIVENESS = {
    "PAPER_INTERNAL_CHECK": 3,           # can establish a contradiction outright
    "ARITHMETIC_RECHECK": 3,             # ditto, and costs nothing
    "AUTHOR_CODE_EXECUTION": 3,          # the only route that can convict on the authors' code
    "FOCUSED_VALIDATION_EXPERIMENT": 2,  # can discriminate explanations, cannot convict alone
    "INDEPENDENT_RECONSTRUCTION": 2,     # bounded by invariant 15
    "ARTIFACT_INSPECTION": 1,            # can raise a concern, cannot settle a printed number
    "LITERATURE_SEARCH": 1,
    "NONE": 0,
}

# Relative cost of the cheapest admissible route, inverted so higher is cheaper.
_CHEAPNESS = {
    "PAPER_INTERNAL_CHECK": 3, "ARITHMETIC_RECHECK": 3,
    "ARTIFACT_INSPECTION": 2, "LITERATURE_SEARCH": 2,
    "AUTHOR_CODE_EXECUTION": 1, "FOCUSED_VALIDATION_EXPERIMENT": 0,
    "INDEPENDENT_RECONSTRUCTION": 0, "NONE": 0,
}

_IDENTITY = {"established": 3, "ambiguous": 1, "unmapped": 0, "": 0}

# Base larger than any field's maximum, so the weighted sum is provably lexicographic:
# no combination of lower keys can ever outweigh one step of a higher key.
_BASE = 8
_FIELDS = 6


def score(*, centrality: str, addressable: bool, cheapest_route: str,
          identity_state: str = "", artifact_available: bool = False) -> tuple[float, str]:
    """(priority, reason). Deterministic once its inputs are fixed.

    A model may argue that a target matters; that argument reaches this function only as
    `centrality`, which `harness.discovery._centrality` derives from structure. A rationale
    a model writes is metadata and is stored beside the score, never inside it.
    """
    values = [
        _CENTRALITY.get(centrality, 0),
        1 if addressable else 0,
        _DECISIVENESS.get(cheapest_route, 0),
        _IDENTITY.get(identity_state, 0),
        1 if artifact_available else 0,
        _CHEAPNESS.get(cheapest_route, 0),
    ]
    total = 0.0
    for i, v in enumerate(values):
        total += min(v, _BASE - 1) * (_BASE ** (_FIELDS - 1 - i))
    reason = (f"centrality={centrality or 'UNASSESSED'}; "
              f"addressable={'yes' if addressable else 'no'}; "
              f"route={cheapest_route or 'NONE'}; "
              f"identity={identity_state or 'unmapped'}; "
              f"artifact={'yes' if artifact_available else 'no'}")
    return round(total / (_BASE ** _FIELDS), 6), reason


def order(objects, *, identity_state: str = "", artifact_available: bool = False):
    """Score every discovered object in place and return them, highest priority first.

    The final tiebreak is `target_id`, which is derived from the object's own address, so
    the ordering is total and therefore reproducible across runs.
    """
    # The route the PLANNER would take, not the first one discovery appended. Scoring on
    # `routes[0]` ranked every target by a route that was never chosen — see
    # `planner.route_that_would_be_taken` for the corpus measurement — and the target
    # budget truncates this ordering, so the mismatch decided which targets were dropped.
    from .planner import route_that_would_be_taken
    for o in objects:
        o.priority, o.priority_reason = score(
            centrality=o.centrality, addressable=bool(o.harness_addressable),
            cheapest_route=route_that_would_be_taken(tuple(o.routes or ())),
            identity_state=identity_state, artifact_available=artifact_available)
    return sorted(objects, key=lambda o: (-o.priority, o.target_id))


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import DiscoveredObject

    central_hard, _ = score(centrality="CENTRAL", addressable=True,
                            cheapest_route="AUTHOR_CODE_EXECUTION")
    peripheral_free, _ = score(centrality="PERIPHERAL", addressable=True,
                               cheapest_route="ARITHMETIC_RECHECK")
    assert central_hard > peripheral_free, (
        "a cheap peripheral target must never outrank an expensive central one")

    # equivalence: everything above cheapness equal, so cost decides
    a, _ = score(centrality="CENTRAL", addressable=True, cheapest_route="PAPER_INTERNAL_CHECK")
    b, _ = score(centrality="CENTRAL", addressable=True, cheapest_route="ARITHMETIC_RECHECK")
    assert a == b, "two equally decisive, equally cheap routes tie"

    unaddressable, _ = score(centrality="CENTRAL", addressable=False, cheapest_route="NONE")
    addressable, _ = score(centrality="SUPPORTING", addressable=True,
                           cheapest_route="ARTIFACT_INSPECTION")
    assert unaddressable > addressable, "centrality outranks addressability, by design"

    # ...but within one centrality, an addressable target wins
    x, _ = score(centrality="CENTRAL", addressable=True, cheapest_route="ARTIFACT_INSPECTION")
    y, _ = score(centrality="CENTRAL", addressable=False, cheapest_route="NONE")
    assert x > y

    objs = [DiscoveredObject(target_id="TGT-B", centrality="PERIPHERAL",
                             harness_addressable=True, routes=["ARITHMETIC_RECHECK"]),
            DiscoveredObject(target_id="TGT-A", centrality="CENTRAL",
                             harness_addressable=True, routes=["AUTHOR_CODE_EXECUTION"])]
    assert [o.target_id for o in order(objs)] == ["TGT-A", "TGT-B"]
    assert order(objs) == order(objs), "ordering is total and reproducible"

    # the signature admits vocabulary and booleans only
    # `from __future__ import annotations` leaves these as strings, which is what the
    # equivalent assertion in tests/test_reasoning_architecture.py reads too.
    for name, p in inspect.signature(score).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"
    print("harness.priority self-check ok")


if __name__ == "__main__":
    _self_check()
