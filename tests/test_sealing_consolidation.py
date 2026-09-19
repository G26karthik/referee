"""Adversarial pinning tests for Task 6's seal/accept consolidation into `harness/sealing.py`.

Written and run GREEN against the pre-refactor code first (per the task-6 brief's TDD
discipline), then re-run green after `stages/audit.py`, `stages/probe.py`,
`stages/grade.py`, and the seven `*_driver.py` modules are refactored to route their
write and read sides through `harness.sealing.seal`/`verify_seal`.

Covers 4 of the 9 instances, chosen to span the variety the consolidation must preserve
without collapsing:

  - `stages/audit.py::accept_lens` / `lens_is_accepted` — the multi-writer case
    (`delegation.WRITTEN_BY` plus `COMPOSED_WRITER`) and the mode-carrying provenance
    fields (`tool_policy` forced to `unrecorded` for a mode that cannot prove one).
  - `stages/probe.py::accept_spec` / `spec_is_accepted` — the FORCED `"driver_accept"`
    token, regardless of the `mode` a caller supplies.
  - `stages/grade.py::accept_grade` / `grade_is_accepted` — a plain instance that renames
    `reviewer` to `grader` in the sidecar.
  - `reimplement_driver.py::accept_reimplementation` / `load_accepted` — the
    four-condition independent-verification read check, the most complex read side of
    the nine.
"""
from __future__ import annotations

import json

from harness import reimplement_driver, state
from harness.artifacts import ReimplementationIngredient, ReimplementationReadiness
from harness.config import Config
from harness.stages import audit as audit_stage
from harness.stages import grade as grade_stage
from harness.stages import probe as probe_stage


def _cfg(tmp_path):
    return Config(projects_dir=tmp_path / "projects")


# --------------------------------------------------------------------------- #
# 1. stages/audit.py::accept_lens — the multi-writer case
# --------------------------------------------------------------------------- #
def test_accept_lens_seals_with_the_requested_mode_and_is_tamper_evident(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "", "T", pid="p")
    raw = json.dumps({"lens": "overclaim", "schema_version": 2, "findings": []})

    rec = audit_stage.accept_lens(cfg, pid, "overclaim", raw,
                                  reviewer="alice", tool_policy="unrecorded",
                                  mode="SESSION_SUBAGENT")
    assert rec["written_by"] == "session_subagent"
    assert rec["lens"] == "overclaim" and rec["paper_id"] == pid and rec["findings"] == 0
    assert len(rec["content_sha256"]) == 64

    root = state.project_dir(cfg, pid)
    ok, why = audit_stage.lens_is_accepted(root, "overclaim")
    assert ok, why

    out = root / "audit" / "overclaim.json"
    out.write_text(out.read_text(encoding="utf-8").replace("[]", "[1]"), encoding="utf-8")
    ok, why = audit_stage.lens_is_accepted(root, "overclaim")
    assert ok is False and "changed after" in why


def test_accept_lens_manual_mode_cannot_claim_a_tool_policy(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "", "T", pid="p")
    raw = json.dumps({"lens": "protocol", "schema_version": 2, "findings": []})
    rec = audit_stage.accept_lens(cfg, pid, "protocol", raw, reviewer="bob",
                                  tool_policy="enforced: everything", mode="MANUAL")
    assert rec["written_by"] == "manual_accept"
    assert rec["tool_policy"] == "unrecorded", (
        "MANUAL cannot prove a tool policy and must not be allowed to claim one")


def test_lens_is_accepted_refuses_an_unrecognized_writer(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "", "T", pid="p")
    root = state.project_dir(cfg, pid)
    out = root / "audit" / "overclaim.json"
    state.write_json(out, {"lens": "overclaim", "schema_version": 2, "findings": []})
    state.write_json(out.with_suffix(".driver.json"), {
        "written_by": "not_a_real_writer",
        "content_sha256": __import__("hashlib").sha256(out.read_bytes()).hexdigest(),
    })
    ok, why = audit_stage.lens_is_accepted(root, "overclaim")
    assert ok is False and "not recognized" in why


# --------------------------------------------------------------------------- #
# 2. stages/probe.py::accept_spec — the forced token
# --------------------------------------------------------------------------- #
def test_accept_spec_always_writes_driver_accept_regardless_of_mode(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "T", pid="p")
    root = state.project_dir(cfg, pid)
    (root / "runs" / pid).mkdir(parents=True)
    script_path = root / "runs" / pid / "probe.py"
    script_path.write_text("print('SH_METRIC arm=baseline seed=0 value=1.0')",
                           encoding="utf-8")
    raw = json.dumps({"paper_id": pid, "script": str(script_path), "provenance": "driver"})

    for mode in ("MANUAL", "SESSION_SUBAGENT", "CLI_SUBPROCESS"):
        result = probe_stage.accept_spec(cfg, pid, raw, reviewer="alice", mode=mode)
        assert result["accepted"] is True
        sidecar = state.read_json(state.control_dir(root) / "spec.driver.json")
        assert sidecar["written_by"] == "driver_accept", (mode, sidecar["written_by"])
        ok, why = probe_stage.spec_is_accepted(root)
        assert ok, why


def test_spec_is_accepted_refuses_a_tampered_file(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "T", pid="p")
    root = state.project_dir(cfg, pid)
    (root / "runs" / pid).mkdir(parents=True)
    script_path = root / "runs" / pid / "probe.py"
    script_path.write_text("print('SH_METRIC arm=baseline seed=0 value=1.0')",
                           encoding="utf-8")
    raw = json.dumps({"paper_id": pid, "script": str(script_path), "provenance": "driver"})
    probe_stage.accept_spec(cfg, pid, raw, reviewer="alice", mode="MANUAL")
    spec_path = state.control_dir(root) / "spec.json"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace("driver", "repo_exec"),
        encoding="utf-8")
    ok, why = probe_stage.spec_is_accepted(root)
    assert ok is False and "changed after" in why


# --------------------------------------------------------------------------- #
# 3. stages/grade.py::accept_grade — a plain instance, reviewer renamed to grader
# --------------------------------------------------------------------------- #
def test_accept_grade_round_trips_and_renames_reviewer_to_grader(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "", "T", pid="p")
    raw = json.dumps({"verdict": "PLAUSIBLE", "severity": "MINOR", "confidence": "MEDIUM"})
    rec = grade_stage.accept_grade(cfg, pid, "c-01", raw, grader="carol",
                                   tool_policy="unrecorded", mode="SESSION_SUBAGENT")
    assert rec["grader"] == "carol"
    assert "reviewer" not in rec, "the grade sidecar's own field is 'grader', not 'reviewer'"
    assert rec["written_by"] == "session_subagent"
    assert rec["verdict"] == "PLAUSIBLE"

    gdir = state.project_dir(cfg, pid) / "audit" / "grade"
    ok, why = grade_stage.grade_is_accepted(gdir, "c-01")
    assert ok, why

    out = gdir / "c-01.json"
    out.write_text(out.read_text(encoding="utf-8").replace("PLAUSIBLE", "CONFIRMED"),
                   encoding="utf-8")
    ok, why = grade_stage.grade_is_accepted(gdir, "c-01")
    assert ok is False and "changed after" in why


def test_grade_is_accepted_refuses_a_missing_sidecar(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "", "T", pid="p")
    gdir = state.project_dir(cfg, pid) / "audit" / "grade"
    out = gdir / "c-02.json"
    state.write_json(out, {"verdict": "PLAUSIBLE"})
    ok, why = grade_stage.grade_is_accepted(gdir, "c-02")
    assert ok is False and "no provenance sidecar" in why


# --------------------------------------------------------------------------- #
# 4. reimplement_driver.py — the four-condition independent-verification read check
# --------------------------------------------------------------------------- #
def _readiness() -> ReimplementationReadiness:
    ings = [
        ReimplementationIngredient(kind="method", required=True, present=True,
                                   ref="s0", quote="we define the objective as a sum"),
        ReimplementationIngredient(kind="training", required=True, present=True,
                                   ref="s0", quote="Adam for 30 epochs at lr=1e-3"),
        ReimplementationIngredient(kind="dataset", required=True, present=True,
                                   ref="s0", quote="the CIFAR-100 dataset"),
        ReimplementationIngredient(kind="metric", required=True, present=True,
                                   ref="s0", quote="report accuracy"),
        ReimplementationIngredient(kind="comparison_target", required=True, present=True,
                                   ref="T0", quote="Table 1"),
    ]
    return ReimplementationReadiness(established=True, ingredients=ings)


def _good_report() -> str:
    return json.dumps({
        "script": "import argparse\n# method: sum objective\ndef train():\n    pass\n"
                  "print('SH_METRIC arm=a seed=0 value=0.9')",
        "bindings": [
            {"kind": "method", "impl_ref": "line 2", "impl_quote": "# method: sum objective"},
            {"kind": "training", "impl_ref": "line 3", "impl_quote": "def train():"},
            {"kind": "dataset", "impl_ref": "line 1", "impl_quote": "import argparse"},
            {"kind": "metric", "impl_ref": "line 5",
             "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
            {"kind": "comparison_target", "impl_ref": "line 5",
             "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
        ],
        "notes": "",
    })


def test_accept_reimplementation_refuses_to_call_a_self_verified_build_established(
        tmp_path):
    """`conformance()` itself computes `independent = bool(generator and verifier and
    generator != verifier)`, so a generator and verifier that are the same identity make
    `established` False even though every ingredient's snippet is present and bound —
    generated code cannot certify its own scientific conformance."""
    cfg = Config(projects_dir=tmp_path, allow_reimplementation_driver=True)
    readiness = _readiness()
    rec = reimplement_driver.accept_reimplementation(
        cfg, "p", "T1", _good_report(), readiness,
        reviewer="same-name", generated_by="same-name")
    assert rec["established"] is False
    # A nonconformant submission still loads — HONESTLY marked not established, never
    # silently dropped or silently promoted; the caller (`backends.authorize`) is the one
    # that must check `.established` before treating it as usable.
    got = reimplement_driver.load_accepted(cfg, "p", "T1")
    assert got is not None and got[1].established is False


def test_load_accepted_refuses_a_forged_established_seal_with_matching_identities(
        tmp_path):
    """The read side's four-condition check is a defense IN DEPTH over `conformance()`'s
    own establishment rule, not a duplicate of it: this constructs — by writing the
    sealed payload directly, bypassing `accept_reimplementation` — a `conformance` blob
    that claims `established=True` and `independently_verified=True` while
    `generated_by == verified_by`, which `conformance()` itself would never produce. Even
    so, `load_accepted` must still refuse it on the `generated_by != verified_by` leg of
    its own four-condition check, rather than trusting the persisted `established` flag."""
    from harness.artifacts import ReimplementationConformance

    cfg = Config(projects_dir=tmp_path, allow_reimplementation_driver=True)
    forged_conf = ReimplementationConformance(
        established=True, bindings=[], unbound=[], reason="forged",
        generated_by="same-name", verified_by="same-name",
        independently_verified=True)
    out_path, sidecar_path = reimplement_driver._paths(cfg, "p", "Tforged")
    state.write_json(out_path, {"script": "print(1)",
                                "conformance": forged_conf.model_dump()})
    state.write_json(sidecar_path, {
        "written_by": "manual_accept", "independent_verification": True,
        "content_sha256": __import__("hashlib").sha256(
            out_path.read_bytes()).hexdigest(),
    })
    assert reimplement_driver.load_accepted(cfg, "p", "Tforged") is None, (
        "matching generated_by/verified_by must be refused even when every other "
        "field in the forged payload claims establishment")


def test_load_accepted_accepts_a_genuinely_independent_seal(tmp_path):
    cfg = Config(projects_dir=tmp_path, allow_reimplementation_driver=True)
    readiness = _readiness()
    reimplement_driver.accept_reimplementation(
        cfg, "p", "T2", _good_report(), readiness,
        reviewer="verifier-x", generated_by="generator-y")
    got = reimplement_driver.load_accepted(cfg, "p", "T2")
    assert got is not None
    script, conf = got
    assert conf.established and conf.independently_verified
    assert conf.generated_by == "generator-y" and conf.verified_by == "verifier-x"


def test_load_accepted_refuses_a_tampered_reconstruction(tmp_path):
    cfg = Config(projects_dir=tmp_path, allow_reimplementation_driver=True)
    readiness = _readiness()
    reimplement_driver.accept_reimplementation(
        cfg, "p", "T3", _good_report(), readiness,
        reviewer="verifier-x", generated_by="generator-y")
    out_path, _sidecar = reimplement_driver._paths(cfg, "p", "T3")
    state.write_json(out_path, {"script": "tampered", "conformance": {}})
    assert reimplement_driver.load_accepted(cfg, "p", "T3") is None
