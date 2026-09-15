"""A refused reconciliation must record what identity FOUND, not a field default.

`Reconciliation.experiment_state` / `metric_state` / `configuration_state` default to the
string `"unmapped"`, and they used to be assigned BELOW the provenance ceiling — which
returns for every synthesized and template probe. So on every refused probe all three
kept their defaults, and the defaults were then read as measurements.

Measured on the eight-paper corpus: all 16 reconciliations reported `unmapped` on all
three states, while the specs that produced them recorded three different findings about
three different repositories:

    acl       no_candidate  the cited row is a third-party baseline the authors' code
                            does not implement
    apt-icml  ambiguous     84 advertised commands could emit the metric
    cvpr/iclr unmapped      the prose claim names no population to bind against

"We did not look" and "we looked and the cell is not something their code produces" are
opposite facts about an artifact, and one word was carrying both.

These tests pin the repair and, just as importantly, pin that it changed no decision: the
ceiling still returns first, the status is still INCONCLUSIVE, and nothing became
admissible.
"""
from __future__ import annotations

import pytest

from harness.artifacts import (ConfigurationIdentity, ExperimentIdentity, MetricIdentity,
                               ProbeSpec)
from harness.local_exec import reconcile

VALUES = [0.97, 0.98, 0.99]
BAND = 0.01
SEEDS = [0, 1, 2]


def _spec(provenance: str, experiment: str = "unmapped", metric: str = "unmapped",
          configuration: str = "unmapped") -> ProbeSpec:
    return ProbeSpec(
        paper_id="p", provenance=provenance, script="print('x')",
        table_ref="T1:r0:c0", claimed_cell_value="60.35",
        experiment=ExperimentIdentity(state=experiment, reason="fixture"),
        metric_identity=MetricIdentity(state=metric, reason="fixture"),
        configuration=ConfigurationIdentity(state=configuration, reason="fixture"),
    )


# --------------------------------------------------------------------------- #
# The repair
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provenance", ["synthesized", "template"])
@pytest.mark.parametrize("state", ["no_candidate", "ambiguous", "unsupported", "unmapped"])
def test_a_refused_probe_still_records_what_identity_found(provenance, state):
    """The ceiling returns, and the record still carries the measured state."""
    rec = reconcile(_spec(provenance, experiment=state, metric=state), VALUES, BAND, SEEDS)
    assert rec.status == "INCONCLUSIVE", "the ceiling must still refuse"
    assert rec.experiment_state == state
    assert rec.metric_state == state


def test_the_three_states_are_recorded_independently():
    """One word cannot carry three findings. `acl` really had this shape."""
    rec = reconcile(_spec("synthesized", experiment="no_candidate", metric="ambiguous",
                          configuration="unmapped"), VALUES, BAND, SEEDS)
    assert (rec.experiment_state, rec.metric_state, rec.configuration_state) \
        == ("no_candidate", "ambiguous", "unmapped")


def test_a_missing_identity_object_is_unmapped_and_says_so():
    """`unmapped` remains the honest answer when nothing was assessed at all."""
    spec = ProbeSpec(paper_id="p", provenance="synthesized", script="print('x')")
    rec = reconcile(spec, VALUES, BAND, SEEDS)
    assert rec.experiment_state == "unmapped"
    assert rec.metric_state == "unmapped"
    assert rec.configuration_state == "unmapped"


# --------------------------------------------------------------------------- #
# The repair changed no decision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", ["established", "no_candidate", "ambiguous", "unmapped"])
def test_recording_the_state_never_makes_a_refused_probe_admissible(state):
    """Even fully established identity cannot lift a synthesized probe past the ceiling."""
    # `established` is a derived property of `state`, not a settable field — which is
    # itself the right shape: an identity that could be marked established independently
    # of what it found would be an identity nobody checked.
    spec = _spec("synthesized", experiment=state, metric=state, configuration=state)
    rec = reconcile(spec, VALUES, BAND, SEEDS)
    assert rec.status == "INCONCLUSIVE"
    assert "not the paper's own code" in rec.reason


def test_the_ceiling_still_reports_the_ceiling_and_not_an_identity_class():
    """Ordering: a refused probe must say WHY it was refused, which is provenance."""
    rec = reconcile(_spec("synthesized", experiment="no_candidate"), VALUES, BAND, SEEDS)
    assert rec.failure_class != "experiment_unidentified", (
        "a synthesized probe is refused for what it is, not for what it failed to bind")
    assert "not the paper's own code" in rec.reason


def test_an_admissible_probe_still_gates_on_identity():
    """The identity precondition is unchanged for the provenances the ceiling admits."""
    rec = reconcile(_spec("repo_exec", experiment="no_candidate"), VALUES, BAND, SEEDS)
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "experiment_unidentified"
    assert rec.experiment_state == "no_candidate"


# --------------------------------------------------------------------------- #
# The outcome carries it too
# --------------------------------------------------------------------------- #
def test_the_target_outcome_carries_the_identity_state():
    """`TargetOutcome.identity_state` is what reaches the funnel and the report."""
    from harness.artifacts import ProbeResult
    from harness.stages.probe import outcome_for

    spec = _spec("synthesized", experiment="ambiguous")
    rec = reconcile(spec, VALUES, BAND, SEEDS)
    result = ProbeResult(paper_id="p", provenance="synthesized", reconciliation=rec,
                         experiment=spec.experiment, metric_identity=spec.metric_identity,
                         configuration=spec.configuration, executions=6)
    out = outcome_for("TGT-1", result, "AUTHOR_CODE_REPRODUCTION", "AUTHOR_CODE_EXECUTION")
    assert out.identity_state == "ambiguous"
    assert out.launched == 6, "the launch count is still read off the runner's own record"
    assert not out.establishes_failure, "a synthesized probe convicts nobody"


def test_an_outcome_with_no_identity_object_reports_empty_not_unmapped():
    """Empty means "identity was never reached for this target", which is not the same
    as "identity ran and mapped nothing"."""
    from harness.artifacts import ProbeResult
    from harness.stages.probe import outcome_for

    result = ProbeResult(paper_id="p", provenance="synthesized")
    out = outcome_for("TGT-2", result, "NO_EXPERIMENT_NEEDED", "NONE")
    assert out.identity_state == ""
