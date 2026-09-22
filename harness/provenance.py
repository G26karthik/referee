"""The provenance ceiling: only admissible provenance may establish or refute a printed
result, in either direction. A probe this harness synthesised can neither convict a paper
nor acquit one. `reimpl_exec`/`cert_exec` are admissible here by membership alone but
gated further downstream (`local_exec.reconcile`, `backends.authorize`) on established
conformance; their label is `INDEPENDENT_REIMPLEMENTATION`/`INDEPENDENT_CERTIFICATE`,
never `AUTHOR_REPOSITORY`, since this is not the authors' own code.

The tuple is deliberately tiny and not configurable: no environment variable widens it,
because a ceiling an operator can raise is not a ceiling.

`python -m harness.provenance` runs the self-check.
"""
from __future__ import annotations

# `driver`: human-written reproduction sealed via `run.py accept`. `repo_exec`: authors'
# own checkout at a verified commit. `reimpl_exec`: harness.reimplement_driver's governed
# reconstruction. `cert_exec`: harness.certificate's EXACT_CERTIFICATE route (a
# self-contained exact-arithmetic script checking every hypothesis a paper's
# theorem/bound states). `synthesized`/`template` are diagnostics: they may inform a
# reader and may never settle a printed number.
ADMISSIBLE_REPRODUCTION_PROVENANCE: tuple[str, ...] = ("driver", "repo_exec", "reimpl_exec",
                                                        "cert_exec")

# Reader-facing name for what actually ran. Fails closed in `label`, so an unrecognised
# token can never be reported as the authors' own code.
PROVENANCE_LABEL = {
    "repo_exec": "AUTHOR_REPOSITORY",
    "driver": "INDEPENDENT_REIMPLEMENTATION",
    "reimpl_exec": "INDEPENDENT_REIMPLEMENTATION",
    "cert_exec": "INDEPENDENT_CERTIFICATE",
    "synthesized": "SYNTHESIZED_DIAGNOSTIC",
    "template": "SYNTHESIZED_DIAGNOSTIC",
}

# Tokens/labels derived from the dict above so other fields describing this vocabulary
# (`TargetOutcome.provenance`, `EvalReport.execution_provenance`) reference one constant
# instead of retyping the enumeration as free prose.
PROVENANCE_VALUES: tuple[str, ...] = tuple(PROVENANCE_LABEL)
PROVENANCE_LABELS: tuple[str, ...] = tuple(dict.fromkeys(PROVENANCE_LABEL.values()))


def admits(provenance: str = "") -> bool:
    """May a reconciliation on this provenance settle a printed quantity, either way?

    EXACT membership: no stripping, no case folding, no normalisation. `" repo_exec"` is
    not `"repo_exec"`; it is an unrecognised token, and an unrecognised token is refused.
    """
    return (provenance or "") in ADMISSIBLE_REPRODUCTION_PROVENANCE


def label(provenance: str = "") -> str:
    """Internal token -> the reader's vocabulary, failing closed to the diagnostic label."""
    return PROVENANCE_LABEL.get(provenance or "", "SYNTHESIZED_DIAGNOSTIC")


def _self_check() -> None:
    assert admits("driver") and admits("repo_exec") and admits("reimpl_exec")
    assert admits("cert_exec")
    # Whitespace-padded and case-shifted forms are UNRECOGNISED, not repaired.
    for bad in ("synthesized", "template", "paper", "", "  ", "DRIVER", "repo-exec",
                " repo_exec", "repo_exec ", " driver", "driver	", "Repo_Exec",
                "REIMPL_EXEC", " reimpl_exec", "reimpl-exec", " cert_exec", "CERT_EXEC",
                "cert-exec", None):
        assert not admits(bad or ""), bad
        assert label(bad or "") == "SYNTHESIZED_DIAGNOSTIC", bad
    # the ceiling is symmetric by construction: one predicate, no direction parameter
    import inspect
    assert set(inspect.signature(admits).parameters) == {"provenance"}

    assert label("repo_exec") == "AUTHOR_REPOSITORY"
    assert label("driver") == "INDEPENDENT_REIMPLEMENTATION"
    assert label("reimpl_exec") == "INDEPENDENT_REIMPLEMENTATION"
    assert label("cert_exec") == "INDEPENDENT_CERTIFICATE"
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
