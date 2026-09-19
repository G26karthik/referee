"""Part 2 of the rerun-safety task: `artifact_review_driver.load(cfg, pid, *, commit="")`
already did the correct read/verify mechanics (hash + writer + commit match) and had ZERO
callers anywhere in `harness/` or `tests/` except its own self-check — a complete,
already-tested guard sitting unused. `stages/artifact.py::run_route` called
`_reviewer_facts`, which dispatches the live, temperature-bearing model subprocess in
`artifact_review_driver.run`, UNCONDITIONALLY on every invocation. A rerun of
`run.py review` for an unrelated reason silently re-dispatched the model and overwrote
`artifact/<pid>.route.json`, discarding a previously-established `ArtifactFact` — this
already happened twice in production (a lost `PAPER_ARTIFACT_MISMATCH` on `apt-icml`, then
a lost fact on `acl`; see CLAUDE.md's Known limitations).

These tests exercise the fix at the same entry point `tests/test_artifact_route.py`
already uses (`artifact_stage.run_route`), spying on `artifact_review_driver.run` the same
way `test_gate_open_with_a_real_checkout_dispatches_the_reviewer` does, against REAL git
checkouts rather than mocks. Every existing test in `tests/test_artifact_route.py` is left
untouched; this file only adds new cache-specific coverage.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from harness import artifact_evidence, artifact_review_driver, state
from harness.artifacts import (ArtifactFact, ArtifactInspection, ClaimRef,
                               DiscoveredObject, PaperDoc, PlanDecision, Section,
                               TargetSet)
from harness.config import Config
from harness.stages import artifact as artifact_stage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_BROAD_CLAIM = "The released repository implements the described method."


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.yaml").write_text("batch_size: 32\nepochs: 100\n", encoding="utf-8")
    (root / "requirements.txt").write_text("torch==2.1.0\n", encoding="utf-8")
    (root / "README.md").write_text(
        "Table 2 of the paper is produced by `python train.py --config config.yaml`.\n",
        encoding="utf-8")
    try:
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@e")
        _git(root, "config", "user.name", "t")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "initial")
    except (OSError, subprocess.CalledProcessError):     # pragma: no cover
        pytest.skip("git is not available on this host")
    return root


def _doc(title: str = "A Paper", method_extra: str = "") -> PaperDoc:
    return PaperDoc(paper_id="p", title=title, n_pages=2, sections=[
        Section(section_idx=0, title="Method", page_start=1,
                text="We train every model with a batch size of 128 for 100 epochs. "
                     "The released code includes requirements.txt for the environment."
                     + method_extra),
    ])


def _target(tid: str, claim: str = _BROAD_CLAIM) -> DiscoveredObject:
    return DiscoveredObject(
        target_id=tid, question_kind="", claim_text=claim, centrality="CENTRAL",
        ref=ClaimRef(ref="P0:0-20", kind="prose_claim", resolution="resolved"),
        routes=["ARTIFACT_INSPECTION"], harness_addressable=True)


def _plan(tid: str) -> PlanDecision:
    return PlanDecision(target_id=tid, action="ARTIFACT_INSPECTION_ONLY",
                        route="ARTIFACT_INSPECTION", requires_execution=False)


def _target_set(objects: list[DiscoveredObject]) -> TargetSet:
    return TargetSet(paper_id="p", objects=objects,
                     plans=[_plan(o.target_id) for o in objects])


def _fake_run(calls: list[tuple]):
    """Stands in for `artifact_review_driver.run`'s subprocess mechanics — see
    `test_gate_open_with_a_real_checkout_dispatches_the_reviewer` in
    `tests/test_artifact_route.py` for the precedent — but, unlike that fixture's fake,
    this one also SEALS its result exactly as the real `run()` does (snapshot +
    `prompt_sha256` of the exact prompt text it was handed). Without that, nothing would
    ever land on disk for `artifact_review_driver.load` to find, and every one of these
    cache tests would silently pass for the wrong reason (the fake never persists, so the
    cache always misses and every call looks like a correct 'redispatch') rather than the
    right one (the cache is checked and legitimately hits or misses).
    """
    def run(cfg, pid, doc, root, prompt_text, *, url="", statements=None, facts=None):
        calls.append((pid, url, prompt_text))
        insp = ArtifactInspection(
            paper_id=pid, snapshot=artifact_evidence.snapshot(root, url),
            facts=[ArtifactFact(
                probe="code_review:SUSPICIOUS_IMPLEMENTATION", authority="ARTIFACT_FACT",
                statement="a code-only observation the auditor located and the harness "
                          "relocated")])
        artifact_review_driver.seal(cfg, pid, insp, {
            "written_by": "artifact_review_driver", "reader": "",
            "delegation_mode": "CLI_SUBPROCESS", "tool_policy": "unrecorded",
            "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        })
        return insp
    return run


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True)
    state.create_project(cfg, "", "T", pid="p")
    return cfg


# --------------------------------------------------------------------------- #
# (a) same commit, same paper-derived inputs: the second call reuses the cache
# --------------------------------------------------------------------------- #
def test_second_call_same_commit_reuses_cache_and_does_not_redispatch(
        checkout, tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])

    outcomes1, whole1 = artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1, "the first call must dispatch"

    outcomes2, whole2 = artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1, (
        "a second call with the SAME commit and the SAME paper-derived prompt inputs must "
        "reuse the sealed inspection rather than re-dispatching the live model call")

    # An equivalent result — the same reviewer fact still reaches the aggregate and the
    # per-target outcome the broad claim raises.
    assert whole1 is not None and whole2 is not None
    assert any(f.probe == "code_review:SUSPICIOUS_IMPLEMENTATION" for f in whole1.facts)
    assert any(f.probe == "code_review:SUSPICIOUS_IMPLEMENTATION" for f in whole2.facts)
    assert len(outcomes1) == len(outcomes2) == 1
    assert outcomes1[0].disposition == outcomes2[0].disposition


def test_cached_reuse_survives_a_second_run_routes_own_process(checkout, tmp_path,
                                                                monkeypatch):
    """The cache is read from DISK, not from an in-memory short-circuit inside one call —
    it must still hit after `artifact_review_driver`'s module-level state has been
    'reset' (simulated here by re-resolving the function through the real module, not the
    monkeypatched stage attribute), matching how a real second `run.py review` invocation
    is a fresh process with no shared memory."""
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])
    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1

    # A brand-new Config object (as a fresh CLI invocation would construct) pointed at the
    # SAME projects_dir must still see the sealed inspection on disk.
    cfg2 = Config(projects_dir=cfg.projects_dir, allow_artifact_review=True)
    artifact_stage.run_route(cfg2, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1, "a fresh Config over the same project dir must still hit the cache"


# --------------------------------------------------------------------------- #
# (b) the commit changes: the cache must NOT be reused
# --------------------------------------------------------------------------- #
def test_a_changed_commit_busts_the_cache_and_redispatches(checkout, tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])

    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1

    # A fresh commit on the SAME checkout path (as a re-clone/re-pin would produce).
    (checkout / "config.yaml").write_text("batch_size: 64\nepochs: 100\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "bump batch size")

    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 2, "a changed commit must not reuse the stale inspection"


# --------------------------------------------------------------------------- #
# (c) paper-derived PROMPT inputs change: the driver's own prompt hash must catch it,
# even at the identical commit — and inputs that are NOT part of the prompt (the
# target set's own statements) must NOT force a redispatch, because the auditor is never
# shown them (`prompts.artifact_review.build`'s signature has no such parameter).
# --------------------------------------------------------------------------- #
def test_a_changed_prompt_input_busts_the_cache_at_the_same_commit(checkout, tmp_path,
                                                                    monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])

    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    assert len(calls) == 1

    # Same commit, same target set — but the PAPER's own method text changed (a
    # re-extraction, or simply a different doc object for the same paper id). The prompt
    # the auditor would be shown is different, so the cache must not serve the old answer.
    changed_doc = _doc(method_extra=" We additionally ablate the learning rate schedule.")
    artifact_stage.run_route(cfg, "p", changed_doc, ts, checkout, url="u")
    assert len(calls) == 2, (
        "a changed paper-derived prompt input (method text) must bust the cache even at "
        "the identical commit")


def test_a_changed_title_busts_the_cache_at_the_same_commit(checkout, tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])

    artifact_stage.run_route(cfg, "p", _doc(title="A Paper"), ts, checkout, url="u")
    assert len(calls) == 1
    artifact_stage.run_route(cfg, "p", _doc(title="A Different Title"), ts, checkout, url="u")
    assert len(calls) == 2, "the title is part of the prompt (`AP.build`'s first argument)"


def test_a_changed_statements_list_does_not_force_a_redispatch(checkout, tmp_path,
                                                                monkeypatch):
    """`statements` is derived from the CURRENT target set's own claim texts and is never
    passed into `prompts.artifact_review.build` — the auditor reads the paper's method
    text and the checkout, not the harness's list of what it happens to be checking this
    run. A target set that grew or shrank between runs (a later grade, a re-discovery)
    must not force the live model call to re-read code that has not moved."""
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)

    ts1 = _target_set([_target("t1", claim="claim one")])
    outcomes1, whole1 = artifact_stage.run_route(cfg, "p", _doc(), ts1, checkout, url="u")
    assert len(calls) == 1

    ts2 = _target_set([_target("t1", claim="claim one"), _target("t2", claim="claim two")])
    outcomes2, whole2 = artifact_stage.run_route(cfg, "p", _doc(), ts2, checkout, url="u")
    assert len(calls) == 1, (
        "a different statements list alone must not redispatch the model — it is not "
        "part of the prompt the auditor is shown")

    # And the change IS still reflected downstream: `run_route` recomputes the aggregate
    # `inspect()` call fresh every time against the CURRENT statements, so the new claim
    # shows up in the aggregate even though the underlying model call was served from cache.
    assert len(outcomes1) == 1 and len(outcomes2) == 2
    assert whole2.statements_examined and "claim two" in whole2.statements_examined


# --------------------------------------------------------------------------- #
# route.json itself: unsealed, but stable given a stable target set once the underlying
# reviewer dispatch is cached — the decision documented in `run_route`'s own comment.
# --------------------------------------------------------------------------- #
def test_route_json_is_stable_across_a_cached_rerun_with_an_unchanged_target_set(
        checkout, tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", _fake_run(calls))
    cfg = _cfg(tmp_path)
    ts = _target_set([_target("t1")])

    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    route_path = state.project_dir(cfg, "p") / "artifact" / "p.route.json"
    first = route_path.read_text(encoding="utf-8")

    artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")
    second = route_path.read_text(encoding="utf-8")

    assert len(calls) == 1, "the rerun must have been served from cache"
    assert first == second, (
        "route.json is a deterministic function of doc/commit/target-set/reviewer-facts; "
        "with the reviewer dispatch cached and the target set unchanged it must be "
        "byte-identical, since ArtifactInspection/ArtifactFact carry no timestamp field")
