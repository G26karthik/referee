"""Which quantity a candidate's code computes, from a real call site — measured inert,
kept as a documented no-op because `experiment_id.describe_command` still calls it.

`python -m harness.alignment.evaluator` runs the self-check.

**What this used to do.** A call to a named metric function (`accuracy_score(...)`) or a
`compute_metrics`-shaped definition is a structural signal a bare quoted key
(`experiment_id.describe_command`'s own key search) cannot fake — a help string or a
docstring containing the word "accuracy" would fool the key search but not a real call
shape. This module found exactly that signal, and only when the key search came up with
nothing (`cmd.emits` empty), so it could only ever ADD a candidate, never override one the
key search already classified.

**Why it is gone.** Re-run live against the four repository papers this harness has
cached checkouts for (167 candidates total, `describe_command` invoked on every one, exact
same call path production uses): the key search left `cmd.emits` empty for a subset of
them, and this module's `find_in_text` matched on ZERO of them — 0 candidates
corroborated, 0 outer identity decisions changed, on every paper. The mechanism is real
and the measurement is honest, but zero effect on every checkout this harness has ever
produced is the same standard `literature.py`/`validation.py`/`claimlink.py` were removed
under (see `CLAUDE.md`'s Known limitations) — 2026-09-21.

`corroborate` stays importable with its original signature because
`harness/experiment_id.py` calls it unconditionally on every candidate; it is now
literally `return cmd` so a future re-measurement on a fifth, different paper is a
one-function change rather than a resurrection.
"""
from __future__ import annotations

from pathlib import Path

from ..schema import CandidateCommand


def corroborate(repo: Path, cmd: CandidateCommand) -> CandidateCommand:
    """No-op, by measurement (see module docstring). Signature preserved for the caller."""
    return cmd


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    cmd = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
    out = corroborate(Path("."), cmd)
    assert out is cmd, "mutates nothing, returns the SAME object"
    assert cmd.emits == [] and cmd.label == "", "measured inert -- never invents a finding"

    already_classified = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1",
                                          emits=["model_flops"], label="flops")
    corroborate(Path("."), already_classified)
    assert already_classified.label == "flops", "an existing finding is never touched"
    print("harness.alignment.evaluator self-check ok")


if __name__ == "__main__":
    _self_check()
