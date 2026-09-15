"""Delegation is environment-agnostic, and the modes are never pooled.

The defect: `audit_driver` hard-coded one mechanism, a fresh `claude -p` subprocess, and
everything not produced that way was sealed `manual_accept`. Two consequences, both bad.

  1. A controller with its own isolated-subagent mechanism could not use it. It paid an
     ACCOUNT-level session limit for work its own session could have done, and the harness
     had no way to express "delegate this" without also saying "by spawning a CLI".
  2. An isolated subagent the controller dispatched autonomously and a human pasting JSON
     into a file received the SAME provenance token. One is autonomous delegated execution
     with a fresh context per task; the other is a person, with no isolation claim at all.
     A corpus mixing them and reporting one number reports neither.

Every test here holds one of those two apart. The first group is about the interface being
agnostic; the second is about the provenance classes staying distinct; the third is about a
mixed corpus being unable to describe itself as homogeneous.
"""
from __future__ import annotations

import inspect
import json

import pytest

from harness import delegation
from harness.config import Config
from harness.stages import audit as audit_stage
from harness.stages import grade as grade_stage

MODES = ("CLI_SUBPROCESS", "SESSION_SUBAGENT", "MANUAL")


def test_the_harness_cannot_detect_whether_the_controller_can_delegate():
    """`session_can_delegate` is a PARAMETER, not a probe.

    A Python process cannot discover whether the thing that launched it is able to
    dispatch a subagent. Any attempt to sniff it — an environment variable, a parent
    process name, a marker file — would be a guess that fails differently in every
    controller, and the failure mode is the harness quietly deciding it must use the CLI.
    So the environment answers and the harness records the answer.
    """
    params = inspect.signature(delegation.modes_available).parameters
    assert "session_can_delegate" in params
    assert params["session_can_delegate"].default is False, (
        "the default must claim the LESS capable environment, so a controller that says "
        "nothing does not get a mechanism it may not have")
    # Checked over the module's IMPORT GRAPH, not over its text.
    #
    # Two textual versions of this check failed on the module's own honesty: the word
    # "environment" appears in its docstrings AND in the `ISOLATION_CLAIM` values, which
    # are the reader-facing sentences explaining what each mode cannot prove. Grepping
    # for `environ` flagged "the delegate's environment" as a call to `os.environ`.
    #
    # The import graph is the real property anyway: a module that imports neither `os`
    # nor `subprocess` nor `sys` cannot read an environment variable, inspect a parent
    # process, or spawn a delegate, whatever its prose says.
    import ast
    tree = ast.parse(inspect.getsource(delegation))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("os", "sys", "subprocess", "psutil", "platform", "socket"):
        assert forbidden not in imported, (
            f"delegation imports {forbidden}, so it could sniff its environment or spawn "
            f"a delegate; deciding a mechanism and executing one are different jobs")
    # `shutil` for `which`, and `inspect` inside the self-check only. Nothing else: this
    # module decides a mechanism, and executing one is a different job in a different file.
    assert imported <= {"shutil", "inspect", "__future__"}, imported


def test_no_mode_is_hard_coded_as_the_only_one():
    """The whole point. Every mechanism is a value in a vocabulary, and the vocabulary is
    closed but not singular."""
    assert len(delegation.DELEGATION_MODES) >= 4
    assert set(delegation.WRITTEN_BY) == {"CLI_SUBPROCESS", "SESSION_SUBAGENT", "MANUAL"}
    # and an environment with no CLI still has an autonomous mode
    without_cli = delegation.modes_available(cli_gate_open=False,
                                             session_can_delegate=True)
    assert "SESSION_SUBAGENT" in without_cli
    assert "CLI_SUBPROCESS" not in without_cli
    assert delegation.choose(without_cli) == "SESSION_SUBAGENT", (
        "an environment with no CLI must still be able to delegate autonomously")


def test_a_shut_gate_removes_the_cli_even_when_the_binary_exists():
    """Finding `claude` on PATH is not permission to spend an account session on it."""
    assert "CLI_SUBPROCESS" not in delegation.modes_available(
        cli_command="claude", cli_gate_open=False, session_can_delegate=True)


def test_the_operators_preference_decides_whose_budget_is_spent():
    """Which mechanism to spend is not the harness's decision. One controller pays for a
    CLI session, another pays for its own subagents, and the harness has no standing to
    prefer one budget over the other."""
    both = delegation.modes_available(cli_command="claude", cli_gate_open=True,
                                      session_can_delegate=True)
    assert delegation.choose(both, prefer="SESSION_SUBAGENT") == "SESSION_SUBAGENT"
    assert delegation.choose(both, prefer="CLI_SUBPROCESS") == "CLI_SUBPROCESS"
    # but a preference for something unavailable is not honoured by inventing it
    assert delegation.choose(("MANUAL",), prefer="CLI_SUBPROCESS") == "MANUAL"
    assert delegation.choose(("SESSION_SUBAGENT", "MANUAL"),
                             prefer="CLI_SUBPROCESS") == "SESSION_SUBAGENT"


def test_a_work_order_states_what_the_delegate_must_not_read():
    """For SESSION_SUBAGENT and MANUAL the stated contract is the ONLY isolation there is,
    and an unstated contract is not a contract."""
    wo = delegation.work_order(task="lens:confound", prompt_path="p.md",
                               output_path="o.json", mode="SESSION_SUBAGENT")
    joined = " ".join(wo["must_not_read"]).lower()
    for forbidden in ("other task", "other paper", "source", "earlier review"):
        assert forbidden in joined, forbidden
    assert wo["read"] == "p.md" and wo["write"] == "o.json"


# --------------------------------------------------------------------------- #
# the three provenance classes are distinct, and only one may claim enforcement
# --------------------------------------------------------------------------- #
def test_an_autonomous_subagent_is_not_sealed_as_manual():
    """The exact conflation this module exists to end."""
    sub = delegation.provenance_record(mode="SESSION_SUBAGENT")
    man = delegation.provenance_record(mode="MANUAL")
    assert sub["written_by"] != man["written_by"]
    assert sub["written_by"] == "session_subagent"
    assert man["written_by"] == "manual_accept"
    assert sub["delegation_mode"] != man["delegation_mode"]
    assert sub["isolation_claim"] != man["isolation_claim"]
    # and the subagent's claim is honest about the half it cannot prove
    assert "isolation is real" in sub["isolation_claim"]
    assert "NOT provable" in sub["isolation_claim"]
    assert "none that this harness can establish" in man["isolation_claim"]


@pytest.mark.parametrize("mode", ["SESSION_SUBAGENT", "MANUAL"])
def test_only_the_cli_may_report_an_enforced_tool_policy(mode):
    """A caller may pass a policy it wishes were true. Only the mode that built the
    delegate's command line may report one, and the refusal happens HERE rather than by
    asking every caller to remember."""
    rec = delegation.provenance_record(
        mode=mode, tool_policy="enforced: tools=Read denied=12 restricted strict_mcp")
    assert rec["tool_policy"] == "unrecorded"
    assert rec["tool_policy_provable"] is False


def test_the_cli_mode_keeps_the_policy_it_can_prove():
    rec = delegation.provenance_record(mode="CLI_SUBPROCESS",
                                       tool_policy="enforced: tools=Read denied=12")
    assert rec["tool_policy"] == "enforced: tools=Read denied=12"
    assert rec["tool_policy_provable"] is True


def test_an_unrecognised_mode_falls_to_the_one_that_claims_least():
    """Fail-closed. A future mode nobody taught this module about must not inherit the
    CLI's guarantees by being unrecognised."""
    for junk in ("SOMETHING_NEW", "", "cli", "AUTONOMOUS", "  "):
        rec = delegation.provenance_record(mode=junk, tool_policy="enforced: everything")
        assert rec["delegation_mode"] == "MANUAL", junk
        assert rec["tool_policy"] == "unrecorded", junk
        assert rec["written_by"] == "manual_accept", junk


def test_a_lens_sealed_by_a_subagent_records_that_and_not_manual(tmp_path):
    """Through the real `accept_lens`, against the real validation gate."""
    cfg = Config(projects_dir=tmp_path / "projects")
    pid = "p"
    (cfg.projects_dir / pid / "audit").mkdir(parents=True)
    raw = json.dumps({"lens": "confound", "findings": [], "notes": "n",
                      "unasked_question": ""})

    rec = audit_stage.accept_lens(cfg, pid, "confound", raw,
                                  reviewer="an isolated subagent of this session",
                                  tool_policy="enforced: tools=Read",
                                  mode="SESSION_SUBAGENT")
    assert rec["written_by"] == "session_subagent"
    assert rec["delegation_mode"] == "SESSION_SUBAGENT"
    assert rec["tool_policy"] == "unrecorded", (
        "a subagent cannot prove a sandbox, so it may not report one even when asked to")
    # and the harness's own completeness gate accepts it — a distinct mode must not be
    # sealed by one function and refused by the other
    ok, why = audit_stage.lens_is_accepted(cfg.projects_dir / pid, "confound")
    assert ok, why


def test_the_default_mode_is_the_one_that_promises_least(tmp_path):
    """A caller who does not say gets MANUAL, not an autonomous mode's guarantees."""
    cfg = Config(projects_dir=tmp_path / "projects")
    (cfg.projects_dir / "p" / "audit").mkdir(parents=True)
    raw = json.dumps({"lens": "protocol", "findings": [], "notes": "n",
                      "unasked_question": ""})
    rec = audit_stage.accept_lens(cfg, "p", "protocol", raw)
    assert rec["delegation_mode"] == "MANUAL"
    assert rec["written_by"] == "manual_accept"
    assert inspect.signature(audit_stage.accept_lens).parameters["mode"].default == "MANUAL"


def test_every_accept_channel_carries_a_mode():
    """Lenses, grades and the whole-paper read. A channel without a mode would be a hole
    where an autonomous artifact silently becomes a manual one, or the reverse."""
    from harness import verdict_driver
    for fn in (audit_stage.accept_lens, grade_stage.accept_grade,
               verdict_driver.accept_verdict):
        params = inspect.signature(fn).parameters
        assert "mode" in params, fn.__qualname__
        assert params["mode"].default == "MANUAL", fn.__qualname__


def test_no_validated_writer_token_is_shared_between_two_modes():
    """If two modes shared a token, an artifact could not say which produced it — which
    is the state this whole module replaces."""
    tokens = list(delegation.WRITTEN_BY.values())
    assert len(tokens) == len(set(tokens))
    # and every token a seal can produce is one a completeness gate admits
    assert set(tokens) <= set(audit_stage._ACCEPTED_WRITERS)
    assert set(tokens) <= set(grade_stage._ACCEPTED_GRADE_WRITERS)
    from harness import verdict_driver
    assert set(tokens) <= set(verdict_driver.WRITERS)


# --------------------------------------------------------------------------- #
# a mixed corpus cannot describe itself as one run
# --------------------------------------------------------------------------- #
def test_two_modes_in_one_corpus_is_never_reported_as_homogeneous():
    mixed = delegation.summarise([
        {"written_by": "audit_driver"},
        {"delegation_mode": "SESSION_SUBAGENT"},
        {"delegation_mode": "SESSION_SUBAGENT"},
    ])
    assert mixed["homogeneous"] is False
    assert mixed["by_mode"] == {"SESSION_SUBAGENT": 2, "CLI_SUBPROCESS": 1}
    assert mixed["tool_policy_provable_for"] == 1, (
        "only the CLI artifact's isolation is provable, and the count says so")


def test_one_mode_is_homogeneous_and_an_empty_set_is_not_a_claim():
    assert delegation.summarise([{"written_by": "audit_driver"}] * 4)["homogeneous"]
    empty = delegation.summarise([])
    assert empty["total"] == 0 and empty["by_mode"] == {}


def test_a_legacy_sidecar_still_reports_a_mode():
    """The September corpus's sidecars predate `delegation_mode`. They must report the
    mode they were, not a blank — and `manual_accept` was genuinely what they were."""
    assert delegation.mode_of({"written_by": "manual_accept"}) == "MANUAL"
    assert delegation.mode_of({"written_by": "audit_driver"}) == "CLI_SUBPROCESS"
    assert delegation.mode_of({}) == "MANUAL"
    assert delegation.mode_of(None) == "MANUAL"


def test_a_sidecars_declared_mode_wins_over_its_legacy_token():
    """Both fields are written together by `provenance_record`, so they cannot disagree
    in an artifact this harness produced. If they ever do, the explicit field is the one
    that was decided deliberately."""
    assert delegation.mode_of({"delegation_mode": "SESSION_SUBAGENT",
                               "written_by": "manual_accept"}) == "SESSION_SUBAGENT"


def test_the_delegation_layer_decides_nothing_about_a_paper():
    """It chooses a mechanism and records provenance. It must reach no severity, no
    verdict and no colour."""
    from harness import grading, priority, taxonomy
    from harness.stages import report as report_stage
    for fn in (grading.derive, taxonomy.classify, priority.score,
               report_stage.overall_verdict, report_stage.claim_status,
               report_stage.triage):
        params = set(inspect.signature(fn).parameters)
        for banned in ("mode", "delegation", "written_by", "tool_policy",
                       "isolation"):
            assert not any(banned in p for p in params), (fn.__qualname__, banned)
    for mod in (grading, taxonomy, priority):
        assert "delegation" not in inspect.getsource(mod), mod.__name__
