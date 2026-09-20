"""The one place the provenance ceiling is written down.

`python -m harness.provenance` runs the self-check.

**Invariant 3, and why it needed a home.** Only code the authors published, a
human-written faithful reproduction the operator sealed, or a GOVERNED reconstruction
`harness.reimplement_driver` wrote and bound to the paper ingredient-by-ingredient, may
reconcile against a quantity the paper prints — in EITHER direction. A probe this harness
synthesised can neither convict a paper nor acquit one.

**`reimpl_exec`, and why it is admissible here but gated further downstream.** Decision 1
of the reimplementation revision: a reconstruction may settle a verdict ONLY when every
required ingredient the paper specifies is bound to both a paper locator and a verified
implementation locator (`harness.artifacts.ReimplementationConformance.established`).
That extra condition does not live in this module — `admits` stays a pure membership
test, exactly as it always was — it lives in `local_exec.reconcile` and
`backends.authorize`, the same two places `identities_established` already gates
`repo_exec`. A conformant reconstruction's disagreement establishes a failure of the
paper's STATED METHOD; `PROVENANCE_LABEL['reimpl_exec']` is deliberately the same label as
`'driver'`, `INDEPENDENT_REIMPLEMENTATION`, never `AUTHOR_REPOSITORY` — this is not the
authors' own code and must never be reported as if it failed.

That rule was written out as a literal `("driver", "repo_exec")` at five sites:
`local_exec.reconcile`, `taxonomy.evidence_state`, `backends.authorize`,
`artifacts.TargetOutcome.establishes_failure` and `stages.report`. Five copies of one rule
are one rule and four places for it to drift, and the guard against that drift was a
source-text grep over one of the five. It is one object now, and the test is an identity
assertion.

The tuple is deliberately tiny and deliberately not configurable. There is no environment
variable that widens it, because a ceiling an operator can raise is not a ceiling.
"""
from __future__ import annotations

# `driver` is a human-written reproduction an operator sealed through `run.py accept`;
# `repo_exec` is the authors' own checkout at a verified commit; `reimpl_exec` is
# `harness.reimplement_driver`'s governed reconstruction, admissible here by membership
# alone but refused downstream unless `ReimplementationConformance.established` — see the
# module docstring. Nothing else. In particular `synthesized` (a mechanism reimplementation
# this harness authored from the paper) and `template` (the identical-arms noise floor,
# which measures the host) are diagnostics: they may inform a reader and may never settle
# a printed number.
ADMISSIBLE_REPRODUCTION_PROVENANCE: tuple[str, ...] = ("driver", "repo_exec", "reimpl_exec")

# The reader-facing name for what actually ran. Fails closed in `label`, so an
# unrecognised token can never be reported as the authors' own code.
PROVENANCE_LABEL = {
    "repo_exec": "AUTHOR_REPOSITORY",
    "driver": "INDEPENDENT_REIMPLEMENTATION",
    "reimpl_exec": "INDEPENDENT_REIMPLEMENTATION",
    "synthesized": "SYNTHESIZED_DIAGNOSTIC",
    "template": "SYNTHESIZED_DIAGNOSTIC",
}

# v4 schema pre-work: the plain provenance tokens and their reader-facing labels, DERIVED
# from the dict above rather than retyped, so other fields describing this same
# vocabulary (`TargetOutcome.provenance`, `EvalReport.execution_provenance`) reference one
# constant instead of retyping the enumeration as free prose — which is how
# `TargetOutcome.provenance`'s description drifted to list the tokens in a different order
# than this dict declares them.
PROVENANCE_VALUES: tuple[str, ...] = tuple(PROVENANCE_LABEL)
PROVENANCE_LABELS: tuple[str, ...] = tuple(dict.fromkeys(PROVENANCE_LABEL.values()))


def admits(provenance: str = "") -> bool:
    """May a reconciliation on this provenance settle a printed quantity, either way?

    EXACT membership, deliberately: no stripping, no case folding, no normalisation. A
    ceiling that repairs its input is a ceiling that can be talked around, and the input
    here arrives from a JSON artifact on disk. `" repo_exec"` is not `"repo_exec"`; it is
    an unrecognised token, and an unrecognised token is refused.
    """
    return (provenance or "") in ADMISSIBLE_REPRODUCTION_PROVENANCE


def label(provenance: str = "") -> str:
    """Internal token -> the reader's vocabulary, failing closed to the diagnostic label.

    Exact lookup for the same reason `admits` is exact: whatever this map does not
    recognise must read as a diagnostic, never as the authors' own code.
    """
    return PROVENANCE_LABEL.get(provenance or "", "SYNTHESIZED_DIAGNOSTIC")


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    assert admits("driver") and admits("repo_exec") and admits("reimpl_exec")
    # Whitespace-padded and case-shifted forms are UNRECOGNISED, not repaired. Both
    # functions used to strip, which turned " repo_exec" — a token no producer in this
    # codebase emits, so a sign that something upstream is wrong — into the authors' own
    # repository. `tests/test_reimplementation_path.py` pins both forms.
    for bad in ("synthesized", "template", "paper", "", "  ", "DRIVER", "repo-exec",
                " repo_exec", "repo_exec ", " driver", "driver	", "Repo_Exec",
                "REIMPL_EXEC", " reimpl_exec", "reimpl-exec", None):
        assert not admits(bad or ""), bad
        assert label(bad or "") == "SYNTHESIZED_DIAGNOSTIC", bad
    # the ceiling is symmetric by construction: one predicate, no direction parameter
    import inspect
    assert set(inspect.signature(admits).parameters) == {"provenance"}

    assert label("repo_exec") == "AUTHOR_REPOSITORY"
    assert label("driver") == "INDEPENDENT_REIMPLEMENTATION"
    assert label("reimpl_exec") == "INDEPENDENT_REIMPLEMENTATION"
    for unknown in ("", "synthesized", "template", "who knows"):
        assert label(unknown) == "SYNTHESIZED_DIAGNOSTIC", unknown
    # every admissible provenance has a label that is NOT the fail-closed one, and every
    # label that names author code belongs to an admissible provenance
    for p in ADMISSIBLE_REPRODUCTION_PROVENANCE:
        assert label(p) != "SYNTHESIZED_DIAGNOSTIC", p
    for p, lab in PROVENANCE_LABEL.items():
        if lab in ("AUTHOR_REPOSITORY", "INDEPENDENT_REIMPLEMENTATION"):
            assert admits(p), p
    # a reconstruction is never mistaken for the authors' own code — only 'repo_exec' may
    # ever read as AUTHOR_REPOSITORY, exactly as `test_reimplementation_path.py` pins it
    author = [p for p, lab in PROVENANCE_LABEL.items() if lab == "AUTHOR_REPOSITORY"]
    assert author == ["repo_exec"]
    print("harness.provenance self-check ok")


if __name__ == "__main__":
    _self_check()
