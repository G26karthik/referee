"""The eight release blockers, each pinned by the shape that reproduced it.

Every test here was written from a live reproduction against the real tree, not from a
reading of the code. The docstrings record the wrong output that was actually observed,
because the value of these tests is that they fail loudly if the behaviour returns — and a
reader needs to know what "returns" looks like.

The through-line: seven of the eight were the system being *confidently wrong* rather than
crashing. A false attestation, a verdict that depended on seed order, a review of the wrong
paper, three seeds from one process, a clean-tree claim about a tree nobody inspected, an
accusation against authors whose code never ran, and a reproduction verdict announced over
an INCONCLUSIVE. None of them raised. That is why they needed an adversarial pass to find.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from harness import probe_synth, repo as repo_mod
from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, ExperimentalChain, MetricIdentity,
                               PaperDoc, ProbeSpec, ResourceCapability, Section, Table)
from harness.backends import authorize, local_backend
from harness.config import Config
from harness.local_exec import StartupEvidence, reached_experiment, reconcile, run_probe
from harness.repo import TreeUninspectable
from harness.stages.audit import source_units, verify_evidence
from harness.stages.ingest import _same_paper_by_content, allocate_paper_id
from harness.stages.report import _chain_block, overall_verdict


def _cfg(tmp_path: Path, **over) -> Config:
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.probe_timeout_s = 60
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _est(cls):
    return cls(state="established", established=True, reason="fixture")


# C4 (this pass): `reconcile` now requires experiment/metric/configuration identity for
# ANY reconciliation-eligible provenance, not only a `command`-bearing one — see
# test_experiment_identity.py::test_a_driver_script_with_no_command_still_needs_an_identity_chain.
# The tests below are about order-independence, crash classification and the SH_METRIC
# contract, not about identity, so they carry it established as a fixture.
_ESTABLISHED_IDENTITY = dict(
    experiment=_est(ExperimentIdentity), metric_identity=_est(MetricIdentity),
    configuration=_est(ConfigurationIdentity))


# --------------------------------------------------------------------------- #
# D1 — evidence must occur inside ONE source unit
# --------------------------------------------------------------------------- #
_A = "The proposed method reaches 91.4 accuracy on the held-out split."
_B = "Related work has explored dimensional collapse in self-supervised learning."


def _two_section_doc() -> tuple[tuple[str, ...], dict]:
    doc = PaperDoc(
        paper_id="p", title="T",
        sections=[Section(section_idx=0, title="Results", page_start=3, text=_A),
                  Section(section_idx=1, title="Related", page_start=4, text=_B)],
        tables=[Table(table_idx=0, page=3, caption="Table 1",
                      rows=[["method", "acc"], ["ours", "91.4"]])])
    return source_units(doc), {t.table_idx: t for t in doc.tables}


def test_a_quote_spanning_two_sections_is_not_verified():
    """The trust anchor. `corpus` was one concatenated, whitespace-stripped string, so a
    string straddling a section boundary was a substring of it.

    On the real APT paper the join of section 0's tail and section 1's head is
    `'owen Zhao 1 Hannaneh Hajishirzi 1 2 Qingqing Cao*3Fine-tuning and inference'` — text
    that does not occur in the PDF. It came back `prose_verified`, with the harness's own
    machine-written observation stating it "occurs verbatim in the parsed section text".
    Four separate seams verified. This is the one place the design says the machine, not
    the model, authors the observation.
    """
    units, by_idx = _two_section_doc()
    seam = _A[-30:] + _B[:30]
    klass, obs = verify_evidence(seam, "p3", units, by_idx)
    assert klass == "unverified" and obs == ""


def test_a_quote_inside_one_section_still_verifies():
    """The fix must not have bought integrity by refusing real evidence."""
    units, by_idx = _two_section_doc()
    klass, obs = verify_evidence(_A, "p3", units, by_idx)
    assert klass == "prose_verified"
    assert "single parsed section" in obs, "the observation must say what it checked"


def test_cell_verification_is_untouched():
    units, by_idx = _two_section_doc()
    assert verify_evidence("91.4", "T0:r1:c1", units, by_idx)[0] == "cell_verified"
    assert verify_evidence("91.4", "T0:r0:c1", units, by_idx)[0] == "unverified"


def test_a_unit_carries_the_sections_own_index_not_its_position():
    """The first version of this fix reported the TUPLE POSITION as the section identity.

    `source_units` skips empty sections, so the two diverge, and on the real APT paper
    (59 sections, 4 empty) that misaddressed 12 of 12 prose findings — the observation
    said "#50" for text living in section 53. Replacing one false machine attestation with
    another is not a fix, so the index travels with the text.
    """
    doc = PaperDoc(paper_id="p", title="T", sections=[
        Section(section_idx=0, title="a", text=_A), Section(section_idx=1, title="b", text=""),
        Section(section_idx=2, title="c", text=_B)])
    units = source_units(doc)
    assert len(units) == 2, "an empty section is not a unit"
    assert [i for i, _ in units] == [0, 2], "the gap is real and must be preserved"

    # And the address the harness states must be the one a reader can open.
    _, obs = verify_evidence(_B, "p4", units, {})
    assert "section_idx 2" in obs, obs
    by_idx = {s.section_idx: s.text for s in doc.sections}
    assert _B in by_idx[2], "the stated address really contains the quote"


def test_the_observation_no_longer_implies_the_page_was_checked():
    """`_PAGE_REF` only checks the SHAPE `p\\d+`, never that the cited page falls within
    the section that actually holds the quote — that precision tradeoff is deliberate
    (a section can span several pages) and still stands. What the observation may not do
    is imply otherwise, so it says the citation is not checked and names what is.

    (C8, this pass: an IMPOSSIBLE page — one beyond the paper's own page count — is a
    different question from precision, and is now checked when a caller supplies
    `max_page`; see test_an_impossible_page_reference_is_rejected below.)
    """
    units, by_idx = _two_section_doc()
    _, obs = verify_evidence(_A, "p9999", units, by_idx)
    assert "NOT checked" in obs and "section_idx 0" in obs


def test_an_impossible_page_reference_is_rejected():
    """C8 — a page number beyond the paper's own page count cannot be evidence for
    anything: this paper does not have that page. Checked only when `max_page` is
    supplied, which `load_reports` always does for a real review."""
    units, by_idx = _two_section_doc()
    ok = verify_evidence(_A, "p3", units, by_idx, max_page=4)
    assert ok[0] == "prose_verified", ok
    impossible = verify_evidence(_A, "p9999", units, by_idx, max_page=4)
    assert impossible[0] == "unverified", impossible
    zero = verify_evidence(_A, "p0", units, by_idx, max_page=4)
    assert zero[0] == "unverified", zero


# --------------------------------------------------------------------------- #
# D2 — infrastructure and our own clock may never convict
# --------------------------------------------------------------------------- #
def test_a_harness_timeout_is_never_a_reach():
    """`cfg.probe_timeout_s` is OUR clock. The process was alive and printing when we
    killed it, so nothing was established about whether the experiment would finish.

    Observed before: a run with a contract line, 40 lines of output and 1800s that was
    killed by the timeout reconciled as FAILED_REPRODUCTION / runtime_failure and drove RED.
    """
    ev = StartupEvidence(saw_contract_line=True, stdout_lines=40, ran_seconds=1800,
                         timed_out=True)
    assert reached_experiment(ev) is False

    spec = ProbeSpec(paper_id="t", provenance="repo_exec", command=["python", "run.py"],
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     capability=ExecCapability(established=True, reason_code="established"),
                     experiment=_est(ExperimentIdentity),
                     metric_identity=_est(MetricIdentity),
                     configuration=_est(ConfigurationIdentity))
    rec = reconcile(spec, [], 0.0, [0], failure="timeout after 1800s", evidence=ev)
    assert rec.status == "INCONCLUSIVE" and rec.failure_class == "timeout"
    assert overall_verdict([], rec)[0] != "RED"


_ORDER_FIXTURE = """import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", default="reproduction")
a = ap.parse_args()
if a.seed == {ok}:
    print("SH_DEVICE cpu")
    print(f"SH_METRIC arm={{a.arm}} seed={{a.seed}} value=59.2800")
else:
    import a_module_that_does_not_exist
"""


@pytest.mark.parametrize("ok_seed", [0, 1, 2])
def test_a_dependency_failure_is_inconclusive_whichever_seed_hits_it(tmp_path, ok_seed):
    """Classification must not depend on the order the seed loop ran in.

    The classifier was skipped once ANY attempt had emitted a contract line, so the
    verdict was decided by which seed came first. Same repository, same missing package,
    same seed set — measured: succeed-then-fail gave FAILED_REPRODUCTION and RED,
    fail-then-succeed gave INCONCLUSIVE and GREEN. No principle makes both correct.
    """
    spec = ProbeSpec(paper_id="t", seeds=[0, 1, 2], arms=["reproduction"],
                     provenance="driver", table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     script=_ORDER_FIXTURE.format(ok=ok_seed), **_ESTABLISHED_IDENTITY)
    result = run_probe(_cfg(tmp_path), tmp_path, spec)
    rec = result.reconciliation
    assert rec is not None
    assert rec.status == "INCONCLUSIVE", rec.reason
    assert rec.failure_class in ("startup_failure", "dependency_missing"), rec.failure_class


def test_a_crash_after_a_real_start_still_convicts(tmp_path):
    """The fix must not have made every failure inconclusive. A genuine runtime crash
    that strikes BEFORE the experiment ever prints its measurement, with no setup
    signature anywhere, is still a failed reproduction.

    (C3 correction, this pass: a crash AFTER the metric was already printed is a
    different case — see test_an_honest_probe_still_measures's sibling in
    test_local_execution.py — because the measurement itself is already complete by
    then. This fixture crashes first, so it stays a genuine conviction.)
    """
    spec = ProbeSpec(paper_id="t", seeds=[0, 1], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     script=("import argparse\n"
                             "ap = argparse.ArgumentParser()\n"
                             "ap.add_argument('--seed', type=int, required=True)\n"
                             "ap.add_argument('--arm', default='reproduction')\n"
                             "a = ap.parse_args()\n"
                             "print('SH_DEVICE cpu')\n"
                             "raise RuntimeError('loss became NaN at step 4000')\n"),
                     **_ESTABLISHED_IDENTITY)
    rec = run_probe(_cfg(tmp_path), tmp_path, spec).reconciliation
    assert rec is not None and rec.status == "FAILED_REPRODUCTION"
    assert rec.failure_class == "runtime_failure"


# --------------------------------------------------------------------------- #
# D3 — a missing hash is not proof of identity
# --------------------------------------------------------------------------- #
def _legacy(cfg: Config, pid: str, title: str) -> Path:
    d = cfg.projects_dir / pid / "paper"
    d.mkdir(parents=True, exist_ok=True)
    doc = d / "doc.json"
    doc.write_text(json.dumps({"paper_id": pid, "title": title, "content_sha": None}),
                   encoding="utf-8")
    return doc


def test_a_legacy_project_does_not_absorb_a_different_paper(tmp_path):
    """`recorded in ("", sha)` treated an absent hash as a match.

    Measured: a legacy `apt-icml` with content_sha=None accepted an unrelated PDF whose
    filename slugified the same way, `run_ingest` returned `cached: True`, and the
    controller drove a complete review of the new paper against the old doc.json, the old
    lens files and the old findings. An absent hash is the absence of evidence.
    """
    cfg = _cfg(tmp_path)
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    _legacy(cfg, "apt-icml", "APT: Adaptive Pruning and Tuning")
    stranger = tmp_path / "APT _ ICML.pdf"
    stranger.write_bytes(b"%PDF-1.4 an entirely different document")

    pid, same = allocate_paper_id(cfg, stranger, "f" * 40)
    assert same is False, "a different document must not be reported as the same paper"
    assert pid != "apt-icml", "it must get its own identity"
    assert (cfg.projects_dir / "apt-icml" / "paper" / "doc.json").exists(), \
        "the legacy project must be preserved, not overwritten"


def test_a_different_content_hash_forces_a_new_identity(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    d = cfg.projects_dir / "paper" / "paper"
    d.mkdir(parents=True)
    (d / "doc.json").write_text(json.dumps({"content_sha": "a" * 40}), encoding="utf-8")
    src = tmp_path / "paper.pdf"
    src.write_bytes(b"%PDF new")
    pid, same = allocate_paper_id(cfg, src, "b" * 40)
    assert not same and pid != "paper"


def test_the_same_paper_supplied_twice_reuses_its_case(tmp_path):
    """Idempotence must survive the fix: a recorded hash that matches is still a match."""
    cfg = _cfg(tmp_path)
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    d = cfg.projects_dir / "paper" / "paper"
    d.mkdir(parents=True)
    (d / "doc.json").write_text(json.dumps({"content_sha": "a" * 40}), encoding="utf-8")
    src = tmp_path / "paper.pdf"
    src.write_bytes(b"%PDF same")
    pid, same = allocate_paper_id(cfg, src, "a" * 40)
    assert same is True and pid == "paper"


def test_a_legacy_case_is_reused_only_on_content_agreement(tmp_path):
    """The one way a legacy project may still be reused: the parser recovers the same
    title from the new PDF that the legacy artifact recorded. Anything less is refused."""
    cfg = _cfg(tmp_path)
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    doc = _legacy(cfg, "x", "A Very Specific Title")
    src = tmp_path / "x.pdf"
    src.write_bytes(b"not a parseable pdf")
    assert _same_paper_by_content(doc, src) is False, "an unparseable PDF establishes nothing"

    blank = _legacy(cfg, "y", "")
    assert _same_paper_by_content(blank, src) is False, "a legacy doc with no title cannot match"


# --------------------------------------------------------------------------- #
# D4 — one process is one seed
# --------------------------------------------------------------------------- #
_LIAR = """import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", default="reproduction")
a = ap.parse_args()
print("SH_DEVICE cpu")
for s, v in ((0, 59.27), (1, 59.28), (2, 59.29)):
    print(f"SH_METRIC arm=reproduction seed={s} value={v:.4f}")
"""


def test_one_process_cannot_fill_three_seed_slots(tmp_path):
    """`per_seed` was keyed on the seed the PROGRAM asserted, not the one passed on argv.

    Measured: harness requests seeds=[0], one process runs, and it prints three
    `seed=`/`value=` lines. Result was seeds_run=[0, 1, 2] with a fabricated spread of
    0.01 — which then satisfied the `std <= 0` guard that exists precisely to catch a
    pipeline the seed does not perturb — and RESOLVED_VERIFIED from a single execution.
    """
    spec = ProbeSpec(paper_id="t", seeds=[0], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28", script=_LIAR)
    result = run_probe(_cfg(tmp_path), tmp_path, spec)
    assert result.executions == 1
    assert result.seeds_run == [0], "a seed slot needs a process"
    assert result.reconciliation is not None
    assert result.reconciliation.status != "RESOLVED_VERIFIED"
    assert "did not request" in (result.reason or ""), "the discarded lines must be reported"


def test_a_mislabelled_arm_is_also_discarded(tmp_path):
    spec = ProbeSpec(paper_id="t", seeds=[0], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     script=("import argparse\n"
                             "ap = argparse.ArgumentParser()\n"
                             "ap.add_argument('--seed', type=int, required=True)\n"
                             "ap.add_argument('--arm', default='reproduction')\n"
                             "a = ap.parse_args()\n"
                             "print('SH_DEVICE cpu')\n"
                             "print('SH_METRIC arm=some_other_arm seed=0 value=59.28')\n"))
    result = run_probe(_cfg(tmp_path), tmp_path, spec)
    assert result.seeds_run == [], "a metric for an arm we did not request is not a measurement"


def test_an_honest_probe_still_measures(tmp_path):
    """The fix must not have broken the contract for probes that echo correctly."""
    spec = ProbeSpec(paper_id="t", seeds=[0, 1, 2], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     script=("import argparse\n"
                             "ap = argparse.ArgumentParser()\n"
                             "ap.add_argument('--seed', type=int, required=True)\n"
                             "ap.add_argument('--arm', default='reproduction')\n"
                             "a = ap.parse_args()\n"
                             "print('SH_DEVICE cpu')\n"
                             "print(f'SH_METRIC arm={a.arm} seed={a.seed} "
                             "value={59.28 + a.seed * 0.01:.4f}')\n"),
                     **_ESTABLISHED_IDENTITY)
    result = run_probe(_cfg(tmp_path), tmp_path, spec)
    assert result.seeds_run == [0, 1, 2] and result.executions == 3
    assert result.reconciliation is not None
    assert result.reconciliation.status == "RESOLVED_VERIFIED"


# --------------------------------------------------------------------------- #
# D5 — commit verification fails closed
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    p = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args], cwd=str(repo),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr


@pytest.fixture()
def checkout(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "c")
    return repo, repo_mod.head_commit(repo)


def test_an_uninspectable_tree_is_not_a_clean_tree(checkout, monkeypatch):
    """`dirty_files` returned [] on ANY `git status` failure, which reads as "no files
    differ". Measured: with `git status` exiting 128, `verify_commit` returned
    state='verified' with the sentence "the checkout is exactly the audited commit <sha>
    with a clean working tree" — a positive false attestation about a tree nobody looked
    at, persisted into probe_results.json and rendered as a satisfied chain link.
    """
    repo, sha = checkout
    real = repo_mod._git

    def broken(args, cwd, timeout):
        if args and args[0] == "status":
            return subprocess.CompletedProcess(args, 128, "", "fatal: index file corrupt")
        return real(args, cwd, timeout)

    monkeypatch.setattr(repo_mod, "_git", broken)
    with pytest.raises(TreeUninspectable):
        repo_mod.dirty_files(repo)
    ver = repo_mod.verify_commit(repo, sha)
    assert ver.state == "unknown" and not ver.established
    assert "not a clean tree" in ver.reason


def test_a_clean_tree_at_the_audited_commit_still_verifies(checkout):
    repo, sha = checkout
    assert repo_mod.verify_commit(repo, sha).state == "verified"


def test_a_dirty_tree_still_blocks(checkout):
    repo, sha = checkout
    (repo / "f.py").write_text("x = 999\n", encoding="utf-8")
    ver = repo_mod.verify_commit(repo, sha)
    assert ver.state == "dirty" and ver.dirty_files == ["f.py"]


def test_a_moved_commit_still_blocks(checkout):
    repo, sha = checkout
    (repo / "f.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "moved")
    assert repo_mod.verify_commit(repo, sha).state == "mismatch"


def test_a_non_git_directory_is_unknown_not_verified(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    ver = repo_mod.verify_commit(d, "a" * 40)
    assert ver.state == "unknown" and not ver.established


# --------------------------------------------------------------------------- #
# D6 — no paper-specific synthesis (see also tests/test_probe_synthesis.py)
# --------------------------------------------------------------------------- #
def test_the_pilot_corpus_receives_no_special_treatment():
    for title in ("LDReg: Local Dimensionality Regularized Self-Supervised Learning",
                  "APT: Adaptive Pruning and Tuning Pretrained Language Models",
                  "SAPG: Split and Aggregate Policy Gradients",
                  "Stay on topic with Classifier-Free Guidance"):
        doc = PaperDoc(paper_id="p", title=title, sections=[
            Section(section_idx=0, title="Intro", text="local intrinsic dimensionality "
                                                       "and dimensional collapse")])
        assert probe_synth.plan(doc, claim="c").mechanism == "placebo", title


# --------------------------------------------------------------------------- #
# D7 / D8 — the report may not assert what it did not establish
# --------------------------------------------------------------------------- #
def test_a_driver_failure_is_not_attributed_to_the_authors():
    """`overall_verdict` hard-coded "The paper's own code does not reproduce the number it
    prints" into every FAILED_REPRODUCTION. The harness's own vocabulary defines `driver`
    as a human-written spec.json and NOT the authors' checkout, and `authorize` reports
    exactly that spec as `not_repo_execution`."""
    spec = ProbeSpec(paper_id="t", provenance="driver", script="print(1)",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28", arms=["a"],
                     **_ESTABLISHED_IDENTITY)
    rec = reconcile(spec, [12.0, 12.1, 11.9], 0.01, [0, 1, 2])
    assert rec.status == "FAILED_REPRODUCTION"
    verdict, why = overall_verdict([], rec)
    assert verdict == "RED"
    assert "not the authors' checkout" in why
    assert "paper's own code" not in why


def test_a_repo_exec_failure_is_attributed_to_the_repository():
    spec = ProbeSpec(paper_id="t", provenance="repo_exec", command=["python", "e.py"],
                     table_ref="T1:r0:c1", claimed_cell_value="59.28", arms=["a"],
                     experiment=_est(ExperimentIdentity),
                     metric_identity=_est(MetricIdentity),
                     configuration=_est(ConfigurationIdentity))
    rec = reconcile(spec, [12.0, 12.1, 11.9], 0.01, [0, 1, 2])
    assert rec.status == "FAILED_REPRODUCTION"
    assert "audited repository" in overall_verdict([], rec)[1]


@pytest.mark.parametrize("status,expect_verdict", [
    ("RESOLVED_VERIFIED", True), ("FAILED_REPRODUCTION", True), ("INCONCLUSIVE", False),
    ("", False),
])
def test_a_complete_chain_only_claims_a_verdict_when_there_is_one(status, expect_verdict):
    """`_chain_block` printed "Every link established, so the reconciliation above is a
    real reproduction verdict" whenever `broken_link` was empty — including over an
    INCONCLUSIVE, which the same module defines as "no reproduction verdict can be drawn".
    `reconcile` refuses for reasons that break no chain link at all: a units mismatch, a
    zero noise band, an unparseable cell.
    """
    body = "\n".join(_chain_block(ExperimentalChain(
        reconciliation=status, broken_link="", commit_state="verified")))
    if expect_verdict:
        assert "real reproduction verdict" in body and status in body
    else:
        assert "no reproduction verdict follows" in body
        assert "real reproduction verdict" not in body


def test_a_broken_chain_still_names_its_first_broken_link():
    body = "\n".join(_chain_block(ExperimentalChain(
        reconciliation="INCONCLUSIVE", broken_link="experiment identity")))
    assert "breaks at **experiment identity**" in body


# --------------------------------------------------------------------------- #
# M2 — the assessed backend must be the authorized backend
# --------------------------------------------------------------------------- #
def _qualified(backend: str) -> ProbeSpec:
    return ProbeSpec(
        paper_id="t", command=["python", "e.py"], provenance="repo_exec", backend=backend,
        experiment=_est(ExperimentIdentity), metric_identity=_est(MetricIdentity),
        configuration=_est(ConfigurationIdentity),
        capability=ExecCapability(established=True, reason_code="established"),
        resources=ResourceCapability(state="satisfied", reason="fits"))


def _verified() -> CommitVerification:
    return CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)


def test_an_assessment_for_one_backend_does_not_authorize_another():
    """Capability and resources are assessed against the backend `select_for` chose and
    recorded on `spec.backend`. `authorize` read the records without checking they were
    about the backend in front of it — so an 8 GiB `satisfied` could authorize a run on a
    different machine, and a `win32` capability could authorize a `linux` one."""
    cfg = Config.load()
    cfg.allow_repo_exec = True
    auth = authorize(cfg, _qualified("kaggle"), local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "backend_mismatch"
    assert auth.failure_class == "execution_unauthorized", "a fact about us, not the paper"


def test_a_matching_backend_is_still_authorized():
    cfg = Config.load()
    cfg.allow_repo_exec = True
    auth = authorize(cfg, _qualified("local"), local_backend(), commit=_verified())
    assert auth.allowed and auth.decision == "authorized"


def test_a_spec_with_no_planned_backend_is_still_authorized():
    """A spec that never recorded a backend cannot contradict one. The check is for a
    MISMATCH, not for a missing field — tightening that would refuse every hand-written
    spec.json, which is a documented channel."""
    cfg = Config.load()
    cfg.allow_repo_exec = True
    spec = _qualified("")
    auth = authorize(cfg, spec, local_backend(), commit=_verified())
    assert auth.allowed and auth.decision == "authorized"


# --------------------------------------------------------------------------- #
# Regressions introduced BY the correction pass, found by re-attacking the fixes
# --------------------------------------------------------------------------- #
def test_a_refused_metric_line_is_not_reach_evidence(tmp_path):
    """`saw_contract_line` was set BEFORE the label check, so a line the harness had just
    refused as "not a measurement to accept" still satisfied `reached_experiment` — the
    same fabricated output that could no longer fill a seed slot could still license a
    FAILED_REPRODUCTION."""
    spec = ProbeSpec(paper_id="t", seeds=[0], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     script=("import argparse\n"
                             "ap = argparse.ArgumentParser()\n"
                             "ap.add_argument('--seed', type=int, required=True)\n"
                             "ap.add_argument('--arm', default='reproduction')\n"
                             "a = ap.parse_args()\n"
                             "print('SH_METRIC arm=reproduction seed=7 value=59.28')\n"
                             "raise SystemExit(1)\n"))
    rec = run_probe(_cfg(tmp_path), tmp_path, spec).reconciliation
    assert rec is not None
    assert rec.status == "INCONCLUSIVE", rec.reason
    assert not rec.reached_experiment, "a refused line may not establish a reach"


def test_aux_series_are_keyed_on_the_requested_seed(tmp_path):
    """`SH_AUX` was still keyed on the arm and seed the process ASSERTS, so one process
    could fill the whole aux table — and the report renders those numbers."""
    spec = ProbeSpec(paper_id="t", seeds=[0], arms=["reproduction"], provenance="driver",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     aux_metrics=["probe_acc"],
                     script=("import argparse\n"
                             "ap = argparse.ArgumentParser()\n"
                             "ap.add_argument('--seed', type=int, required=True)\n"
                             "ap.add_argument('--arm', default='reproduction')\n"
                             "a = ap.parse_args()\n"
                             "print('SH_DEVICE cpu')\n"
                             "print(f'SH_METRIC arm={a.arm} seed={a.seed} value=59.28')\n"
                             "for s in (0, 1, 2):\n"
                             "    print(f'SH_AUX key=probe_acc arm=reproduction seed={s} value=0.9')\n"))
    result = run_probe(_cfg(tmp_path), tmp_path, spec)
    series = (result.aux.get("probe_acc") or {}).get("reproduction")
    assert series is None or series.n <= 1, "one process is one aux sample"


def test_a_driver_verdict_does_not_contradict_its_own_wording():
    """The first version of fix 7 disclaimed the verdict it was accompanying: the sentence
    said "not established as a failure of the paper's code" while `overall_verdict` still
    returned RED. The provenance ceiling admits `driver` in both directions, so the verdict
    stands — and the sentence has to say what stands, plus what is not machine-checked."""
    spec = ProbeSpec(paper_id="t", provenance="driver", script="print(1)",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28", arms=["a"],
                     **_ESTABLISHED_IDENTITY)
    rec = reconcile(spec, [12.0, 12.1, 11.9], 0.01, [0, 1, 2])
    verdict, why = overall_verdict([], rec)
    assert verdict == "RED"
    assert "not machine-checked" in why, "the caveat a reader needs"
    assert "not established as a failure" not in why, "it must not contradict the verdict"


def test_the_refusal_reason_names_the_actual_cause(tmp_path):
    """One sentence used to cover three different situations, and was false for two of
    them: "exited before any sign that the experiment itself began" is simply untrue when
    the process printed contract lines and then hit a setup error, or when our own clock
    killed a run that had worked for half an hour."""
    ev = StartupEvidence(saw_contract_line=True, ran_seconds=1800, timed_out=True)
    spec = ProbeSpec(paper_id="t", provenance="driver", script="print(1)", arms=["a"],
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     **_ESTABLISHED_IDENTITY)
    rec = reconcile(spec, [], 0.1, [0], failure="timeout after 1800s", evidence=ev)
    assert "own wall-clock limit" in rec.reason
    assert "before any sign" not in rec.reason

    ev2 = StartupEvidence(saw_contract_line=True, setup_error="modulenotfounderror")
    rec2 = reconcile(spec, [], 0.1, [0], failure="exit 1", evidence=ev2)
    assert "setup reason" in rec2.reason and "modulenotfounderror" in rec2.reason
