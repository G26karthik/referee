"""The statistics behind the overclaim call.

This is where a bug is most expensive: get the noise band wrong and the harness
either clears a doctored result or accuses an honest one. The degenerate cases below
are not hypothetical — the zero-variance path shipped broken and was caught by the
module self-check, which is why each one has a test now.
"""
from __future__ import annotations

from pathlib import Path

from harness import state
from harness.artifacts import ProbeSpec
from harness.config import Config
from harness.local_exec import _noise, _stats, run_probe, write_probe

ARMS = ["baseline", "treatment"]


def test_stats_of_a_single_value_has_zero_spread_not_an_error():
    s = _stats([0.5])
    assert s.n == 1 and s.mean == 0.5 and s.std == 0.0


def test_stats_of_nothing_is_empty_not_an_error():
    assert _stats([]).n == 0


def test_paired_noise_is_used_when_both_arms_share_seeds():
    per_seed = {"baseline": {0: 0.50, 1: 0.60, 2: 0.70},
                "treatment": {0: 0.52, 1: 0.63, 2: 0.74}}
    std, how = _noise(per_seed, ARMS)
    assert "paired" in how
    # differences are 0.02 / 0.03 / 0.04 — spread 0.01, far below either arm's own 0.1
    assert abs(std - 0.01) < 1e-9, std


def test_zero_paired_variance_falls_back_to_arm_spread():
    """Identical arms have exactly zero paired variance. Dividing by it would make
    any nonzero delta look infinitely significant."""
    per_seed = {"baseline": {0: 0.50, 1: 0.60, 2: 0.70},
                "treatment": {0: 0.50, 1: 0.60, 2: 0.70}}
    std, how = _noise(per_seed, ARMS)
    assert std > 0, "must not report zero noise just because the arms are identical"
    assert "unpaired" in how


def test_unpaired_fallback_takes_the_wider_arm():
    per_seed = {"baseline": {0: 0.1, 1: 0.9}, "treatment": {5: 0.50, 6: 0.51}}
    std, how = _noise(per_seed, ARMS)
    assert "unpaired" in how
    assert std == _stats([0.1, 0.9]).std


def test_noise_of_nothing_is_zero_not_an_error():
    assert _noise({}, ARMS) == (0.0, "unpaired (wider arm spread)")


def test_write_probe_uses_a_supplied_script_verbatim(tmp_path: Path):
    spec = ProbeSpec(paper_id="p", script="print('SH_METRIC arm=a seed=0 value=1.0')\n")
    path = write_probe(tmp_path, spec)
    assert path == tmp_path / "runs" / "p" / "probe.py"
    assert path.read_text(encoding="utf-8") == spec.script


def test_write_probe_renders_the_default_when_no_script_given(tmp_path: Path):
    body = write_probe(tmp_path, ProbeSpec(paper_id="p", claim='has "quotes" in it')
                       ).read_text(encoding="utf-8")
    compile(body, "probe.py", "exec")
    assert "SH_METRIC" in body


def _script(tmp_path: Path, body: str) -> ProbeSpec:
    return ProbeSpec(paper_id="p", seeds=[0, 1, 2], script=body)


DELTA_SCRIPT = """\
import argparse
p = argparse.ArgumentParser(); p.add_argument("--seed", type=int); p.add_argument("--arm")
a = p.parse_args()
print("SH_DEVICE cpu")
base = [0.50, 0.60, 0.70][a.seed]
print(f"SH_METRIC arm={a.arm} seed={a.seed} value=" + str(base + (%s if a.arm == "treatment" else 0)))
"""


def test_a_delta_inside_the_noise_band_is_called_overclaimed(tmp_path: Path):
    # arms differ by a constant 0.001; paired spread is 0 so the fallback arm spread
    # (~0.1) dominates and the effect is correctly judged undetectable.
    res = run_probe(Config.load(), tmp_path, _script(tmp_path, DELTA_SCRIPT % "0.001"))
    assert res.verdict == "within_noise", res.reason
    assert res.is_overclaimed is True
    assert abs(res.measured_delta - 0.001) < 1e-6


def test_a_probe_that_cannot_run_reports_failed_not_a_verdict(tmp_path: Path):
    res = run_probe(Config.load(), tmp_path, _script(tmp_path, "import sys; sys.exit(3)\n"))
    assert res.verdict == "failed"
    assert res.is_overclaimed is None, "a failed probe must not accuse anyone"
    assert res.seeds_failed


def test_claimed_delta_is_tested_against_the_measured_band(tmp_path: Path):
    spec = _script(tmp_path, DELTA_SCRIPT % "0.001")
    spec.claimed_delta = 0.5              # enormous relative to a ~0.1 noise band
    res = run_probe(Config.load(), tmp_path, spec)
    assert res.claim_within_noise is False, "a 0.5 claim is far outside this band"

    spec2 = _script(tmp_path, DELTA_SCRIPT % "0.001")
    spec2.paper_id, spec2.claimed_delta = "q", 0.0001
    res2 = run_probe(Config.load(), tmp_path, spec2)
    assert res2.claim_within_noise is True, "a 0.0001 claim is inside this band"


def test_results_are_written_to_control_dir(tmp_path: Path):
    res = run_probe(Config.load(), tmp_path, _script(tmp_path, DELTA_SCRIPT % "0.001"))
    out = state.control_dir(tmp_path) / "probe_results.json"
    assert out.exists(), "probe_results.json must be written under state.control_dir(root)"
    assert res.script_path.endswith("probe.py")
    assert res.device == "cpu"
