"""The trust kernel, and nothing else: each check fails if a guarantee breaks.
Run: python tests/test_kernel.py   (or pytest)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import discover, execute, independence, report, state, tasks  # noqa: E402
from harness.evidence import Paper, evaluate, interval, value_in  # noqa: E402
from harness.reconcile import READINGS_DIFFER, arithmetic, certificate, reconcile  # noqa: E402

PAGES = ["Our method reaches 61.4 accuracy on CIFAR.\nThe baseline reaches 59.3 accuracy on CIFAR.\n"
         "We report the mean over 5 random seeds.\n"
         "We use gener-\nation of samples. The ﬁnal loss is −0.52.",
         "Table 2: Results\nMethod Acc F1\nOurs 61.4 0.72\nBase 59.3 0.70"]
ROWS = [[], ["Table 2: Results", "Method Acc F1", "Ours 61.4 0.72", "Base 59.3 0.70"]]
# A script author's fidelity table (tasks._fidelity) for fixtures that are not about it.
FID = [{"aspect": a, "not_applicable": "not part of this synthetic fixture"} for a in tasks.FIDELITY]


def test_quotes_resolve_uniquely_or_not_at_all():
    p = Paper(PAGES, ROWS)
    hit, _ = p.find("reaches 61.4 accuracy")
    assert hit and hit["page"] == 1 and hit["quote"] == "reaches 61.4 accuracy"
    assert p.find("accuracy on CIFAR")[0] is None                   # twice: ambiguous
    assert p.occurs("accuracy on CIFAR") and not p.occurs("reaches 99.9 accuracy")   # prose citing the paper
    assert report.unquoted('It says "a short one" (p1) and then "reaches 61.4 accuracy" and "accuracy on CIFAR".', p) == []
    assert report.unquoted('The paper says "reaches 99.9 accuracy here".', p) == ["reaches 99.9 accuracy here"]
    assert p.find("reaches 99.9 accuracy")[0] is None               # absent
    assert p.find("the")[0] is None                                 # too short to address anything
    assert p.find("We use generation of samples")[0]                # line-break hyphen, after exact fails
    assert p.find("We use gener- ation of samples")[0]              # ...also when the quote copied it
    assert p.find("The final loss is -0.52")[0]                     # ligature and minus folded


def test_printed_numbers_are_standalone_tokens():
    assert value_in("Ours 61.4 0.72", "61.4") and value_in("1,234 runs", "1234")
    assert not value_in("x2 = 5", "2") and not value_in("v0.12.3", "0.12") and not value_in("610.4", "61.4")


def test_a_cell_is_one_printed_row_with_its_header_on_the_page():
    p = Paper(PAGES, ROWS)
    assert p.cell("Ours", "61.4", "Acc") == ({"page": 2, "row": "Ours 61.4 0.72"}, "")
    assert p.cell("Ours", "99.0", "Acc")[0] is None                 # value not in the row
    assert p.cell("Ours", "61.4", "BLEU")[0] is None                # header not on that page
    assert p.cell("Ours", "61.4", "")[0] is None                    # a cell needs its column
    assert Paper(["It cannot significantly change."]).find("not significant")[0] is None   # mid-word


def test_arithmetic_is_exact_and_never_eval():
    from fractions import Fraction
    assert evaluate("(a - b) / b * 100", {"a": Fraction("61.4"), "b": Fraction("59.3")}) == Fraction(2100, 593)
    for bad in ("__import__('os')", "a.real", "open('x')", "[1]"):
        try:
            evaluate(bad, {"a": Fraction(1)})
        except ValueError:
            continue
        raise AssertionError(f"evaluated {bad!r}")
    lo, hi = interval("(a - b) / b * 100", {"a": "61.4", "b": "59.3"})
    assert arithmetic(lo, hi, "3.6")["status"] == "ARITHMETIC_CONSISTENT"   # rounding of the inputs is credited
    assert arithmetic(lo, hi, "4.5")["status"] == "ARITHMETIC_CONTRADICTION"
    for bad in ("3.9", "1 + 2"):                                      # no printed operand used / a free decimal
        try:
            interval(bad, {"a": "61.4"})
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_reconcile_never_convicts_on_environment_or_refusal():
    ev_infra = {"infra_error": "cuda out of memory", "reached": True}
    assert reconcile("AUTHOR_CODE", "61.4", [], "boom", {}, False, False, "gate shut")["status"] == "BLOCKED"
    assert reconcile("AUTHOR_CODE", "61.4", [], "oom", ev_infra, True, True, "")["status"] == "INCONCLUSIVE"
    assert reconcile("AUTHOR_CODE", "61.4", [], "x", {"setup_error": "importerror"}, True, True, "")["status"] == "INCONCLUSIVE"
    own = {"reached": True, "own_code_crash": "/work/repo/train.py"}
    assert reconcile("AUTHOR_CODE", "61.4", [], "crash", own, True, True, "")["status"] == "FAILED_REPRODUCTION"
    assert reconcile("AUTHOR_CODE", "61.4", [], "crash", {"reached": True}, True, True, "")["status"] == "INCONCLUSIVE"
    assert reconcile("AUTHOR_CODE", "61.4", [], "", {}, True, True, "")["status"] == "INCONCLUSIVE"   # no metric
    tb = 'Traceback (most recent call last):\n  File "/work/repo/t.py", line 3\n  File "{}", line 9\nRuntimeError: boom'
    run = {"returncode": 1, "seconds": 60, "stdout": "x\n" * 9}
    lib = execute.classify({**run, "stderr": tb.format("/env/lib/site-packages/torch/x.py")})
    assert lib["reached"] and not lib["own_code_crash"]                 # died inside a library
    assert execute.classify({**run, "stderr": tb.format("/work/repo/model.py")})["own_code_crash"]
    assert reconcile("RECONSTRUCTION", "61.4", [], "crash", {"reached": True}, True, True, "")["status"] == "INCONCLUSIVE"
    assert reconcile("CERTIFICATE", "", [], "AssertionError", {}, False, True, "")["status"] == "INCONCLUSIVE"
    assert reconcile("CERTIFICATE", "", [0, 1, 0], "", {}, False, True, "")["status"] == "INCONCLUSIVE"  # premises unsaid
    # 2026-10-01 (invariant 16): instances whose premises the script never evaluated are not admissible
    assert reconcile("CERTIFICATE", "", [0, 0], "", {}, False, True, "")["status"] == "INCONCLUSIVE"
    assert reconcile("CERTIFICATE", "", [0, 0], "", {}, False, True, "", cert=[{"violated": 0, "premises": 1}] * 2)[
        "status"] == "NO_VIOLATION_FOUND"
    assert reconcile("TRY", "61.4", [61.4], "", {}, False, True, "")["status"] == "INCONCLUSIVE"   # not admissible


def test_reconcile_arithmetic_rules():
    ok = lambda printed, values, seeded: reconcile("AUTHOR_CODE", printed, values, "", {}, seeded, True, "")["status"]
    assert ok("61.4", [0.614], False) == "INCONCLUSIVE"            # 100x: units, not a failure
    assert ok("61.4", [61.43], False) == "RESOLVED_VERIFIED"       # within printed precision
    assert ok("61.4", [62.0], False) == "INCONCLUSIVE"             # deterministic: never FAILED
    assert ok("61.4", [61.0, 61.8, 61.5], True) == "RESOLVED_VERIFIED"
    assert ok("61.4", [70.0, 70.1, 69.9], True) == "FAILED_REPRODUCTION"
    assert ok("61.4", [38.6, 38.5, 38.7], True) == "INCONCLUSIVE"   # the complement: error vs accuracy


def test_one_gate_refuses_what_it_must():
    cfg = state.Config()
    cfg.allow_repo_exec = cfg.allow_script_exec = False
    assert not execute.authorize(cfg, {"kind": "AUTHOR_CODE"})[0]
    assert not execute.authorize(cfg, {"kind": "ARITHMETIC"})[0]
    assert not execute.authorize(cfg, {"kind": "try"})[0]
    cfg.allow_repo_exec = cfg.allow_script_exec = True
    if execute.docker_status()[0]:
        assert not execute.authorize(cfg, {"kind": "AUTHOR_CODE", "repo_attributed": True, "identity": {}}, (True, ""))[0]
        assert not execute.authorize(cfg, {"kind": "AUTHOR_CODE", "repo_attributed": True,
                                           "identity": {"established": True}}, (False, "dirty"))[0]
        assert not execute.authorize(cfg, {"kind": "CERTIFICATE", "script_sha256": "a",
                                           "approval": {"approved": True, "script_sha256": "b"}})[0]
        assert execute.authorize(cfg, {"kind": "CERTIFICATE", "script_sha256": "a",
                                       "approval": {"approved": True, "script_sha256": "a"}})[0]


def test_metric_is_bound_by_name_not_position():
    assert execute.parse_metric('{"acc": 61.4}\n{"loss": 2}', "acc") == (61.4, "'acc' read from json output")
    assert execute.parse_metric("acc: 61.0\nacc: 62.0", "acc")[0] is None       # two values: refused
    assert execute.parse_metric("accuracy 61", "acc")[0] is None
    assert execute.seeded_command("python t.py --seed 7 --n 3", "--seed", 2) == ("python t.py --seed 2 --n 3", True)
    assert execute.seeded_command("python t.py --n 3", "--seed", 2) == ("python t.py --n 3", False)


def _project(td: Path) -> tuple[state.Config, str]:
    cfg = state.Config()
    cfg.projects = td
    pid = "p"
    state.write_json(td / pid / "paper" / "doc.json", {"pid": pid, "title": "T", "sha256": "0", "pages": PAGES,
                                                       "rows": ROWS, "arxiv_id": "", "arxiv_version": "", "source": ""})
    (td / pid / "paper" / "paper.md").write_text("\n".join(PAGES), encoding="utf-8")
    state.write_json(td / pid / "source.json", {})
    return cfg, pid


def _seal(cfg, pid, tid, obj, td) -> dict:
    if tid == "plan" and tasks._sealed(td / pid, "claims") is None and "claims" in {
            t["id"] for t in tasks._plan(tasks._Ctx(cfg, pid))[1]}:
        tasks.abandon(cfg, pid, "claims", "a test of the planner's own claim list (no extraction)")
    f = td / "answer.json"
    f.write_text(json.dumps(obj), encoding="utf-8")
    return tasks.seal(cfg, pid, tid, str(f))


def _audited(root: Path, cid: str, verdict: str = "STANDS", depends_on: list | None = None) -> None:
    """A sealed independent audit of a check's failure, written as the seal would store it (2026-10-02: a failure about
    the printed claim counts only once audited; tests that are not about the audit start from one)."""
    rec = {"verdict": verdict, "depends_on": depends_on or [], "quotes": ["x"], "notes": ""}
    state.write_json(root / "sealed" / f"audit__{cid}.json", rec)
    seals = state.read_json(root / "seals.json", {}) or {}
    seals[f"audit:{cid}"] = state.sha256((root / "sealed" / f"audit__{cid}.json").read_bytes())
    state.write_json(root / "seals.json", seals)


def test_seals_keep_only_harness_derived_fields_and_detect_tampering():
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        concern = {"title": "gap", "severity": "MAJOR", "confidence": "LOW", "class": "CONFIRMED_FINDING",
                   "status": "VERIFIED", "id": "forged", "evidence": [{"quote": "reaches 61.4 accuracy"}]}
        bad = {**concern, "evidence": [{"quote": "a sentence the paper never printed"}]}
        try:
            _seal(cfg, pid, "lens:overclaim", {"concerns": [concern, bad]}, td)
            raise AssertionError("an unresolved quote was sealed on the first attempt")
        except tasks.SealError:
            pass
        _seal(cfg, pid, "lens:overclaim", {"concerns": [concern]}, td)
        rec = tasks._sealed(td / pid, "lens:overclaim")
        c = rec["concerns"][0]
        assert c["id"] == "overclaim-01" and "status" not in c and c["severity"] == "MINOR"   # LOW caps MAJOR
        for lens in tasks.LENSES[1:]:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": [{"id": "overclaim-01", "severity": "FATAL"}]}, td)
        x = tasks._Ctx(cfg, pid)
        assert x.concerns()[0]["severity"] == "MINOR"                  # a critic never raises severity
        p = td / pid / "sealed" / "lens__overclaim.json"
        p.write_text(p.read_text(encoding="utf-8").replace("MINOR", "FATAL"), encoding="utf-8")
        assert tasks._sealed(td / pid, "lens:overclaim") is None       # tampered: not a seal
        try:
            _seal(cfg, pid, "report", {"summary_md": "x"}, td)
            raise AssertionError("sealed a task that was not pending")
        except tasks.SealError:
            pass


def test_identity_needs_two_agreeing_keys_verbatim_in_the_checkout():
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        co = td / pid / "repo"
        co.mkdir(parents=True)
        (co / "README.md").write_text("Run:\n    python train.py --data cifar --epochs 2000 --seed 0\n", encoding="utf-8")
        (co / "train.py").write_text("print({'test_acc': acc})\n", encoding="utf-8")
        (td / pid / "out.md").write_text("python train.py --data cifar --epochs 200 --seed 0\n", encoding="utf-8")
        git = lambda *a: subprocess.run(["git", *a], cwd=co, capture_output=True, text=True)
        git("init", "-q"), git("add", "."), git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c")
        cmd = "python train.py --data cifar --epochs 2000 --seed 0"
        plan = {"checks": [{"id": "C1", "kind": "AUTHOR_CODE", "command_quote": cmd, "metric": "test_acc",
                            "prepare_quote": ""}]}
        x = type("X", (), {"checkout": co, "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, t: plan, "plan": lambda self: plan,
                           "tracked": lambda self: {"README.md", "train.py"}})()
        good = {"command_quote": cmd, "command_file": "README.md", "metric_key": "test_acc", "metric_file": "train.py",
                "seed_flag": "--seed", "runs": 5, "runs_quote": "mean over 5 random seeds"}
        rec = tasks._seal_bind(x, "bind:C1", good, final=True)
        assert rec["identity"]["established"] and rec["runs"] == 5 and rec["seed_flag"] == "--seed"
        for bad in ({"metric_key": "val_acc"}, {"command_quote": ""}, {"metric_file": "README.md"},
                    {"command_quote": "python train.py --data cifar --epochs 200 --seed 0"},     # a downscaled prefix
                    {"command_file": "../out.md"}, {"runs": 500, "runs_quote": ""}):
            assert not tasks._seal_bind(x, "bind:C1", {**good, **bad}, final=True)["identity"]["established"] \
                or bad.get("runs"), bad
        assert tasks._seal_bind(x, "bind:C1", {**good, "runs": 500}, final=True)["runs"] == 3   # unquoted count ignored
        assert tasks._seal_bind(x, "bind:C1", {**good, "seed_flag": "--data"}, final=True)["seed_flag"] == ""


def test_report_status_words_must_be_earned():
    led = {"checks": [{"id": "C1", "status": "INCONCLUSIVE"}, {"id": "C2", "status": "RESOLVED_VERIFIED"}]}
    assert report.unearned("C1 reproduced the table.", led)
    assert report.unearned("The main result is verified.", led)
    assert not report.unearned("C2 reproduced the printed value.", led)
    assert not report.unearned("C1 was not verified: the run was blocked.", led)
    assert report.unearned("The gain is veri**fied** here.", led) and report.unearned("This confirms it.", led)
    assert not report.unearned("C1 is BLOCKED; RESOLVED_VERIFIED would need a run. The verifier refused.", led)
    led["concerns"] = [{"id": "contradiction-02"}]
    assert not report.unearned("- contradiction-02 (MAJOR, model judgment).", led)   # an id is a name
    assert report.unearned("contradiction-02: the table contradicts the abstract.", led)
    assert not report.unearned("Was the schedule validated? How is the validation set carved out?", led)
    assert report.unearned("The schedule was validated on the validation set.", led)
    assert not report.unearned("protocol-04 (replicate count and SEM basis not stated).", led)   # protocol nouns
    assert not report.unearned("Each stage ran 3 seeded replicates.", led)
    assert report.unearned("The ordering was replicated.", led) and report.unearned("This replicates Fig. 2.", led)


def _refused(fn) -> str:
    try:
        fn()
    except (tasks.SealError, ValueError) as e:
        return str(e)
    raise AssertionError("accepted")


def test_plan_spends_the_budget_on_central_claims_first():
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        x = tasks._Ctx(cfg, pid)
        arith = {"id": "A", "kind": "ARITHMETIC", "claim_quote": "The baseline reaches 59.3", "role": "target", "criterion": "stated",
                 "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"},
                 "operands": [{"name": "a", "quote": "reaches 61.4 accuracy", "value": "61.4"}], "expression": "a"}
        cert = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
                "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        central = [{"quote": "We report the mean over 5 random seeds", "checks": ["B"], "claim_type": "theory",
                    "scope": ["the loss"]}]
        cfg.max_checks = 1        # the incidental check came first; the central one still gets the slot
        rec = tasks._seal_plan(x, "plan", {"checks": [arith, cert], "central_claims": central}, final=False)
        assert [c["kind"] for c in rec["checks"]] == ["CERTIFICATE"] and rec["checks"][0]["central"]
        assert rec["central_claims"][0]["checks"] == ["C1"] and rec["dropped"][0]["check"] == "A"
        cfg.max_checks = 3
        assert "incidental_why" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [arith]}, final=False))
        rec = tasks._seal_plan(x, "plan", {"checks": [{**arith, "incidental_why": "no central claim computes"}]}, final=False)
        assert rec["checks"][0]["central"] is False
        code = {"id": "B", "kind": "AUTHOR_CODE", "claim_quote": "We report the mean over 5 random seeds",
                "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}, "role": "target", "criterion": "stated",
                "covers": ["the loss"]}
        assert "`metric`" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [code], "central_claims": central},
                                                              final=False))   # AUTHOR_CODE: the planner's key
        rel = {"id": "B", "kind": "RECONSTRUCTION", "claim_quote": "We report the mean over 5 random seeds",
               "target": {"quote": "Our method reaches 61.4", "relation": "acc_ours > acc_base"}, "role": "target",
               "criterion": "stated", "test": "performance", "covers": ["the loss"]}
        rec = tasks._seal_plan(x, "plan", {"checks": [rel], "central_claims": central}, final=False)
        assert rec["checks"][0]["target"]["names"] == ["acc_base", "acc_ours"] and rec["checks"][0]["printed"] == ""
        for bad in ("acc_ours", "acc_ours > 0.5 * acc_base", "a < b < c", "acc_ours == acc_base"):
            assert "relation" in _refused(lambda: tasks._seal_plan(
                x, "plan", {"checks": [{**rel, "target": {**rel["target"], "relation": bad}}], "central_claims": central},
                final=False)), bad
        assert "relation" in _refused(lambda: tasks._seal_plan(
            x, "plan", {"checks": [{**rel, "kind": "AUTHOR_CODE", "metric": "acc"}], "central_claims": central}, final=False))


def test_the_compared_output_is_bound_by_name_by_the_script_author():
    """Sep-29 changepoint C3: the planner named no metric, the generator was told `violated`,
    and a result line printing the target's value was read as 'no metric was reported'."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg = state.Config()
        plan = {"checks": [{"id": "C1", "kind": "RELEASED_DATA", "metric": "",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "17"}}]}
        state.write_json(td / "released.json", [{"path": "results/a.csv"}])
        x = type("X", (), {"root": td, "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        script = 'rows = open("results/a.csv").read().split()\nn = len(rows)\nprint("REFEREE_RESULT", n)\n'
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("dataset", 'open("results/a.csv")'), ("metric", "n = len(rows)"), ("comparison_target", "print("))]
        g = {"fidelity": FID, "script": script, "runs": 1, "outputs": ["n_no_cp", "violated"], "bindings": binds}
        for metric in ("", "violated", "n_other"):
            assert "`metric`" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "metric": metric}, final=False))
        bad_dev = {"printed": "a sentence the paper never printed", "used": "x", "changes_claim": False}
        assert "deviation" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "metric": "n_no_cp",
                                                                               "deviations": [bad_dev]}, final=False))
        dev = {"printed": "We report the mean over 5 random seeds", "used": "one pass over the file", "why": "no seeds",
               "changes_claim": False}
        rec = tasks._seal_gen(x, "gen:C1.1", {**g, "metric": "n_no_cp", "deviations": [dev]}, final=False)
        assert rec["metric"] == "n_no_cp" and rec["deviations"][0]["page"] == 1
        out = 'REFEREE_RESULT {"violated": 0, "n_no_cp": 17, "n_all": 83}'
        assert execute.result_values(out, rec["metric"]) == [17.0] and execute.result_values(out, "") == []
        assert reconcile("RELEASED_DATA", "17", [17.0], "", {}, False, True, "")["status"] == "RESOLVED_VERIFIED"


def test_a_stated_relation_is_decided_beyond_noise_only():
    rel = "err_ours < err_base"
    out = "\n".join(f'REFEREE_RESULT {{"err_ours": {a}, "err_base": {b}}}' for a, b in ((1, 3), (2, 2.5)))
    assert execute.relation_margins(out + '\nREFEREE_RESULT {"err_ours": 1}', rel) == [2.0, 0.5]
    st = lambda kind, ms: reconcile(kind, "", ms, "", {}, False, True, "", rel)["status"]
    assert st("RELEASED_DATA", [0.3]) == "RELATION_HOLDS" and st("RELEASED_DATA", [-0.3]) == "RELATION_VIOLATED"
    assert st("RELEASED_DATA", [0.0]) == "RELATION_VIOLATED"            # strict: a tie is not "<"
    assert st("RECONSTRUCTION", [0.3]) == "INCONCLUSIVE"                # one run of an experiment: no band
    assert st("RECONSTRUCTION", [0.3, 0.31, 0.29]) == "RELATION_HOLDS"
    assert st("RECONSTRUCTION", [0.3, -0.3, 0.1, -0.1]) == "INCONCLUSIVE"
    assert st("RECONSTRUCTION", [-0.3, -0.31, -0.29]) == "RELATION_VIOLATED"
    assert st("RECONSTRUCTION", []) == "INCONCLUSIVE"
    assert reconcile("RECONSTRUCTION", "", [], "x", {"setup_error": "importerror"}, False, True, "", rel)["status"] \
        == "INCONCLUSIVE"                                                  # environment never convicts


def test_dependency_recovery_is_documented_and_bounded():
    pip = ("ERROR: Ignored the following versions that require a different python version: 1.21.2 Requires-Python "
           ">=3.7,<3.11; 2.5.0 Requires-Python >=3.12\nERROR: No matching distribution found for scikit-lr")
    gxx = "error: [Errno 2] No such file or directory: 'g++'"
    with tempfile.TemporaryDirectory() as t:
        co = Path(t)
        (co / "pyproject.toml").write_text('requires-python = ">=3.11"\n', encoding="utf-8")
        assert execute.recover(pip, "python:3.11-slim", co)[0] == "python:3.12-slim"
        assert execute.recover(gxx, "python:3.11-slim", co)[0] == "python:3.11"      # same Python, with compilers
        assert execute.recover(gxx, "python:3.11", co) is None                      # nothing left: BLOCKED
        assert execute.recover("No matching distribution found for torch==9", "python:3.11-slim", co) is None
        (co / "pyproject.toml").write_text('requires-python = ">=3.10,<3.12"\n', encoding="utf-8")
        assert execute.recover(pip, "python:3.11-slim", co) is None                 # the declared range forbids it
        (co / ".python-version").write_text("3.12\n", encoding="utf-8")
        assert execute.image_for(co) == "python:3.12-slim"


def test_status_is_about_central_claims_and_conflicting_readings_are_recorded():
    """Sep-29 changepoint: an incidental ARITHMETIC_CONSISTENT made the paper SUPPORTED_BY_CHECK
    while every central claim was NOT_VERIFIED."""
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        plan = {"checks": [{"id": "C1", "kind": "CERTIFICATE", "concerns": [], "claim": "x", "statement": "Theorem 1"},
                           {"id": "C2", "kind": "ARITHMETIC", "concerns": [], "claim": "y", "printed": "26.7",
                            "target": {"quote": "q 26.7", "value": "26.7"}}],
                "central_claims": [{"quote": "a", "page": 1, "checks": ["C1"], "why_unchecked": "", "claim_type": "theory"},
                                   {"quote": "b", "page": 1, "checks": [], "why_unchecked": "figure", "claim_type": "theory"}]}
        state.write_json(root / "sealed" / "plan.json", plan)
        assert report.scientific_status(root) == "CHECKS_PENDING"
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "NO_VIOLATION_FOUND", "values": [0]})
        for s in ("ARITHMETIC_CONSISTENT", "ARITHMETIC_CONTRADICTION"):   # incidental: never lifts or sinks it
            state.write_json(root / "checks" / "C2" / "outcome.json", {"status": s, "values": [0]})
            assert report.scientific_status(root) == "CENTRAL_NO_VIOLATION_FOUND"
        assert report._central(plan, report._checks(root, plan), [])[0]["claim_status"] == "NO_VIOLATION_FOUND"
        dev = {"printed": "for j = 1..J", "used": "j = 0..J-1", "why": "1-based leaves the range", "page": 6,
               "changes_claim": True}
        state.write_json(root / "checks" / "C1" / "check.json", {"deviations": [dev]})
        state.write_json(root / "checks" / "C1" / "outcome.json",
                         {"status": "NO_VIOLATION_FOUND", "values": [0], "literal": {"undefined": 36}})
        assert report._central(plan, report._checks(root, plan), [])[0]["claim_status"] == "READING_CHANGED"
        assert report.scientific_status(root) == "CENTRAL_READING_CHANGED"      # a changed reading: not support
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "PREMISE_NOT_MET", "values": [0]})
        assert report.scientific_status(root) == "CENTRAL_PREMISE_NOT_MET"      # an unmet premise: not a failure
        plan["checks"].append({"id": "C3", "kind": "CERTIFICATE", "concerns": [], "claim": "x", "statement": "Theorem 1"})
        plan["central_claims"][1]["checks"] = ["C3"]
        state.write_json(root / "checks" / "C1" / "check.json", {})
        state.write_json(root / "checks" / "C3" / "outcome.json", {"status": "COUNTEREXAMPLE_FOUND", "values": [0]})
        conf = report.conflicts(report._checks(root, plan))       # same reading, opposite results
        assert conf == []                                         # C1's premise was never met: nothing to disagree
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "NO_VIOLATION_FOUND", "values": [0]})
        conf = report.conflicts(report._checks(root, plan))       # same reading, opposite results
        assert len(conf) == 1 and not conf[0]["explained"] and conf[0]["checks"] == ["C1", "C3"]
        plan["checks"] = plan["checks"][:2] + [{**plan["checks"][2], "statement": "Lemma 2", "step": "hence x < 1"}]
        assert report._central(plan, report._checks(root, plan), [])[1]["claim_status"] == "PENDING"   # 2026-10-02: unaudited
        _audited(root, "C3")
        claims = report._central(plan, report._checks(root, plan), [])   # a failed proof STEP is a gap, not a refutation
        assert claims[1]["claim_status"] == "PROOF_GAP_FOUND" and report._headline([], claims) == "CENTRAL_PROOF_GAP_FOUND"
        plan["central_claims"][1]["checks"] = []
        assert report._central(plan, report._checks(root, plan), [])[1]["claim_status"] == "NOT_CHECKED"


def test_a_counterexample_satisfies_every_premise_of_the_exact_claim():
    """Sep-29 central audit: changepoint C3's 'counterexample' dropped the step's premise (it was
    unsatisfiable), and GRACE/label-ranking held only under changed indexing."""
    from harness.reconcile import certificate
    row = lambda v, p, lit=None: {"violated": v, "premises": p, "literal": lit}
    assert certificate([row(1, 0)] * 15, False, True)["status"] == "PREMISE_NOT_MET"          # premise never met
    assert certificate([row(1, 1), row(0, 1)], False, False)["status"] == "COUNTEREXAMPLE_FOUND"
    assert certificate([row(1, 1)], True, False)["status"] == "VIOLATION_UNDER_CHANGED_READING"
    assert certificate([row(0, 1, "fails")], True, False)["status"] == "COUNTEREXAMPLE_FOUND"  # fails exactly as printed
    und = certificate([row(0, 1, "undefined")] * 36, True, False)
    assert und["status"] == "NO_VIOLATION_FOUND" and und["literal"]["undefined"] == 36 and und["reading"].startswith("changed")
    assert certificate([row(1, None)], False, False)["status"] == "INCONCLUSIVE"               # premises unsaid
    assert certificate([row(0, 0), row(0, 1)], False, False)["admissible"] == 1
    rows = execute.cert_rows('REFEREE_RESULT {"violated": 1, "premises_hold": 0}\nREFEREE_RESULT {"violated": 0}')
    assert rows == [{**row(1, 0), "exact": False}, {**row(0, None), "exact": False}]
    with tempfile.TemporaryDirectory() as t:                  # the seal demands premises and honest deviations
        cfg = state.Config()
        plan = {"checks": [{"id": "C1", "kind": "CERTIFICATE", "metric": "", "statement": "Lemma 1", "step": ""}]}
        x = type("X", (), {"root": Path(t), "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        g = {"script": script, "runs": 1, "outputs": ["violated"], "bindings": b}
        assert "premises_hold" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", g, final=False))
        g["script"] = script.replace("{'violated': 0}", "{'violated': 0, 'premises_hold': 1}")
        dev = {"printed": "", "used": "j from 0", "why": "range"}
        assert "changes_claim" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": [dev]}, final=False))
        assert "literal" in _refused(lambda: tasks._seal_gen(
            x, "gen:C1.1", {**g, "deviations": [{**dev, "changes_claim": True}]}, final=False))
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": [{**dev, "changes_claim": False}]}, final=False)["deviations"]


def test_small_samples_are_decided_with_student_t():
    """Sep-29 central audit: changepoint C1 held 'beyond 2 SE' on 3 replicates; t(2)=4.30 says no."""
    from harness.reconcile import t975
    rel = "err_km < err_lb"
    st = lambda kind, ms: reconcile(kind, "", ms, "", {}, False, True, "", rel)["status"]
    assert st("RECONSTRUCTION", [0.2417, 0.1411, 0.0821]) == "INCONCLUSIVE"
    assert st("RECONSTRUCTION", [0.30, 0.31, 0.29]) == "RELATION_HOLDS"
    assert t975(2) == 4.303 and t975(29) == 2.086 and t975(5000) == 1.96
    ok = lambda printed, values: reconcile("RECONSTRUCTION", printed, values, "", {}, True, True, "")["status"]
    assert ok("61.4", [61.0, 61.8, 61.5]) == "RESOLVED_VERIFIED"          # inside the CI of the mean
    assert ok("61.4", [70.0, 70.1, 69.9]) == "FAILED_REPRODUCTION"        # outside the prediction interval
    assert ok("61.4", [62.2, 63.0, 62.6]) == "INCONCLUSIVE"               # between: consistent, not pinned down


def test_arithmetic_errors_from_extracted_text_need_the_page_image():
    """Sep-29 central audit: a CONFIRMED 'arithmetic error' ('about 103 (= 51326/83)') was almost
    surely 10^3 flattened by text extraction."""
    from harness.evidence import mask, printed_form
    assert mask("about 103(= 51326/83) times", "103") == "about [?](= 51326/83) times"
    assert printed_form("10³") == printed_form("10^3") != printed_form("103")
    p = Paper(["Total frames 1,369,349 over all users; mean length is 26.7."])   # a bare-number quote
    ctx = p.masked_context(p.find("1,369,349")[0], "1369349")                    # still gets its words
    assert ctx == "Total frames [?] over all users; mean length is 26.7."
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        calc = {"operands": [{"name": "a", "quote": "reaches 61.4 accuracy", "value": "61.4"}], "expression": "a",
                "paper_result": {"quote": "The baseline reaches 59.3", "value": "59.3"}}
        concern = {"title": "sum", "severity": "MAJOR", "confidence": "HIGH", "class": "CONFIRMED_FINDING",
                   "evidence": [{"quote": "reaches 61.4 accuracy"}], "calculation": calc}
        _seal(cfg, pid, "lens:overclaim", {"concerns": [concern]}, td)
        for lens in tasks.LENSES[1:]:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        x = tasks._Ctx(cfg, pid)
        assert x.concerns()[0]["class"] == "PLAUSIBLE_CONCERN"            # not yet read off the page image
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        assert "vision:concerns" in {t["id"] for t in tasks._plan(tasks._Ctx(cfg, pid))[1]}
        _seal(cfg, pid, "vision:concerns", {"items": [{"id": "overclaim-01:a", "printed": "61.4"},
                                                      {"id": "overclaim-01:result", "printed": "59.8"}]}, td)
        c = tasks._Ctx(cfg, pid).concerns()[0]
        assert c["class"] == "OPEN_QUESTION" and c["severity"] == "MINOR" and "59.8" in c["image_check"]


def test_resource_limits_end_in_one_documented_blocker():
    st = {}
    oom = {"mode": "evidence", "error": "out of memory", "seconds": 40}
    assert execute.resource_action(oom, st, "0", 3600)[0] == "retry" and st["width"] == 1   # alone, once
    act, why = execute.resource_action(oom, st, "0", 3600)
    assert act == "blocker" and why[0] == "memory" and "not repeated" in why[1]            # never a third time
    assert execute.resource_action({"mode": "evidence", "timed_out": True}, {}, "0", 60)[0] == "blocker"
    st = {"pilot_s": 84}                      # a replicate of an 84 s script past 3600 s: the host stalled
    assert execute.resource_action({"mode": "evidence", "timed_out": True}, st, "39", 3600)[0] == "retry"
    assert execute.resource_action({"mode": "evidence", "timed_out": True}, st, "39", 3600)[0] == "blocker"  # once
    assert execute.resource_action({"mode": "evidence", "timed_out": True}, {"pilot_s": 1200}, "3", 3600)[0] == "blocker"
    assert execute.resource_action({"mode": "evidence", "returncode": 255}, {}, "0", 60) == ("", "")
    with tempfile.TemporaryDirectory() as t:                  # completed seeds of the same script are reused
        cdir = Path(t)
        for k, sha in ((0, "a"), (1, "a"), (0, "b")):
            state.append_jsonl(cdir / "seeds.jsonl", {"key": sha, "seed": k, "values": [k], "seconds": 5})
        assert [r["seed"] for r in execute._checkpoints(cdir, {"script_sha256": "a", "runs": 3})] == [0, 1]
    pr = execute.protocol({"kind": "RECONSTRUCTION", "runs": 3, "runs_source": "referee_floor",
                           "target": {"relation": "a < b"}, "deviations": [
                               {"printed": "", "used": "thresholds 50..700", "changes_claim": False},
                               {"printed": "x", "used": "0-based j", "changes_claim": True},
                               {"printed": "", "used": "two baselines only", "changes_claim": True}]}, {"pilot_s": 8}, "t-test")
    assert "REFEREE" in pr["runs_from"] and pr["supplied_by_referee"] == ["thresholds 50..700"]   # each choice listed once
    assert pr["claim_changes"] == ["0-based j", "two baselines only"]
    assert "planner" in pr["relation"] and pr["pilot_seconds"] == 8


def test_detached_steps_outlive_their_poller():
    """Sep-29 central run: every host process a tool call started died with that call, and a
    second env build wrote into the same dir as the orphaned first (a corrupt venv). A step is
    now a named container: a second start adopts it, and any later poll collects it."""
    if not execute.docker_status()[0]:
        return
    import time
    spec = {"mounts": [], "workdir": "/", "image": execute.DEFAULT_IMAGE, "network": False, "mode": "try", "target": "t"}
    name = execute._cname("kernel", time.time())
    rec = execute.start(name, ["sh", "-c", 'echo \'REFEREE_RESULT {"x": 1}\'; echo oops >&2'], **spec)
    assert "returncode" not in execute.start(name, ["sh", "-c", "echo second writer"], **spec)   # adopted
    done = None
    for _ in range(90):
        if (done := execute.collect(rec, 60)):
            break
        time.sleep(1)
    assert done and done["returncode"] == 0 and execute.result_values(done["stdout"], "x") == [1.0]
    assert "oops" in done["stderr"] and "second writer" not in done["stdout"]
    slow = execute.start(execute._cname("kernel-slow", time.time()), ["sleep", "60"], **spec)
    for _ in range(90):
        if (done := execute.collect(slow, 2)):
            break
        time.sleep(1)
    assert done and done["timed_out"] and done["returncode"] is None
    gone = execute.start(execute._cname("kernel-gone", time.time()), ["sleep", "30"], **spec)
    execute._docker(["docker", "rm", "-f", gone["container"]], 60)       # removed by hand, uncollected
    assert execute._vanished(execute.collect(gone, 60))                  # an infrastructure event, restarted
    with tempfile.TemporaryDirectory() as t:        # an env is a named volume, built by polls, never blocking
        cfg = state.Config()
        cfg.allow_install = cfg.allow_network = True
        env_dir = Path(t) / "env"
        env = None
        for _ in range(120):
            if (env := execute.ensure_env(cfg, Path(t), env_dir, execute.DEFAULT_IMAGE, None)):
                break
            time.sleep(2)
        assert env and env["ok"] and env["volume"] == execute.volume(env_dir) and "freeze sha256" in env["detail"]
        assert execute._docker(["docker", "volume", "rm", "-f", env["volume"]], 60)[0] == 0


def test_a_host_that_slept_is_no_run_time():
    """Oct-01 PPRM C8/C9: the laptop slept on a critical battery from 10:52 to 17:01Z with two evidence runs in flight.
    The first poll after it read 23043 s of wall time as a run past the 3600 s limit, killed both and blocked them as
    RESOURCE BLOCKER (per_run_timeout); C8 had just printed its result. A run's time is the host's awake time."""
    import datetime
    import time as _t
    iso = lambda s: datetime.datetime.fromtimestamp(s, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    now, box, calls = _t.time(), {}, []

    def docker(a, timeout):
        calls.append(a[1])
        if a[1] == "kill":
            box.update(Running=False, FinishedAt=iso(_t.time()), ExitCode=137)
        return (0, json.dumps(box)) if a[1] == "inspect" else (0, "")
    real = execute._docker, execute.awake
    execute._docker = docker
    try:
        rec = {"container": "referee-kernel-sleep", "awake_offset": (now - 23043) - 1000.0}   # awake clock 1000 at start
        box.update(Running=True, StartedAt=iso(now - 23043))
        execute.awake = lambda: 1000.0 + 912 + (_t.time() - now)   # 912 s awake since: the host slept 22131 s
        assert execute.collect(rec, 3600) is None and "kill" not in calls          # still within its limit: never killed
        box.update(Running=False, FinishedAt=iso(now - 9), ExitCode=0)              # it ended on its own, after 903 s awake
        done = execute.collect(rec, 3600)
        assert done["returncode"] == 0 and not done["timed_out"] and "error" not in done, done
        assert 850 < done["seconds"] < 950 and 22000 < done["host_slept_s"] < 22200
        # The same wall time on a host that never slept is past the limit: killed, recorded as timed out (blocked).
        calls.clear()
        box.clear()
        box.update(Running=True, StartedAt=iso(now - 23043))
        execute.awake = lambda: 1000.0 + 23043 + (_t.time() - now)
        done = execute.collect(rec, 3600)
        assert "kill" in calls and done and done["timed_out"] and done["returncode"] is None, done
        assert execute.resource_action({**done, "mode": "evidence"}, {}, "0", 3600)[0] == "blocker"
        assert "host_slept_s" not in done
        box.update(Running=True, StartedAt=iso(now - 23043))                        # a record from before the clock
        calls.clear()
        execute.collect({"container": "referee-kernel-sleep"}, 3600)               # (no awake_offset): wall time, as before
        assert "kill" in calls
        assert isinstance(real[1](), float) and real[1]() >= 0                      # the real clock answers on this host
    finally:
        execute._docker, execute.awake = real


def test_measurements_survive_a_later_stage_failure():
    """Sep-29 label ranking C2: political's five folds were measured (in stderr), then the movies
    stage exited 1 at 29 s; the check read 'no sign the experiment itself began' and kept nothing."""
    from harness.reconcile import PARTIAL
    run = {"returncode": 1, "seconds": 29, "stdout": "",
           "stderr": 'REFEREE_PROGRESS {"stage": "movies"}\nFit:  14%|█▍        | 7/50 [00:05<00:31,  1.37it/s]\n'
                     "parsed data for movies does not match the paper Table 4: got (260, 15, 256)"}
    assert execute.classify(run)["reached"]                                  # a progress line in stderr counts
    assert execute.failure_text(run).startswith("exit 1 after 29s: REFEREE_PROGRESS") and "260, 15, 256" in \
        execute.failure_text(run) and "it/s]" not in execute.failure_text(run)
    rel = "ece * 10 > 1"
    out = 'REFEREE_RESULT {"stage": "political", "ece": 0.17}\nREFEREE_RESULT {"stage": "movies", "x": 1}'
    got = execute.staged_values(out, rel, "")                               # a line lacking a name is not a result
    assert len(got) == 1 and got[0][0] == "political" and abs(got[0][1] - 0.7) < 1e-9
    st = lambda staged, errs, failed: reconcile("RECONSTRUCTION", "", [v for _, v in staged], "", {}, True, True, "",
                                                rel, staged=staged, failed=failed, stage_errors=errs)
    pol = [["political", m] for m in (0.70, 0.72, 0.69)]
    part = st(pol, {"movies": "exit 1: data mismatch"}, {"0": "exit 1", "1": "exit 1", "2": "exit 1"})
    assert part["status"] == PARTIAL and part["status_on_completed"] == "RELATION_HOLDS"
    assert part["stages"]["political"]["n"] == 3 and part["stages"]["movies"]["status"] == "NOT_COMPLETED"
    bad = st([["political", -m] for m in (0.70, 0.72, 0.69)], {"movies": "exit 1"}, {"0": "exit 1"})
    assert bad["status"] == PARTIAL and bad["status_on_completed"] == "RELATION_VIOLATED"   # kept, not the verdict
    assert st([["political", -m] for m in (0.70, 0.72, 0.69)], {}, {})["status"] == "RELATION_VIOLATED"
    assert execute._num({"a": float("nan"), "b": 1, "c": True}) == {"b": 1.0}   # a NaN is no measurement
    long_id = {}
    execute.markers(long_id, {"stdout": "REFEREE_DATA " + __import__("json").dumps(
        {"dataset": "d", "observed": {"labels": ["x" * 20] * 600}}), "stderr": ""})
    assert "truncated" in long_id["data_identity"]["d"]                    # a long identity line never crashes
    assert st(pol + [["movies", m] for m in (0.3, 0.31, 0.32)], {}, {})["status"] == "RELATION_HOLDS"
    mixed = st(pol + [["movies", m] for m in (0.3, -0.31, 0.02)], {}, {})
    assert mixed["status"] == "INCONCLUSIVE"                                 # every stage must hold
    assert mixed["rule"].startswith("mean paired margin")                    # ...and the rule it was decided by is said
    single = reconcile("RECONSTRUCTION", "", [0.7, 0.72], "", {}, True, True, "", rel,
                       staged=[["", 0.7], ["", 0.72]], failed={"2": "exit 1"})
    assert single["status"] == PARTIAL and single["status_on_completed"] == "RELATION_HOLDS"
    c = {"id": "C1", "kind": "RECONSTRUCTION", "evidence": "PAPER_DERIVED_IMPLEMENTATION", "deviations": [],
         "status": PARTIAL}
    assert report._claim_status([c]) == "PARTIAL_EVIDENCE"
    assert report._headline([c], [{"claim_status": "PARTIAL_EVIDENCE"}]) == "CENTRAL_PARTIAL_EVIDENCE"
    assert report._state(Path("."), "C1", {"status": PARTIAL}) == "PARTIALLY_COMPLETED"
    assert report._state(Path("."), "C1", {"status": "BLOCKED", "reason": "RESOURCE BLOCKER: x"}) == "RESOURCE_LIMITED"
    with tempfile.TemporaryDirectory() as t:        # an operator stop keeps what completed, per stage, deciding nothing
        cfg = state.Config()
        cfg.projects = Path(t)
        (Path(t) / "p" / "checks" / "C1").mkdir(parents=True)
        state.write_json(Path(t) / "p" / "checks" / "C1" / "check.json",
                         {"id": "C1", "kind": "RECONSTRUCTION", "runs": 100, "target": {"relation": "a < b"}})
        state.write_json(Path(t) / "p" / "checks" / "C1" / "exec.json", {
            "stage": "run", "seed": 2, "records": 2, "fly": {}, "values": [0.1, 0.3, 0.2, 0.4],
            "staged": [["k5", 0.1], ["k20", 0.3], ["k5", 0.2], ["k20", 0.4]]})
        o = execute.stop(cfg, "p", "C1", "the host")
        assert o["status"] == "INCONCLUSIVE" and not o.get("stages")
        assert o["completed_stages"] == {"k5": {"n": 2, "mean": 0.15}, "k20": {"n": 2, "mean": 0.35}}
    stopped = {"status": "INCONCLUSIVE", "values": [0.1] * 42, "runs": 14, "protocol": {"runs": 100}}
    assert report._state(Path("."), "C1", stopped) == "PARTIALLY_COMPLETED"     # 14 of 100 runs is not COMPLETED
    assert report._state(Path("."), "C1", {**stopped, "runs": 100}) == "COMPLETED"


def test_a_proof_candidate_that_violates_a_premise_or_precision_is_not_a_counterexample():
    """Sep-29: GRACE Thm 5.1's '8 violations' were float round-off (|lhs-rhs| ~ 4e-16) in a script
    that claimed exact arithmetic; label ranking's 6 literal failures were reported with 'admissible:
    12' under a reading that had moved the step's own assertion into its premises."""
    from harness.reconcile import certificate
    fuzzy = {"violated": 1, "premises": 1, "literal": None, "lhs": -19.731718235877057, "rhs": -19.73171823587706,
             "exact": False}
    res = certificate([fuzzy] * 8 + [{"violated": 0, "premises": 1, "lhs": 1.0, "rhs": 1.0, "exact": False}] * 2,
                      False, False)
    assert res["status"] == "INCONCLUSIVE" and res["below_precision"] == 8
    assert certificate([{**fuzzy, "exact": True}], False, False)["status"] == "COUNTEREXAMPLE_FOUND"
    assert certificate([{**fuzzy, "lhs": 1.0, "rhs": 2.0}], False, False)["status"] == "COUNTEREXAMPLE_FOUND"
    rows = [{"violated": 0, "premises": 1, "literal": "holds"}] * 12 + [
        {"violated": 0, "premises": 0, "literal": "fails"}] * 6 + [{"violated": 0, "premises": 0, "literal": "undefined"}] * 6
    lit = certificate(rows, True, True)
    assert lit["status"] == "COUNTEREXAMPLE_FOUND" and lit["admissible"] == 12 and lit["violated_admissible"] == 0
    assert "exactly as printed" in lit["reason"] and "0 of 12 admissible" in lit["reason"]
    step = {"id": "C1", "kind": "CERTIFICATE", "evidence": "PROOF_AUDIT", "status": "COUNTEREXAMPLE_FOUND",
            "deviations": [{"changes_claim": True}]}
    assert report._claim_status([step], claim_type="theory") == "PROOF_GAP_FOUND"   # the printed step fails as printed
    assert certificate([{"violated": 1, "premises": 0, "literal": "premise_not_met"}] * 3, True, True)["status"] \
        == "PREMISE_NOT_MET"
    crashed = certificate([{"violated": 0, "premises": 1}], False, False, crashed=2)
    assert crashed["status"] == "NO_VIOLATION_FOUND" and crashed["crashed"] == 2 and "crashed" in crashed["reason"]
    assert execute.cert_rows('REFEREE_RESULT {"violated": 1, "premises_hold": 1, "lhs": 1, "rhs": 2, '
                             '"lhs_exact": "1", "rhs_exact": "2"}')[0]["exact"]


def test_support_is_limited_by_the_recorded_changes():
    """Sep-29 GRACE C1: no tuning, a rebuilt baseline, different training splits and a changed
    aggregation were declared as claim changes, yet a relation holding would have read SUPPORT_FOUND."""
    c = {"id": "C1", "kind": "RECONSTRUCTION", "evidence": "PAPER_DERIVED_IMPLEMENTATION",
         "status": "RELATION_HOLDS", "deviations": [{"printed": "tuned", "used": "untuned", "changes_claim": True}]}
    assert report._claim_status([c]) == "READING_CHANGED"
    assert report._claim_status([{**c, "status": "RELATION_VIOLATED"}]) == "READING_CHANGED"
    assert report._claim_status([{**c, "deviations": [{"changes_claim": False}]}]) == "SUPPORT_FOUND"
    led = {"checks": [{"id": "C1", "status": "COUNTEREXAMPLE_FOUND"}], "concerns": []}
    rows = report._check_rows([{"id": "C1", "kind": "CERTIFICATE", "evidence": "INSTANCE_CHECK", "claim": "x",
                                "target": None, "printed": "", "step": "", "statement": "s", "values": [1],
                                "status": "PREMISE_NOT_MET", "deviations": [], "state": "COMPLETED",
                                "reason": "no admissible instance; a counterexample must satisfy every premise",
                                "rule": "", "reason_by": "harness"}], led)
    assert "withheld" not in rows[-1]                                     # the harness's own words are facts
    rows = report._check_rows([{"id": "C1", "kind": "RECONSTRUCTION", "evidence": "x", "claim": "x", "target": None,
                                "printed": "", "step": "", "statement": "", "values": [], "status": "NOT_CHECKABLE",
                                "deviations": [], "reason": "the author refused: this refutes the paper", "rule": "",
                                "reason_by": "model"}], led)
    assert "withheld" in rows[-1]                                         # a model's status word is screened


def test_a_cited_public_artifact_outside_the_checkout_is_acquirable_with_provenance():
    """Sep-29 changepoint WISDM: the checkout's README names the public download, yet the check
    ended 'dataset absent from checkout'; label ranking's RewardBench scores are an HF dataset the
    authors' code names. Only sources the paper or a tracked checkout file cites are fetched."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        co = td / pid / "repo"
        co.mkdir(parents=True)
        (co / "README.md").write_text("Download the data from https://www.example.org/lab/dataset.php first.\n", "utf-8")
        (co / "analyze.py").write_text('hf_hub_download("allenai/some-results", repo_type="dataset")\n', "utf-8")
        git = lambda *a: subprocess.run(["git", *a], cwd=co, capture_output=True, text=True)
        git("init", "-q"), git("add", "."), git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c")
        x = tasks._Ctx(cfg, pid)
        ok = lambda src, cited: tasks._acquire(x, {"acquire": [{"source": src, "cited_in": cited, "include": ["*.gz"]}]},
                                               errs := [], "C1") and not errs
        assert ok("https://www.example.org/lab/dataset.php", "README.md")
        assert ok("http://www.example.org/lab/dataset.php", "README.md")        # the scheme is not the citation
        assert ok("hf://datasets/allenai/some-results", "analyze.py")
        assert not ok("https://www.example.org/other.tar.gz", "README.md")     # not cited
        assert not ok("https://www.example.org/lab/dataset.php", "../outside.md")   # not a tracked file
        assert not ok("hf://datasets/ICML-2026-agent-repro/verdicts", "analyze.py")  # denied, cited or not
        assert not ok("file:///etc/passwd", "paper")
        assert not ok("https://www.example.org@evil.example/x.gz", "README.md")    # user info hides the host
        assert not ok("https://www.example.org/", "README.md")                     # a prefix is not the citation
        assert not ok("https://example.org/lab/dataset.php", "README.md")          # inside another host name
        assert not ok("hf://datasets/nai/some-results", "analyze.py")              # inside another hub id
        assert execute.data_mount(td / pid, "C1") == []                       # nothing acquired, nothing mounted
        state.write_json(td / pid / "checks" / "C1" / "data.json", {"n_files": 2, "files": []})
        assert execute.data_mount(td / pid, "C1")[0][1:] == (execute.DATA_MOUNT, True)   # read-only


def test_every_claim_scope_item_is_covered_or_omitted_with_a_reason_and_undecided_claims_get_a_follow_up():
    """Sep-29 label ranking: the calibration claim compares five models; the check tested two and
    RPC's omission was recorded nowhere. GRACE's layer-replacement claim was never listed."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        x = tasks._Ctx(cfg, pid)
        chk = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
               "statement_quote": "The final loss is -0.52", "covers": ["PL", "MM"], "role": "target"}
        claim = {"quote": "We report the mean over 5 random seeds", "checks": ["B"], "scope": ["PL", "MM", "RPC"],
                 "claim_type": "theory"}
        assert "RPC" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [chk], "central_claims": [claim]},
                                                          final=False))
        claim["omitted"] = [{"item": "RPC", "why": "its pairwise ECE needs the vendored Cython build, which failed",
                             "blocker": "other"}]
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [claim]}, td)
        state.write_json(td / pid / "checks" / "C1" / "outcome.json", {"status": "NOT_CHECKABLE", "reason": "refused"})
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and [o["id"] for o in owed] == ["plan:2"]   # an undecided central claim: a follow-up
        assert "FOLLOW-UP" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        _seal(cfg, pid, "plan:2", {"checks": [{**chk, "id": "F1", "covers": ["RPC"]}],
                                   "central_claims": [{"quote": claim["quote"], "checks": ["F1"], "claim_type": "theory",
                                                       "scope": ["RPC"]}]}, td)
        plan = tasks._Ctx(cfg, pid).plan()
        assert [c["id"] for c in plan["checks"]] == ["C1", "C7"] and plan["central_claims"][0]["checks"] == ["C1", "C7"]
        led = report.ledger(tasks._Ctx(cfg, pid))
        assert led["central_claims"][0]["omitted"][0]["item"] == "RPC"
        assert report.scientific_status(td / pid) == "CHECKS_PENDING"      # the follow-up check is still owed
        state.write_json(td / pid / "checks" / "C7" / "outcome.json", {"status": "NOT_CHECKABLE", "reason": "x"})
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "report"                                             # one follow-up round, not a loop


def test_no_verifier_sees_a_script_the_harness_has_not_run():
    """Sep-29: every draft of every check failed (a named-volume env was bind-mounted as an empty
    host dir) or never ran ('environment still building'), so each script was approved unexecuted."""
    assert execute._src("referee-874db90acf9f1b5d") == "referee-874db90acf9f1b5d"   # a volume name stays a name
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        chk = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
               "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [{"quote": chk["claim_quote"], "checks": ["B"],
                                                                      "claim_type": "theory", "scope": ["the loss"]}]}, td)
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0, 'premises_hold': 1})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        _seal(cfg, pid, "gen:C1.1", {"script": script, "runs": 1, "outputs": ["violated"], "bindings": b}, td)
        state.write_json(td / pid / "checks" / "C1" / "smoke.1.json",
                         {"returncode": 1, "failed": True, "failure": "exit 1 after 0s: NameError: name 'q' is not defined",
                          "stderr": "Traceback ... NameError", "stdout": "", "reached": False})
        _, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert [o["id"] for o in owed] == ["gen:C1.2"]                       # back to the author, not to a verifier
        assert "NameError" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")


def test_extra_packages_are_a_thin_layer_not_a_copy_of_the_environment():
    """Sep-29 repaired run: each script declaring packages copied the authors' ~6 GB torch venv,
    filling the host disk. The packages now live in their own volume over the read-only base."""
    builder, steps, _ = execute._env_steps(None, ("econml==0.15.1",), base="referee-base")
    assert "cp -a" not in " ".join(steps) and any("--target /env/extra" in s for s in steps)
    mounts, env = execute.env_mounts(Path("x"), {"layered": True, "base": "referee-base"})
    assert mounts[0] == ("referee-base", "/env", True) and mounts[1][1:] == ("/extra", True)
    assert env == {"PYTHONPATH": "/extra/extra"}
    assert execute.env_mounts(Path("x"), {"ok": True})[0] == [(execute.volume(Path("x")), "/env", True)]


def test_a_vanished_volume_is_rebuilt_never_mounted_empty():
    """Sep-30: Docker's storage was reset under a paused run; every 'built' marker then named a
    volume that no longer existed. The marker is set aside and the same build or download runs
    again; a daemon that cannot answer is not taken as 'gone'."""
    saved = execute._volume_gone, execute.docker_status
    try:
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = state.Config()
            cfg.projects, cfg.allow_install, cfg.allow_network = td, True, True
            env_dir = td / "env"
            state.write_json(env_dir / "referee-env.json", {"ok": True, "volume": "referee-x", "image": "i"})
            state.write_json(td / "checks" / "C1" / "data.json", {"n_files": 2, "volume": "referee-d", "files": []})
            execute.docker_status = lambda: (False, "away")          # nothing starts in this test
            execute._volume_gone = lambda v: False
            assert execute.ensure_env(cfg, td, env_dir, "i", None)["ok"]
            assert execute.fetch(cfg, td, "C1", [{"source": "u"}])["n_files"] == 2
            execute._volume_gone = lambda v: True
            assert execute.ensure_env(cfg, td, env_dir, "i", None) is None
            assert (env_dir / "referee-env.vanished.json").exists() and not (env_dir / "referee-env.json").exists()
            assert execute.fetch(cfg, td, "C1", [{"source": "u"}]) is None
            assert (td / "checks" / "C1" / "data.vanished.json").exists()
            # A running check's env being rebuilt under the same volume name (Sep-30: two seeds ran
            # against a half-built env) is a change, though the volume exists again.
            execute._volume_gone = lambda v: False
            st = {"env": {"ok": True, "volume": "referee-x", "detail": "built v; freeze 1 (the authors' env did not build)"}}
            assert execute._storage_changed(st, env_dir, td, {"id": "C1"})            # marker set aside: rebuilding
            state.write_json(env_dir / "referee-env.json", {"ok": True, "volume": "referee-x", "detail": "built v; freeze 1"})
            assert not execute._storage_changed(st, env_dir, td, {"id": "C1"})        # the same env, detail appended
            assert execute._storage_changed(st, env_dir, td, {"id": "C1", "acquire": [{"source": "u"}]})  # data re-acquiring
        execute._docker, real = (lambda a, t: (1, "Cannot connect to the Docker daemon")), execute._docker
        try:
            assert not saved[0]("referee-x")
        finally:
            execute._docker = real
    finally:
        execute._volume_gone, execute.docker_status = saved


def test_a_run_that_measures_then_fails_keeps_its_measurements_live():
    """Live (Docker): a script measures stage `a`, then fails in stage `b`, on every seed. The check
    runs all its seeds, keeps stage a's three results, and ends PARTIAL — never 'did not begin'."""
    if not execute.docker_status()[0]:
        return
    import time
    saved = execute.SCRIPT_PACKAGES
    execute.SCRIPT_PACKAGES = ()                               # a bare venv: quick to build
    try:
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cfg = state.Config()
            cfg.projects, cfg.allow_install, cfg.allow_network, cfg.allow_script_exec = td, True, True, True
            state.write_json(td / ".gpu.json", False)
            cdir = td / "p" / "checks" / "C1"
            cdir.mkdir(parents=True)
            state.write_json(td / "p" / "source.json", {})
            script = ("import argparse, json, sys\np = argparse.ArgumentParser(); p.add_argument('--seed', type=int)\n"
                      "s = p.parse_args().seed\nprint('REFEREE_PROGRESS ' + json.dumps({'units': ['a', 'b']}))\n"
                      "print('REFEREE_PROGRESS ' + json.dumps({'stage': 'setup'}), flush=True)\n"
                      "print('REFEREE_RESULT ' + json.dumps({'stage': 'a', 'gap': 0.5 + s / 100}), flush=True)\n"
                      "print('REFEREE_PROGRESS ' + json.dumps({'stage': 'b'}), file=sys.stderr, flush=True)\n"
                      "sys.exit('stage b: the parsed data do not match the paper')\n")
            (cdir / "script.py").write_bytes(script.encode("utf-8"))
            sha = state.sha256(script)
            state.write_json(cdir / "check.json", {"id": "C1", "kind": "RECONSTRUCTION", "runs": 3, "script_sha256": sha,
                                                   "target": {"relation": "gap > 0"}, "repo_attributed": False,
                                                   "approval": {"approved": True, "script_sha256": sha}})
            state.write_json(cdir / "exec.json", {"token": state.now()})
            t0 = time.time()
            while execute.poll(cfg, "p", "C1") and time.time() - t0 < 600:
                time.sleep(2)
            o = state.read_json(cdir / "outcome.json")
            assert o["status"] == "PARTIAL" and o["status_on_completed"] == "RELATION_HOLDS", o
            assert o["stages"]["a"]["n"] == 3 and o["stages"]["b"]["status"] == "NOT_COMPLETED"
            assert "do not match the paper" in o["stages"]["b"]["reason"] and len(o["failed_seeds"]) == 3
            assert "setup" not in o["stages"] and "(during b)" in o["failed_seeds"]["0"]   # a step is not a unit
            # Oct-01 PPRM C8: a run past the per-run limit is a RESOURCE BLOCKER, and what it printed before the
            # limit is kept beside it (pilot_stages), never discarded; it decides nothing.
            c2 = td / "p" / "checks" / "C2"
            c2.mkdir(parents=True)
            slow = ("import argparse, json, time\np = argparse.ArgumentParser(); p.add_argument('--seed', type=int)\n"
                    "p.parse_args()\nprint('REFEREE_RESULT ' + json.dumps({'stage': 'a', 'gap': 0.75}), flush=True)\n"
                    "time.sleep(600)\n")
            (c2 / "script.py").write_bytes(slow.encode("utf-8"))
            sha2 = state.sha256(slow)
            state.write_json(c2 / "check.json", {"id": "C2", "kind": "RECONSTRUCTION", "runs": 3, "script_sha256": sha2,
                                                 "target": {"relation": "gap > 0"}, "repo_attributed": False,
                                                 "approval": {"approved": True, "script_sha256": sha2}})
            state.write_json(c2 / "exec.json", {"token": state.now()})
            cfg.run_timeout_s, t0 = 25, time.time()
            while execute.poll(cfg, "p", "C2") and time.time() - t0 < 600:
                time.sleep(2)
            o = state.read_json(c2 / "outcome.json")
            assert o and o["status"] == "BLOCKED" and o["resource"] == "per_run_timeout", o
            assert o["pilot_stages"] == {"a": {"n": 1, "mean": 0.75}} and o["values"] == [], o
            execute._docker(["docker", "volume", "rm", "-f", execute.ckpt_volume(c2, 0)], 60)
            env_dir = td / ".script-env"                     # a draft mounts the env's named volume by name
            rec = execute.run(["/env/bin/python", "-c", "print('ok')"], mounts=[(execute.volume(env_dir), "/env", True)],
                              workdir="/", image=execute.DEFAULT_IMAGE, network=False, timeout=120, mode="try", target="t")
            assert rec["returncode"] == 0 and "ok" in rec["stdout"], rec
            execute._docker(["docker", "volume", "rm", "-f", execute.volume(env_dir)], 60)
            for k in range(3):
                execute._docker(["docker", "volume", "rm", "-f", execute.ckpt_volume(cdir, k)], 60)
    finally:
        execute.SCRIPT_PACKAGES = saved


def test_verify_commit_fails_closed():
    from harness.repo import verify_commit
    with tempfile.TemporaryDirectory() as t:
        r = Path(t)
        git = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True)
        git("init", "-q"), git("config", "user.email", "a@b"), git("config", "user.name", "a")
        (r / "a.py").write_text("x = 1\n")
        git("add", "."), git("commit", "-qm", "c")
        sha = git("rev-parse", "HEAD").stdout.strip()
        assert verify_commit(r, sha)[0]
        assert not verify_commit(r, "0" * 40)[0]
        (r / "new.py").write_text("y = 2\n")
        assert not verify_commit(r, sha)[0]                           # an untracked file counts


def test_the_result_schema_is_checked_apart_from_what_the_results_say():
    """Sep-30 label ranking C9: the script declared its metric's name as its one unit and printed
    an unnamed result line; the run completed, yet the check read PARTIAL ("a declared unit printed
    no result line"). A labeling defect is the script's, recorded apart; execution is not science."""
    from harness.reconcile import PARTIAL
    check = {"kind": "RELEASED_DATA", "metric": "mean_ece_top10", "target": {"quote": "q", "value": "0.22"}}
    rec = {"returncode": 0, "stdout": 'REFEREE_PROGRESS {"units": ["mean_ece_top10"]}\n'
                                      'REFEREE_RESULT {"mean_ece_top10": 0.0135}', "stderr": ""}
    defects = execute.result_schema(rec, check)
    assert len(defects) == 2 and "printed no result line" in defects[0] and "(no stage)" in defects[1]
    assert "0.0135" not in " ".join(defects)                                # names only, never a value
    staged = execute.staged_values(rec["stdout"], "", "mean_ece_top10")
    errs, labels = execute.split_units({"mean_ece_top10"}, staged, False, "no line")
    assert errs == {} and "without that `stage` name" in labels[0]
    # Sep-30 changepoint C7: 22 declared units printed nothing while other stages printed under names
    # the (then truncated) units list lacked: those 22 were not measured; they are never relabeled.
    assert execute.split_units({"a", "b"}, [["a", 1.0], ["c", 2.0]], False, "x") == ({"b": "x"}, [])
    assert len(execute.units({"stdout": "REFEREE_PROGRESS " + json.dumps({"units": [f"u{i}" for i in range(242)]})})) == 242
    assert execute.split_units({"mean_ece_top10"}, staged, True, "exit 1")[0] == {"mean_ece_top10": "exit 1"}
    assert execute.split_units({"a", "b"}, [["a", 1.0]], False, "no line")[0] == {"b": "no line"}   # b never measured
    res = reconcile("RELEASED_DATA", "0.22", [0.0135], "", {}, False, True, "", staged=staged, stage_errors=errs)
    assert res["status"] == "INCONCLUSIVE" and res["reason"].startswith("produced 0.0135")
    ok = {"runs_planned": 1, "runs_ended": 1, "runs_exited_ok": 1, "runs_failed": [], "schema_defects": labels}
    assert report._state(Path("."), "C9", {**res, "values": [0.0135], "execution": ok}) == "COMPLETED"
    # a unit genuinely not measured is partial evidence, yet the runs themselves completed
    assert report._state(Path("."), "C9", {"status": PARTIAL, "values": [1.0], "execution": ok}) == "COMPLETED"
    assert report._state(Path("."), "C9", {"status": "INCONCLUSIVE", "values": [1.0],
                                           "execution": {**ok, "runs_planned": 100, "runs_ended": 14}}) == "PARTIALLY_COMPLETED"
    assert report._state(Path("."), "C9", {"status": "INCONCLUSIVE", "values": [],
                                           "execution": {**ok, "runs_exited_ok": 0}}) == "FAILED"
    with tempfile.TemporaryDirectory() as t:        # the outcome records execution next to the finding
        cfg = state.Config()
        cfg.projects = Path(t)
        root = Path(t) / "p"
        (root / "checks" / "C9").mkdir(parents=True)
        execute._finish(cfg, root, {**check, "id": "C9", "runs": 1, "basis": "predictions", "printed": "0.22"},
                        {"values": [0.0135], "staged": staged, "records": 1, "ok_runs": 1, "schema_defects": labels})
        o = state.read_json(root / "checks" / "C9" / "outcome.json")
        assert o["status"] == "INCONCLUSIVE" and o["reproduced"] == 0.0135 and o["basis"] == "predictions"
        assert o["execution"] == {"runs_planned": 1, "runs_ended": 1, "runs_exited_ok": 1, "runs_failed": [],
                                  "schema_defects": labels}
    with tempfile.TemporaryDirectory() as t:        # a completed draft that breaks the contract goes back to its author
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        chk = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
               "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [{"quote": chk["claim_quote"], "checks": ["B"],
                                                                      "claim_type": "theory", "scope": ["the loss"]}]}, td)
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0, 'premises_hold': 1})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        _seal(cfg, pid, "gen:C1.1", {"script": script, "runs": 1, "outputs": ["violated"], "bindings": b}, td)
        assert execute._smoke_check(td / pid, "C1", 1)["metric"] == "violated"
        state.write_json(td / pid / "checks" / "C1" / "smoke.1.json",
                         {"returncode": 0, "failed": False, "reached": True, "stdout": "REFEREE_RESULT <masked>",
                          "stderr": "", "schema": ["no REFEREE_RESULT line carries every compared output ['violated']"]})
        _, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert [o["id"] for o in owed] == ["gen:C1.2"]
        assert "RESULT SCHEMA" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")


def _checkout(td: Path, pid: str, files: dict) -> None:
    co = td / pid / "repo"
    co.mkdir(parents=True)
    for name, text in files.items():
        (co / name).write_text(text, encoding="utf-8")
    git = lambda *a: subprocess.run(["git", *a], cwd=co, capture_output=True, text=True)
    git("init", "-q"), git("add", "."), git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c")


def test_a_paper_and_code_disagreement_is_computed_both_ways_never_chosen():
    """Sep-30 label ranking RewardBench2: the paper pools every candidate position, the released
    analysis script scores the chosen response only. One reading was computed (0.0135 vs 0.22), and
    the 09-30 fix told scripts to follow the code. Neither is presumed right: both run, same data
    and cohort, and the harness never picks the one that matches."""
    from harness.reconcile import READINGS_DIFFER
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        code_line = "ece = abs(p_chosen.mean() - 1.0)  # the chosen response only"
        _checkout(td, pid, {"analyze.py": f"import numpy\n{code_line}\n"})
        state.write_json(td / pid / "released.json", [{"path": "results/preds.csv"}])
        x = tasks._Ctx(cfg, pid)
        rd = {"id": "B", "kind": "RELEASED_DATA", "basis": "predictions", "claim_quote": "We report the mean over 5 random seeds",
              "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}, "role": "target", "criterion": "stated",
              "covers": ["accuracy"],
              "readings": [{"name": "paper", "source": "paper", "quote": "We use generation of samples"},
                           {"name": "code", "source": "analyze.py", "quote": code_line}]}
        central = [{"quote": "We report the mean over 5 random seeds", "checks": ["B"], "claim_type": "value",
                    "scope": ["accuracy"]}]
        rec = tasks._seal_plan(x, "plan", {"checks": [rd], "central_claims": central}, final=False)
        assert [r["name"] for r in rec["checks"][0]["readings"]] == ["paper", "code"]
        assert rec["checks"][0]["readings"][0]["page"] == 1 and rec["checks"][0]["basis"] == "predictions"
        bad_code = {**rd, "readings": [rd["readings"][0], {**rd["readings"][1], "quote": "ece = pooled_over_positions()"}]}
        assert "literal code" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [bad_code], "central_claims": central},
                                                                  final=False))
        assert "basis" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [{**rd, "basis": ""}],
                                                                        "central_claims": central}, final=False))
    check = {"kind": "RELEASED_DATA", "metric": "ece", "readings": [{"name": "paper"}, {"name": "code"}]}
    out = ('REFEREE_RESULT {"ece": 0.0135, "reading": "paper", "cohort": ["m1", "m2"]}\n'
           'REFEREE_RESULT {"ece": 0.214, "reading": "code", "cohort": ["m2", "m1"]}')
    staged = execute.staged_values(out, "", "ece")
    assert staged == [["", 0.0135, "paper"], ["", 0.214, "code"]] and execute.cohort_mismatch(out, check) == []
    assert execute.result_schema({"stdout": out}, check) == []
    moved = out.replace('["m2", "m1"]', '["m2", "m3"]')
    assert execute.cohort_mismatch(moved, check) == ["(no stage)"]
    assert any("different cohorts" in d for d in execute.result_schema({"stdout": moved}, check))
    assert any("no `cohort`" in d for d in execute.result_schema({"stdout": out.replace(', "cohort": ["m1", "m2"]', "")}, check))
    one = out.splitlines()[0]
    assert any("['code'] printed no result" in d for d in execute.result_schema({"stdout": one}, check))
    dec = lambda st, **kw: reconcile("RELEASED_DATA", "0.22", [e[1] for e in st], "", {}, False, True, "", staged=st,
                                     readings=["paper", "code"], deterministic=True, **kw)
    diff = dec(staged)
    assert diff["status"] == READINGS_DIFFER and set(diff["readings"]) == {"paper", "code"}
    assert diff["readings"]["paper"]["reproduced"] == 0.0135 and diff["readings"]["code"]["reproduced"] == 0.214
    same = dec([["", 0.22, "paper"], ["", 0.221, "code"]])
    assert same["status"] == "RESOLVED_VERIFIED" and "every reading" in same["reason"]
    assert dec(staged, cohort_mismatch=["(no stage)"])["status"] == "INCONCLUSIVE"
    assert dec(staged[:1])["status"] == "INCONCLUSIVE"                    # a reading that printed nothing
    c = {"id": "C1", "kind": "RELEASED_DATA", "evidence": "RELEASED_DATA_RECOMPUTATION", "deviations": [],
         "status": READINGS_DIFFER}
    assert report._claim_status([c]) == "READINGS_DISAGREE"
    assert report._claim_status([c, {**c, "id": "C2", "status": "RESOLVED_VERIFIED"}]) == "READINGS_DISAGREE"
    assert report._headline([c], [{"claim_status": "READINGS_DISAGREE"}]) == "CENTRAL_READINGS_DISAGREE"
    with tempfile.TemporaryDirectory() as t:        # a script owes both keys once readings exist
        td = Path(t)
        plan = {"checks": [{"id": "C1", "kind": "RELEASED_DATA", "metric": "ece", "readings": check["readings"],
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        state.write_json(td / "released.json", [{"path": "results/a.csv"}])
        x = type("X", (), {"root": td, "paper": Paper(PAGES, ROWS), "cfg": state.Config(), "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        script = 'rows = open("results/a.csv").read().split()\nece = len(rows)\nprint("REFEREE_RESULT", ece)\n'
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("dataset", 'open("results/a.csv")'), ("metric", "ece = len(rows)"), ("comparison_target", "print("))]
        g = {"fidelity": FID, "script": script, "runs": 1, "metric": "ece", "outputs": ["ece"], "bindings": binds}
        assert "`reading`" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", g, final=False))
        ok = {**g, "script": script.replace("ece)", "ece, 'reading', 'cohort')")}
        assert tasks._seal_gen(x, "gen:C1.1", ok, final=False)["metric"] == "ece"


def test_an_engineering_claim_gets_a_compatibility_test_never_a_benchmark():
    """Sep-30 GRACE C9: 'can serve as a flexible replacement for' FC layers was planned as a cheap
    swap-in, then generated as a 100-run, 12-setting superiority benchmark against a tuned
    baseline (22 min a run, 18.4 h projected) and blocked. A compatibility test is its own test,
    with its own budget, and speaks only for compatibility."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        x = tasks._Ctx(cfg, pid)
        compat = {"id": "B", "kind": "RECONSTRUCTION", "test": "compatibility", "role": "target", "criterion": "stated",
                  "covers": ["the layer"], "claim_quote": "We report the mean over 5 random seeds",
                  "target": {"quote": "Our method reaches 61.4", "relation": "loss_first - loss_last > 0"},
                  "define": {"loss_first": "training loss after epoch 1", "loss_last": "training loss after the last epoch"}}
        eng = [{"quote": "We report the mean over 5 random seeds", "claim_type": "engineering", "checks": ["B"],
                "scope": ["the layer"]}]
        rec = tasks._seal_plan(x, "plan", {"checks": [compat], "central_claims": eng}, final=False)
        assert rec["checks"][0]["test"] == "compatibility" and rec["central_claims"][0]["claim_type"] == "engineering"
        assert rec["checks"][0]["define"]["loss_first"].startswith("training loss")
        bench = {**compat, "test": "performance", "target": {"quote": "Our method reaches 61.4",
                                                            "relation": "err_base - err_ours > 0"}}
        assert "compatibility test" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [bench],
                                                                                    "central_claims": eng}, final=False))
        perf = [{**eng[0], "claim_type": "performance"}]
        assert "may speak only" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [compat], "central_claims": perf},
                                                                      final=False))
        assert "condition" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [{
            **compat, "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}], "central_claims": eng}, final=False))
    cfg = state.Config()
    g = {"runs": 100, "runs_quote": "report averages over 100 simulation runs", "stochastic": True}
    assert tasks.run_count(cfg, {"kind": "RECONSTRUCTION", "test": "compatibility"}, g) == (cfg.replicates, "compatibility")
    assert tasks.run_count(cfg, {"kind": "RECONSTRUCTION", "test": "performance"}, g) == (100, "paper")
    assert execute.budget(cfg, {"test": "compatibility"}) == (cfg.compat_budget_s, "SH_COMPAT_BUDGET_S")
    with tempfile.TemporaryDirectory() as t:
        cfg.projects = Path(t)
        state.write_json(Path(t) / ".gpu.json", False)
        pilot = {"pilot_s": 1320, "seed": 1, "width": 1}         # a 22-minute pilot, two more runs
        why = execute._over_budget(cfg, {"kind": "RECONSTRUCTION", "test": "compatibility"}, pilot, 3)
        assert why and why[0] == "time_budget" and "SH_COMPAT_BUDGET_S" in why[1]
        assert not execute._over_budget(cfg, {"kind": "RECONSTRUCTION"}, pilot, 3)
    rel = "loss_first - loss_last > 0"
    cond = lambda ms: reconcile("RECONSTRUCTION", "", ms, "", {}, True, True, "", rel, test="compatibility")
    held = cond([0.8, 0.8, 0.8])                                       # a condition: identical runs are fine
    assert held["status"] == "RELATION_HOLDS" and "no performance conclusion" in held["rule"] and held["met"] == 3
    assert cond([0.8, -0.1, 0.5])["status"] == "INCONCLUSIVE" and cond([-0.2, -0.1, -0.3])["status"] == "RELATION_VIOLATED"
    c = {"id": "C1", "kind": "RECONSTRUCTION", "evidence": "PAPER_DERIVED_IMPLEMENTATION", "deviations": [],
         "status": "RELATION_HOLDS", "test": "compatibility"}
    assert report._claim_status([c], claim_type="engineering") == "SUPPORT_FOUND"
    assert report._claim_status([c], claim_type="performance") != "SUPPORT_FOUND"   # compatibility is not performance


def test_identical_reruns_are_one_measurement_never_replicates():
    """Sep-30 changepoint C7: a deterministic statistic of one fixed dataset ran as 3 'replicates';
    220 stages had zero spread and were decided by a t-test with t*SE = 0. Identical reruns are one
    measurement; an audit of released files is not a recomputation, nor a fresh run."""
    rel = "var_lb - var_km > 0"
    st = lambda ms, det: reconcile("RECONSTRUCTION", "", ms, "", {}, True, True, "", rel, deterministic=det)
    seeded = st([0.044, 0.044, 0.044], False)
    assert seeded["status"] == "INCONCLUSIVE" and seeded["n_independent"] == 1 and "not 3 independent" in seeded["reason"]
    det = st([0.044, 0.044], True)
    assert det["status"] == "RELATION_HOLDS" and det["n_independent"] == 1 and "one measurement" in det["rule"]
    assert st([-0.044, -0.044], True)["status"] == "RELATION_VIOLATED"
    assert st([0.3, 0.31, 0.29], False)["status"] == "RELATION_HOLDS"          # real replicates still decide
    stages = reconcile("RECONSTRUCTION", "", [], "", {}, True, True, "", rel,
                       staged=[[s, 0.1] for s in ("ADD", "ARL") for _ in range(3)])
    assert stages["status"] == "INCONCLUSIVE" and all(p["n_independent"] == 1 for p in stages["stages"].values())
    pt = reconcile("RECONSTRUCTION", "0.52", [0.5] * 3, "", {}, True, True, "")
    assert pt["status"] == "INCONCLUSIVE" and pt["n_independent"] == 1 and "identical" in pt["rule"]   # never FAILED
    cfg = state.Config()
    assert tasks.run_count(cfg, {"kind": "RELEASED_DATA"}, {"runs": 5}) == (1, "deterministic")
    assert tasks.run_count(cfg, {"kind": "RECONSTRUCTION"}, {"runs": 1, "stochastic": False}) == (2, "deterministic")
    assert tasks.run_count(cfg, {"kind": "RECONSTRUCTION"}, {"runs": 1, "stochastic": True}) == (3, "referee_floor")
    assert st([0.044, 0.05], True)["status"] == "INCONCLUSIVE"                # declared deterministic, yet it varied
    assert execute.split_units({"a", "b"}, [["a", 1.0]], False, "x") == ({"b": "x"}, [])   # b was never measured
    assert execute.result_schema({"stdout": 'REFEREE_RESULT {"violated": true, "premises_hold": 1}'},
                                 {"kind": "CERTIFICATE"}) == []                   # a boolean flag is a flag
    with tempfile.TemporaryDirectory() as t:        # a reconstruction says whether its seed drives randomness
        plan = {"checks": [{"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        x = type("X", (), {"root": Path(t), "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        script = "acc = 61.0\nfit()\nload()\nprint('REFEREE_RESULT', acc)\n"
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "acc = 61.0"),
                  ("comparison_target", "print("))]
        g = {"fidelity": FID, "script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": binds}
        assert "`stochastic`" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", g, final=False))
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "stochastic": False}, final=False)["stochastic"] is False
    led = {"checks": [], "concerns": []}
    rows = report._check_rows([{"id": "C1", "kind": "RELEASED_DATA", "evidence": "RELEASED_DATA_RECOMPUTATION",
                                "basis": "published_results", "claim": "x", "target": None, "printed": "1", "step": "",
                                "statement": "", "values": [1.0], "status": "RESOLVED_VERIFIED", "deviations": [],
                                "state": "COMPLETED", "reason": "r", "rule": "deterministic run", "reason_by": "harness"}], led)
    assert "audit of released result files" in rows[-1]


def _serve(routes: dict):
    """A local HTTP server: {path: callable(handler) -> (status, headers, body)}. -> (server, "host:port")."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = routes.get(self.path.split("?")[0], lambda h: (404, {}, b"gone"))(self)
            self.send_response(status)
            for k, v in {**({"Content-Length": str(len(body))} if isinstance(body, bytes) else {}), **headers}.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in [body] if isinstance(body, bytes) else body:       # a generator streams, slowly if it likes
                self.wfile.write(chunk)
                self.wfile.flush()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.handle_error = lambda request, client_address: None      # a client that hangs up mid-body is part of these tests
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_port}"


def _zip(files: dict) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _fetch(fetcher, td: Path, sources: list, **kw) -> list[dict]:
    f = fetcher.Fetcher(str(td / "cache"), 1 << 30, kw.get("deny", []), sleep=lambda s: None, tries=3, timeout=5)
    return [fetcher.acquire(f, s, str(td / "data" / str(i)), str(td / "tmp"), str(i)) for i, s in enumerate(sources)]


def test_a_landing_page_is_followed_against_its_final_url_cached_or_not():
    """Sep-30 Porto C7: a DOI page redirected to the repository's host, and its relative ZIP link was
    resolved against doi.org (404). Links resolve against the FINAL url, from the cache too."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        hits = []
        repo, repo_host = _serve({
            "/dataset/339/taxi": lambda h: (200, {"Content-Type": "text/html"},
                                           b'<a href="/static/public/339/taxi.zip">zip</a><a href="/about">about</a>'),
            "/static/public/339/taxi.zip": lambda h: (hits.append(1) or 200, {"Content-Type": "application/zip"},
                                                      _zip({"train.csv": "a,b\n1,2\n"}))})
        doi, doi_host = _serve({"/10.24432/C55W25": lambda h: (302, {"Location": f"http://{repo_host}/dataset/339/taxi"}, b"")})
        src = {"source": f"http://{doi_host}/10.24432/C55W25", "include": ["*.zip"]}
        for round_ in range(2):                                      # the second round reads every response from the cache
            rec = _fetch(fetcher, td / f"r{round_}", [src])[0] if round_ == 0 else None
            if round_ == 1:
                f = fetcher.Fetcher(str(td / "r0" / "cache"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
                rec = fetcher.acquire(f, src, str(td / "again"), str(td / "tmp2"), "0")
                assert any(a.get("from_cache") for a in rec["attempts"] + rec["followed"])
            assert rec["failure_class"] == "" and [a["file"] for a in rec["admitted"]] == ["train.csv"], rec
            assert rec["landing"]["final_url"] == f"http://{repo_host}/dataset/339/taxi"
            assert rec["attempts"][0]["redirects"][0]["to"].endswith("/dataset/339/taxi")
        assert len(hits) == 1                                        # the repository served the ZIP once; the DOI host never did
        assert not any(f["url"].startswith(f"http://{doi_host}/static") for f in rec["followed"])
        for s in (repo, doi):
            s.shutdown()


def test_download_failures_are_classified_and_only_transient_ones_are_retried():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td, calls, slept = Path(t), {"flaky": 0, "gone": 0}, []

        def flaky(h):
            calls["flaky"] += 1
            return (503, {}, b"busy") if calls["flaky"] < 3 else (200, {}, b"a,b\n1,2\n")

        def gone(h):
            calls["gone"] += 1
            return 404, {}, b"no"
        srv, host = _serve({"/flaky.csv": flaky, "/gone.csv": gone, "/private.csv": lambda h: (403, {}, b"no")})
        f = fetcher.Fetcher(str(td / "cache"), 1 << 30, [], sleep=slept.append, tries=3, timeout=5)
        p, info = f.get(f"http://{host}/flaky.csv", str(td / "d"))
        assert calls["flaky"] == 3 and len(info["retries"]) == 2 and slept == [1, 2]      # 503 twice, then the file
        for path, klass in (("gone.csv", "missing"), ("private.csv", "inaccessible")):
            try:
                f.get(f"http://{host}/{path}", str(td / "d"))
                raise AssertionError("fetched")
            except fetcher.FetchError as e:
                assert e.klass == klass and len(e.attempts) == 1, (path, e.klass)          # never retried
        assert calls["gone"] == 1
        srv.shutdown()
        rec = _fetch(fetcher, td / "x", [{"source": f"http://{host}/flaky.csv"}])[0]     # nothing listens any more
        assert rec["failure_class"] == "transient" and len(rec["attempts"][-1]["tries"]) == 3
        assert fetcher.classify(KeyError("x"))[0] == "bug"                                 # our own error is no missing data
        assert fetcher.failure_class({"attempts": [{"class": "bug"}, {"class": "missing"}], "followed": [], "rejected": []}) == "bug"


def test_a_file_the_record_lists_but_the_plan_did_not_request_is_never_called_unreleased():
    """Oct-01 transformer: Zenodo 18281512 lists 25 files (discovery.jsonl F1); the plan requested 20; C2's identity lines
    then called flash_vanaddition and the algorithmic-addition files "not among the files released". What a COMPLETE
    listing names and the plan did not take is reported as not requested (never a gap: a plan may take 3 of 25)."""
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        state.append_jsonl(root / "discovery.jsonl", {"id": "F1", "files_of": "https://zenodo.org/records/7", "files": [
            {"name": n, "bytes": 9, "md5": ""} for n in ("a.csv", "b.csv", "c.csv", "README.md", "run.py")]})
        data = {"sources": [{"source": "https://zenodo.org/records/7", "dir": "0", "admitted_files": 1,
                             "admitted": [{"file": "a.csv", "from": "a.csv"}]},
                            {"source": "https://x.org/page", "dir": "1", "admitted_files": 1,
                             "admitted": [{"file": "z.csv"}]}]}                                    # a landing page: no listing
        got = execute.unrequested(root, data)
        assert got == {"0": {"source": "https://zenodo.org/records/7", "not_requested": ["b.csv", "c.csv"], "listed": 3}}, got
        assert not execute.data_gaps(data)                                   # not requested is no gap
        hub = {"sources": [{"source": "hf://datasets/o/n", "dir": "0", "listing": ["data/train.parquet", "data/test.parquet"],
                            "admitted": [{"file": "data/test.parquet", "from": "hf://datasets/o/n"}]}]}
        assert execute.unrequested(root, hub)["0"]["not_requested"] == ["data/train.parquet"]   # the fetcher's own listing
        lines = report._unrequested_lines({"id": "C2", "not_requested": got,
                                           "data_identity": {"b": {"source": "/work/data/0/b.csv", "observed": {"file_present": False},
                                                                   "matches": False}}})
        assert "this plan did not request: b.csv, c.csv" in lines[0] and "not absent from the release" in lines[0]
        assert "b.csv" in lines[1] and "the record lists this file" in lines[1]


def test_a_hub_repository_is_listed_before_include_is_chosen_and_a_denied_one_never_asked():
    """Oct-01 PPRM: `discover --files` listed only records-API sources, so the planner guessed CMExam's format
    (`*.jsonl`, `*.csv`, `*.parquet`) and three patterns that name nothing read as data gaps. A hub repository is
    listed from the hub's own API (what the fetcher would take: no dot-paths, no empty files); a denied one is refused
    before anything is sent."""
    from harness import discover
    sent = []

    def get(url):
        sent.append(url)
        return {"id": "fzkuji/CMExam", "gated": False, "private": False, "siblings": [
            {"rfilename": "test.json", "size": 6434215}, {"rfilename": "train.json", "size": 48807760},
            {"rfilename": ".gitattributes", "size": 2}, {"rfilename": "empty.json", "size": 0}, {"rfilename": "README.md", "size": 9}]}
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        cfg.allow_network = cfg.allow_data_search = True
        rec = discover.files(cfg, pid, "hf://datasets/fzkuji/CMExam", get=get)
        assert sent == ["https://huggingface.co/api/datasets/fzkuji/CMExam?blobs=true"], sent
        assert [f["name"] for f in rec["files"]] == ["test.json", "train.json", "README.md"] and rec["total_bytes"] == 6434215 + 48807760 + 9
        assert discover.files(cfg, pid, "hf://datasets/fzkuji/CMExam", get=get).get("reused")      # answered from the log
        sent.clear()
        bad = discover.files(cfg, pid, "hf://datasets/ICML-2026-agent-repro/verdicts", get=get)
        assert "denied" in bad["error"] and sent == []                                         # nothing was sent
        assert "hf://" in discover.files(cfg, pid, "hf://spaces/a/b", get=get)["error"] and sent == []
        def down(url):
            raise OSError("HTTP Error 401: Unauthorized")
        failed = discover.files(cfg, pid, "hf://models/o/gated-model", get=down)
        assert failed["error"] and not failed["files"]
        assert discover._spent(discover.records(cfg, pid)) == 1                               # a failed listing costs nothing


def test_a_named_file_that_arrives_packed_is_acquired_and_says_so():
    """Oct-01 conformal C5: the UCI Porto archive holds `train.csv.zip`; the plan named `train.csv` (the paper's name).
    Every file arrived, yet `train.csv` read as matching nothing and the check ended a DATA BLOCKER (missing). A named
    file admitted in its packed form (one .zip/.gz/.bz2/.xz around that name) is acquired, recorded as packed; a name no
    file carries, packed or not, stays unmatched."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        porto = _zip({"train.csv.zip": _zip({"train.csv": "TRIP_ID,POLYLINE\n1,[]\n"}), "solution.csv": "x\n1\n"})
        srv, host = _serve({"/ds": lambda h: (200, {"Content-Type": "text/html"}, b'<a href="/static/porto.zip">zip</a>'),
                            "/static/porto.zip": lambda h: (200, {"Content-Type": "application/zip"}, porto)})
        try:
            rec = _fetch(fetcher, td, [{"source": f"http://{host}/ds", "include": ["*.zip", "train.csv"]}])[0]
            assert rec["failure_class"] == "" and rec["unmatched_include"] == [], rec
            assert rec["packed"] == [{"include": "train.csv", "file": "train.csv.zip"}], rec
            man = fetcher.manifest(str(td / "data"), [rec])
            assert man["status"] == "ok" and not execute.data_gaps(man)
            miss = _fetch(fetcher, td / "b", [{"source": f"http://{host}/ds", "include": ["*.zip", "test.csv"]}])[0]
            assert miss["unmatched_include"] == ["test.csv"] and not miss.get("packed")      # still a gap
            assert fetcher.manifest(str(td / "b" / "data"), [miss])["status"] == "partial"
        finally:
            srv.shutdown()


def test_a_cut_download_resumes_from_the_bytes_it_holds():
    """Oct-01 PPRM C1: CIFAR-10-C.tar (2.9 GB) was cut at 285 MB by a link drop at 08:14Z and every retry began again at
    byte 0. A cut transfer continues from what it holds (Range, If-Range on the first response's validator); a server
    that ignores the range, or answers another one, starts over; a cut that repeats still fails, leaving nothing."""
    import hashlib as _h
    from harness import fetcher
    body = bytes(range(256)) * 1200                                   # 307200 bytes
    md5 = _h.md5(body).hexdigest()
    seen: dict = {}

    def range_of(h):
        r = h.headers.get("Range") or ""
        return int(r[6:-1]) if r.startswith("bytes=") and r.endswith("-") else None

    def make(mode):
        def route(h):
            seen.setdefault(mode, []).append((h.headers.get("Range"), h.headers.get("If-Range")))
            start, n = range_of(h), len(seen[mode])
            val = {} if mode == "novalidator" else {"ETag": '"v1"'}
            if n == 1 or mode == "alwayscut":                         # the first answer (every answer): cut at 100 KB
                return 200, {"Content-Length": str(len(body)), **val}, iter([body[:102400]])
            if start is not None and mode in ("resume", "redirect") and h.headers.get("If-Range") == '"v1"':
                return 206, {"Content-Length": str(len(body) - start), **val,
                             "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}, body[start:]
            if mode == "wrongrange" and n == 2:                       # a range other than the one asked for
                return 206, {"Content-Length": str(len(body)), **val, "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}"}, body
            return 200, {**val}, body                                 # ignores the range: the whole file again
        return route
    routes = {f"/{m}.tar": make(m) for m in ("resume", "ignored", "wrongrange", "novalidator", "alwayscut", "redirect")}
    srv, host = _serve({**routes, "/hop": lambda h: (302, {"Location": f"http://{host}/redirect.tar"}, b"")})
    try:
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            f = fetcher.Fetcher(str(td / "cache"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
            for mode, url in [(m, f"http://{host}/{m}.tar") for m in ("resume", "ignored", "wrongrange", "novalidator")] + [
                    ("redirect", f"http://{host}/hop")]:
                p, info = f.get(url, str(td / mode), md5=md5)
                assert Path(p).read_bytes() == body, (mode, len(Path(p).read_bytes()))
                assert info["retries"][0]["class"] == "short", (mode, info)
            assert seen["resume"][1] == ("bytes=102400-", '"v1"') and seen["redirect"][1] == ("bytes=102400-", '"v1"')
            assert seen["novalidator"][1] == (None, None)              # nothing to tie the bytes to: never resumed
            try:
                f.get(f"http://{host}/alwayscut.tar", str(td / "cut"), md5=md5)
                raise AssertionError("a cut on every try was admitted")
            except fetcher.FetchError as e:
                assert e.klass == "transient" and len(e.attempts) == 3, (e.klass, e.attempts)
            assert len(seen["alwayscut"]) == 3 and seen["alwayscut"][1][0] == "bytes=102400-"
            assert not list((td / "cache").rglob("*.part.*"))         # no partial file outlives the fetch
        if "SH_FETCH_TIMEOUT_S" not in os.environ:                     # a download has its own limit: the data cap at 1 MB/s
            assert state.Config().fetch_timeout_s == state.Config().max_data_gb * 1024
    finally:
        srv.shutdown()


def test_downloaded_content_is_validated_before_it_reaches_an_experiment():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        html = b"<!DOCTYPE html><html><body>Please log in</body></html>"
        page = "".join(f'<a href="/{n}">{n}</a>' for n in ("login.csv", "empty.csv", "bad.zip", "evil.zip", "mixed.zip", "ok.csv"))
        srv, host = _serve({
            "/": lambda h: (200, {"Content-Type": "text/html"}, page.encode()),
            "/login.csv": lambda h: (200, {"Content-Type": "text/csv"}, html),
            "/empty.csv": lambda h: (200, {}, b""),
            "/bad.zip": lambda h: (200, {}, _zip({"a.csv": "1"})[:-30]),
            "/evil.zip": lambda h: (200, {}, _zip({"../escape.csv": "1"})),
            "/mixed.zip": lambda h: (200, {}, _zip({"data/x.csv": "1,2\n", "run.py": "print(1)"})),
            "/ok.csv": lambda h: (200, {}, b"a,b\n1,2\n")})
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/", "include": ["*.csv", "*.zip"]}])[0]
        assert sorted(a["file"] for a in rec["admitted"]) == ["data/x.csv", "ok.csv"], rec["admitted"]
        why = {r["file"]: r["class"] for r in rec["rejected"]}
        assert why == {"login.csv": "content_invalid", "empty.csv": "content_invalid", "bad.zip": "content_invalid",
                       "evil.zip": "content_invalid", "run.py": "code_excluded"}, why
        assert not (td / "data" / "0" / "run.py").exists() and (td / "data" / "0" / "data" / "x.csv").exists()
        only = _fetch(fetcher, td / "y", [{"source": f"http://{host}/", "include": ["login.csv"]}])[0]
        assert only["admitted"] == [] and only["failure_class"] == "content_invalid"       # an HTML page is not data
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for i in range(3):
                ti = tarfile.TarInfo(f"d/f{i}.npy")
                ti.size = 100000
                tf.addfile(ti, io.BytesIO(b"x" * 100000))
        (td / "cut.tar").write_bytes(buf.getvalue()[:150000])
        ok, bad = fetcher.admit(str(td / "cut.tar"), "cut.tar", str(td / "atomic"))
        assert ok == [] and bad and not [p for p in (td / "atomic").rglob("*") if p.is_file()]   # a cut archive leaves nothing behind
        srv.shutdown()


def test_recovery_uses_documented_mechanisms_and_the_printed_alternatives():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td, csv = Path(t), b"a,b\n1,2\n"
        import hashlib
        md5 = hashlib.md5(csv).hexdigest()
        listing = lambda host: json.dumps({"files": [
            {"key": "a.csv", "size": len(csv), "checksum": f"md5:{md5}", "links": {"self": f"http://{host}/files/a.csv"}},
            {"key": "b.csv", "size": len(csv), "checksum": "md5:" + "0" * 32, "links": {"self": f"http://{host}/files/b.csv"}},
            {"key": "big.tar", "size": 1, "checksum": "", "links": {"self": f"http://{host}/files/big.tar"}}]}).encode()
        box = {}
        srv, host = _serve({"/records/7": lambda h: (200, {"Content-Type": "text/html"}, b"<html>no file links here</html>"),
                            "/api/records/7": lambda h: (200, {}, listing(box["host"])),
                            "/files/a.csv": lambda h: (200, {}, csv), "/files/b.csv": lambda h: (200, {}, csv)})
        box["host"] = host
        fetcher.RECORD_APIS[host] = f"http://{host}/api/records/{{id}}"
        try:
            rec = _fetch(fetcher, td, [{"source": f"http://{host}/records/7", "include": ["a.csv", "b.csv"]}])[0]
            assert [a["file"] for a in rec["admitted"]] == ["a.csv"] and any("records API" in r for r in rec["recovery"])
            assert rec["listing"] == ["a.csv", "b.csv", "big.tar"]                     # the record's complete listing, kept
            assert execute.unrequested(td, {"sources": [rec]})["0"]["not_requested"] == ["b.csv", "big.tar"]
            bad = [x for x in rec["followed"] if x.get("class") == "content_invalid"]      # its checksum disagrees, at every transfer
            assert len(bad) == 1 and "md5" in bad[0]["error"] and len(bad[0]["tries"]) == 3 and not rec["rejected"]
            # the first citation reading is unreachable; the second printed reading is where the record is
            alt = _fetch(fetcher, td / "z", [{"source": "http://127.0.0.1:1/records/7", "include": ["a.csv"],
                                              "alternates": [f"http://{host}/records/7"]}])[0]
            assert [a["file"] for a in alt["admitted"]] == ["a.csv"] and any("another reading" in r for r in alt["recovery"])
            none = _fetch(fetcher, td / "w", [{"source": f"http://{host}/records/7", "include": ["nothing*"]}])[0]
            assert none["failure_class"] == "no_data" and none["admitted"] == []
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()


DATA_PAGES = [
    "The data are public at zen-\nodo.org/records/18281512. The error bars use 95% CIs.\n"
    "Method A beats method B on CIFAR-10-C with ResNet-32 by a wide margin.\n"
    "Our LLM monitor reaches 61.4 accuracy on MMLU in the paper.\n"
    "Theorem 1 states that the bound holds for every n.\nMethod C uses GPT-4.1 as its labeler.",
    "See https://proceedings.\nneurips.cc/paper/2020/hash/\n1457c0d6-\nAbstract.html and doi 10.24432/C55W25.\n"
    "The files are at https://data.example.org/set.zip for all."]


def _project_pages(td: Path, pages: list[str]) -> tuple[state.Config, str]:
    cfg, pid = _project(td)
    state.write_json(td / pid / "paper" / "doc.json", {"pid": pid, "title": "T", "sha256": "0", "pages": pages,
                                                       "rows": [[] for _ in pages], "arxiv_id": "", "arxiv_version": "", "source": ""})
    (td / pid / "paper" / "paper.md").write_text("\n".join(pages), encoding="utf-8")
    return cfg, pid


def _registry(url: str):
    """What the three registries would answer (a stand-in for the network)."""
    if "zenodo.org" in url:
        return {"hits": {"hits": [
            {"id": 2535967, "doi": "10.5281/zenodo.2535967", "files": [{"key": "CIFAR-10-C.tar", "size": 2918471680}],
             "metadata": {"title": "CIFAR-10-C and CIFAR-10-P", "creators": [{"name": "Hendrycks, Daniel"}],
                          "publication_date": "2019-01-25", "resource_type": {"type": "dataset"}, "license": {"id": "cc-by-4.0"},
                          "description": "<p>Corruptions</p>"}},
            {"id": 7, "doi": "10.5281/zenodo.7", "files": [], "metadata": {"title": "some code", "resource_type": {"type": "software"}}},
            {"id": 8, "doi": "10.5281/zenodo.8", "files": [], "metadata": {"title": "ICML-2026-agent-repro verdicts",
                                                                          "resource_type": {"type": "dataset"}}}]}}
    if "datacite" in url:
        return {"data": [{"attributes": {"doi": "10.24432/c55w25", "titles": [{"title": "Taxi Service Trajectory"}],
                                         "creators": [{"name": "Moreira-Matias"}], "publicationYear": 2015,
                                         "publisher": "UCI", "url": "https://archive.ics.uci.edu/dataset/339"}},
                         {"attributes": {"doi": "10.48550/arxiv.1", "titles": [{"title": "a paper"}]}}]}
    return [{"id": "someone/cifar-10-c", "downloads": 3, "author": "someone", "tags": ["image"]}]


def test_a_cited_url_is_matched_as_printed_across_line_breaks_and_identifiers():
    """Sep-30 transformer errors: the paper prints `zen-⏎odo.org/records/18281512`; the planner's normalized
    address was refused as 'not cited verbatim', so the accuracy claim was never tested."""
    p = Paper(DATA_PAGES)
    hit = p.cites("https://zenodo.org/records/18281512")
    assert hit and hit["span"] == "zen-\nodo.org/records/18281512" and hit["form"] == "line-break hyphen dropped" and hit["page"] == 1
    assert hit["variants"] == ["https://zen-odo.org/records/18281512"]          # the other printed reading is kept, not lost
    assert p.cites("https://doi.org/10.5281/zenodo.18281512")                    # the same record by its DOI
    assert p.cites("https://doi.org/10.24432/C55W25")                            # a bare DOI cites its landing page
    for wrong in ("https://zenodo.org/records/1828151", "https://zenodo.org/records/182815123",
                  "https://example.org/records/18281512", "https://ww.zenodo.org/records/18281512"):
        assert p.cites(wrong) is None, wrong                                     # a prefix or another host is not the citation
    hard = p.cites("https://proceedings.neurips.cc/paper/2020/hash/1457c0d6-Abstract.html")
    assert hard and hard["form"] == "as printed" and "https://proceedings.neurips.cc/paper/2020/hash/1457c0d6Abstract.html" in hard["variants"]
    assert p.cites("http://data.example.org/set.zip")                            # the scheme is not the citation


def test_acquire_takes_a_printed_citation_or_a_record_the_harness_itself_discovered():
    from harness import discover
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        x = tasks._Ctx(cfg, pid)
        errs: list = []
        out = tasks._acquire(x, {"acquire": [{"source": "https://zenodo.org/records/18281512", "cited_in": "paper",
                                              "include": ["*.csv"]}]}, errs, "C1")
        assert not errs and out[0]["cited_as"] == "zen-\nodo.org/records/18281512" and out[0]["alternates"]   # the citation is kept
        assert out[0]["required"] is True
        rec = discover.search(cfg, pid, "CIFAR-10-C", get=_registry)
        got = rec["results"]
        assert [c["source"] for c in got["zenodo"]["candidates"]] == ["https://zenodo.org/records/2535967"]   # software and denied dropped
        assert got["zenodo"]["denied"] == 1 and got["datacite"]["candidates"][0]["source"] == "https://doi.org/10.24432/c55w25"
        assert got["huggingface"]["candidates"][0]["source"] == "hf://datasets/someone/cifar-10-c"
        def ok(src, named="CIFAR-10-C", did=rec["id"]):
            e: list = []
            r = tasks._acquire(x, {"acquire": [{"source": src, "cited_in": "discovery", "discovery": did, "named_in_paper": named,
                                                "include": ["CIFAR-10-C.tar"]}]}, e, "C1")
            return r[0] if r and not e else None
        assert ok("https://zenodo.org/records/2535967")["found"]["title"].startswith("CIFAR-10-C")
        assert ok("https://doi.org/10.5281/zenodo.2535967")                        # the record by its DOI is the same record
        assert ok("hf://datasets/someone/cifar-10-c")
        assert not ok("https://zenodo.org/records/999999")                        # nothing the search returned
        assert not ok("https://zenodo.org/records/2535967", did="D9")             # a search that never happened
        assert not ok("https://zenodo.org/records/2535967", named="ImageNet-9000")   # the paper does not name that dataset
        assert not ok("https://zenodo.org/records/8")                             # denied records are never returned
        assert len(discover.records(cfg, pid)) == 1


def test_data_search_is_its_own_gate_and_grants_no_code():
    """Sep-30: SH_ALLOW_SOURCE_SEARCH (the authors' repository) was the only 'search', so a run without author
    code could not find any dataset. Discovery is a separate permission; it clones nothing and runs nothing."""
    import os
    from harness import discover, repo
    keys = ("SH_ALLOW_DATA_SEARCH", "SH_ALLOW_SOURCE_SEARCH", "SH_ALLOW_REPO_EXEC", "SH_ALLOW_NETWORK", "SH_ALLOW_INSTALL")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        cfg = state.Config()
        assert cfg.allow_data_search and not cfg.allow_source_search and not cfg.allow_repo_exec   # defaults: data yes, code no
        os.environ.update({"SH_ALLOW_SOURCE_SEARCH": "0", "SH_ALLOW_REPO_EXEC": "0", "SH_ALLOW_DATA_SEARCH": "1"})
        cfg = state.Config()
        with tempfile.TemporaryDirectory() as t:
            cfg.projects = Path(t)
            assert "error" not in discover.search(cfg, "p", "CIFAR-10-C", get=_registry)       # no author code, yet data is found
            assert repo.search(cfg, "A Title Long Enough To Search For")[0] == ""               # the authors' repo is still not searched
            os.environ.update({"SH_ALLOW_DATA_SEARCH": "0", "SH_ALLOW_SOURCE_SEARCH": "1", "SH_ALLOW_REPO_EXEC": "1"})
            cfg = state.Config()
            cfg.projects = Path(t)
            sent = []
            res = discover.search(cfg, "p2", "CIFAR-10-C", get=lambda u: sent.append(u))
            assert "off" in res["error"] and not sent                                            # code permissions grant no data search
            os.environ.update({"SH_ALLOW_DATA_SEARCH": "1", "SH_ALLOW_NETWORK": "0"})
            cfg = state.Config()
            cfg.projects = Path(t)
            assert "off" in discover.search(cfg, "p3", "CIFAR-10-C", get=lambda u: sent.append(u))["error"] and not sent
            os.environ.update({"SH_ALLOW_NETWORK": "1", "SH_MAX_DISCOVERIES": "1"})
            cfg = state.Config()
            cfg.projects = Path(t)
            assert "error" not in discover.search(cfg, "p4", "a", get=_registry)
            assert "budget" in discover.search(cfg, "p4", "b", get=_registry)["error"]           # bounded, not a loop
        # acquiring cited data needs the network gate alone: no execution gate, no install (a hub client aside)
        os.environ.update({"SH_ALLOW_NETWORK": "1", "SH_ALLOW_INSTALL": "0", "SH_ALLOW_REPO_EXEC": "0"})
        cfg = state.Config()
        with tempfile.TemporaryDirectory() as t:
            web, hub = [{"source": "https://x.org/a.csv"}], [{"source": "hf://datasets/o/n"}]
            real = execute.docker_status
            execute.docker_status = lambda: (False, "no docker")
            try:
                assert execute.fetch(cfg, Path(t), "C1", web) is None                            # not refused: it waits for Docker
                shut = execute.fetch(cfg, Path(t), "C2", hub)
                assert shut and shut["sources"][0]["failure_class"] == "gate" and "SH_ALLOW_INSTALL" in shut["sources"][0]["error"]
                os.environ["SH_ALLOW_NETWORK"] = "0"
                shut = execute.fetch(state.Config(), Path(t), "C3", web)
                assert shut and shut["sources"][0]["failure_class"] == "gate" and shut["n_files"] == 0
            finally:
                execute.docker_status = real
    finally:
        for k in keys + ("SH_MAX_DISCOVERIES",):
            os.environ.pop(k, None)
            if saved.get(k) is not None:
                os.environ[k] = saved[k]


def test_equal_summary_values_from_different_runs_are_replicates_not_duplicates():
    """Sep-30 conformal C5: `dev` was 0 in every seed while test coverage was 0.78, 0.83, 0.84, and the harness read
    'equal margins' as 'the seed did not vary the run'. PPRM C2/C8: 0 false alarms in 400 trials of every seed."""
    rel = "dev <= 0"

    def run(detail, staged, rng=None, det=False, r=rel):
        return reconcile("RECONSTRUCTION", "", [v for _, v in staged], "", {}, True, True, "", r, staged=staged,
                         detail=detail, rng=rng, deterministic=det)

    def rows(outs, seeds=None, **kw):
        return [{"seed": s, "stage": "a", "reading": "", "out": o, "trials": kw.get("trials", {}), "data_fp": kw.get("fp", {}).get(s)}
                for s, o in zip(seeds or range(len(outs)), outs)]
    staged = lambda m, n: [["a", m] for _ in range(n)]
    diff = rows([{"dev": 0.0, "test_coverage": c} for c in (0.78, 0.83, 0.84)])
    r = run(diff, staged(0.0, 3))
    assert r["status"] == "NO_VIOLATION_FOUND" and r["stages"]["a"]["n_independent"] == 3 and "0 of 3" in r["stages"]["a"]["reason"]
    from harness import independence
    assert independence.assess(diff, "a", "", None)["state"] == "different"
    # the same numbers with nothing to show the runs differed are still one measurement (the old, correct rule)
    same = rows([{"dev": 0.0, "test_coverage": 0.8}] * 3)
    assert run(same, staged(0.0, 3), rng=False)["stages"]["a"]["n_independent"] == 1
    assert run(same, staged(0.0, 3), rng=False)["status"] == "INCONCLUSIVE"
    # 2026-10-01: a seed that reaches a generator somewhere in the script proves nothing about THIS stage (a stage on
    # default_rng(0) inside a seeded script repeated itself six times and passed a sign test): identical lines with
    # no per-line fingerprint stay one measurement
    assert run(same, staged(0.0, 3), rng=True)["status"] == "INCONCLUSIVE"
    # a declared data fingerprint decides both ways
    fps = {0: "a", 1: "b", 2: "c"}
    assert run(rows([{"dev": 0.0}] * 3, fp=fps), staged(0.0, 3), rng=False)["status"] == "NO_VIOLATION_FOUND"
    assert run(rows([{"dev": 0.0}] * 3, fp={0: "a", 1: "a", 2: "a"}), staged(0.0, 3), rng=True)["status"] == "INCONCLUSIVE"
    # a time-like output differs between two runs of the same experiment: it is no evidence of a difference
    timed = rows([{"dev": 0.0, "seconds": 1.2}, {"dev": 0.0, "seconds": 3.4}, {"dev": 0.0, "seconds": 2.2}])
    assert run(timed, staged(0.0, 3), rng=False)["status"] == "INCONCLUSIVE"
    # zero variance is not certainty: a strict margin repeated by independent replicates needs an exact sign test
    strict = "gain > 0"
    three = rows([{"gain": 0.2, "n": i} for i in range(3)])
    r3 = run(three, staged(0.2, 3), r=strict)
    assert r3["status"] == "INCONCLUSIVE" and "sign test needs 6" in r3["stages"]["a"]["reason"]
    six = rows([{"gain": 0.2, "n": i} for i in range(6)])
    assert run(six, staged(0.2, 6), r=strict)["status"] == "RELATION_HOLDS"
    assert run(six, staged(-0.2, 6), r=strict)["status"] == "RELATION_VIOLATED"
    # a declared deterministic pipeline whose runs differ in other outputs is not deterministic
    assert run(diff, staged(0.0, 3), det=True)["status"] == "INCONCLUSIVE"
    # a proportion of counted trials: zero events are decided by the exact interval, not by a standard error of zero
    fa = "fa < delta"
    zero = rows([{"fa": 0.0, "delta": 0.25} for _ in range(3)], trials={"fa": 400})
    z = run(zero, staged(0.25, 3), rng=True, r=fa)["stages"]["a"]
    # identical lines, no fingerprint: one run's 400 trials, never 1200 pooled
    assert z["status"] == "RELATION_HOLDS" and z["events"] == 0 and z["trials"] == 400 and z["ci"][1] < 0.0092
    bad = rows([{"fa": 0.9, "delta": 0.25} for _ in range(3)], trials={"fa": 400})
    assert run(bad, staged(-0.65, 3), rng=True, r=fa)["stages"]["a"]["status"] == "RELATION_VIOLATED"
    edge = rows([{"fa": 0.25, "delta": 0.25} for _ in range(3)], trials={"fa": 400})
    assert run(edge, staged(0.0, 3), rng=True, r=fa)["stages"]["a"]["status"] == "INCONCLUSIVE"     # straddles the boundary
    lone = rows([{"fa": 0.0, "delta": 0.25}] * 3, trials={"fa": 400})
    one = run(lone, staged(0.25, 3), rng=False, r=fa)["stages"]["a"]                                  # one run repeated: 400 trials, not 1200
    assert one["trials"] == 400 and one["n_independent"] == 1
    from harness.reconcile import clopper_pearson
    lo, hi = clopper_pearson(5, 20)
    assert abs(lo - 0.0866) < 5e-4 and abs(hi - 0.4910) < 5e-4 and clopper_pearson(0, 400)[1] < 0.0092
    # Student-t still decides real replicates whose margins vary, and the old one-measurement rule stands without detail
    assert reconcile("RECONSTRUCTION", "", [0.3, 0.31, 0.29], "", {}, True, True, "", "gain > 0")["status"] == "RELATION_HOLDS"
    assert reconcile("RECONSTRUCTION", "", [0.2] * 3, "", {}, True, True, "", "gain > 0")["status"] == "INCONCLUSIVE"


def test_the_seed_must_reach_a_random_generator_for_a_stochastic_script():
    from harness import independence
    flows = independence.seed_flow
    assert flows("import numpy as np\nrng = np.random.default_rng([args.seed, k])\n") is True
    assert flows("import argparse, numpy as np\nap = argparse.ArgumentParser(); ap.add_argument('--seed')\n"
                 "s = ap.parse_args().seed\nrng = np.random.RandomState(s + 1)\n") is True
    assert flows("def make(sd):\n    return np.random.default_rng(sd)\nfor k in range(3):\n    g = make(args.seed * 10 + k)\n") is True
    assert flows("import random\nrandom.seed(args.seed)\n") is True
    assert flows("from sklearn.model_selection import train_test_split\ntrain_test_split(x, random_state=args.seed)\n") is True
    assert flows("rng = np.random.default_rng(1234)\nprint(args.seed)\n") is False        # reads the seed, never uses it
    assert flows("import numpy as np\nrng = np.random.default_rng()\n") is False
    assert flows("rng = np.random.default_rng(0)\nx = rng.random(args.seed)\n") is False      # a size is not a seed
    assert flows("def broken(:\n") is None
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        plan = {"checks": [{"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        x = type("X", (), {"root": Path(t), "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "acc = 61.0"),
                  ("comparison_target", "print("))]
        g = {"fidelity": FID, "runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": binds, "stochastic": True}
        ignores = ("acc = 61.0\nfit()\nload()\nrng = np.random.default_rng(7)\n"
                   "print('REFEREE_RESULT', {'acc': acc, 'data_fingerprint': fp})\n")
        assert "never reaches a random generator" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "script": ignores}, final=False))
        seeded = ignores.replace("default_rng(7)", "default_rng(args.seed)")
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "script": seeded}, final=False)["seed_flow"] is True
        # 2026-10-01: a stochastic script that prints no per-line fingerprint cannot show its replicates differ
        assert "data_fingerprint" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {
            **g, "script": seeded.replace(", 'data_fingerprint': fp", "")}, final=False))
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "script": ignores, "stochastic": False}, final=False)["seed_flow"] is False


def test_a_check_whose_data_was_not_acquired_ends_as_a_blocker_and_no_surrogate_is_written():
    """Sep-30 Porto C7: the 'acquired' file was an HTML landing page; the script author wrote a synthetic surrogate."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        claim = "Method A beats method B on CIFAR-10-C with ResNet-32 by a wide margin"
        chk = {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": claim, "target": {"quote": claim, "relation": "acc_a > acc_b"},
               "covers": ["CIFAR-10-C"], "acquire": [{"source": "https://data.example.org/set.zip", "cited_in": "paper"}],
               "role": "target", "criterion": "stated", "test": "performance"}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [{"quote": claim, "claim_type": "performance", "checks": ["R"],
                                                                     "scope": ["CIFAR-10-C"]}]}, td)
        x = tasks._Ctx(cfg, pid)
        c = x.plan()["checks"][0]
        cdir = td / pid / "checks" / "C1"
        man = lambda **kw: {"sources": [{"source": "https://data.example.org/set.zip", "dir": "0", "admitted_files": 0, "attempts": [],
                                         "rejected": [{"file": "set.zip", "why": "an HTML page, not the data it was named for"}], **kw}],
                            "files": [], "n_files": 0, "fetched_at": "now"}
        state.write_json(cdir / "data.json", man(failure_class="content_invalid"))
        assert tasks._step(x, c) == []                                   # no gen task: no script is written against nothing
        o = state.read_json(cdir / "outcome.json")
        assert o["status"] == "BLOCKED" and o["reason"].startswith("DATA BLOCKER (content_invalid)") and o["data_blocker"][0]["rejected"]
        assert report._state(td / pid, "C1", o) == "DATA_BLOCKED"
        (cdir / "outcome.json").unlink()
        state.write_json(cdir / "data.json", man(failure_class="transient"))     # a network fault is this run, not the source
        tasks._step(x, c)
        assert state.read_json(cdir / "outcome.json")["status"] == "INCONCLUSIVE"
        (cdir / "outcome.json").unlink()
        d = man(failure_class="")
        d["sources"][0]["admitted_files"], d["n_files"] = 1, 1
        state.write_json(cdir / "data.json", d)
        assert execute.data_blocker(c["acquire"], d) == []               # admitted data: the check goes on to its script
        # a script that never reads the acquired files is refused (a simulation would be another experiment)
        plan = {"checks": [c]}
        x2 = type("X", (), {"root": td / pid, "paper": Paper(DATA_PAGES, [[] for _ in DATA_PAGES]), "cfg": cfg,
                            "sealed": lambda self, tid: plan, "plan": lambda self: plan})()
        binds = [{"kind": k, "impl_quote": q, "paper_quote": claim} for k, q in
                 (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "acc_a = 1"),
                  ("comparison_target", "print("))]
        g = {"fidelity": FID, "runs": 1, "outputs": ["acc_a", "acc_b"], "bindings": binds, "stochastic": True}
        sim = ("acc_a = 1\nfit()\nload()\nrng = np.random.default_rng(args.seed)\n"
               "print('REFEREE_RESULT', {'acc_a': acc_a, 'data_fingerprint': fp})\n")
        assert "never reads /work/data" in _refused(lambda: tasks._seal_gen(x2, "gen:C1.1", {**g, "script": sim}, final=False))
        assert tasks._seal_gen(x2, "gen:C1.1", {**g, "script": sim + "open('/work/data/0/train.csv')\n"}, final=False)["seed_flow"]


def test_an_empirical_claim_keeps_its_requested_experiment():
    """Sep-30: CIFAR-10-C, the Porto evaluation and the accuracy fits were 'omitted' with any sentence, and a
    lemma check or a simulation was reported beside a headline as if the paper had been reproduced."""
    from harness import discover
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        x = tasks._Ctx(cfg, pid)
        claim = "Method A beats method B on CIFAR-10-C with ResNet-32 by a wide margin"
        thm = "Theorem 1 states that the bound holds for every n"
        cert = {"id": "T", "kind": "CERTIFICATE", "claim_quote": thm, "statement_quote": thm, "role": "target"}
        run = {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": claim, "target": {"quote": claim, "relation": "acc_a > acc_b"},
               "covers": ["CIFAR-10-C"], "role": "target", "criterion": "stated", "test": "performance"}
        cc = {"quote": claim, "claim_type": "performance", "scope": ["CIFAR-10-C"]}

        def seal(checks, claim_):
            return tasks._seal_plan(x, "plan", {"checks": checks, "central_claims": [claim_]}, final=False)
        # a proof of a related lemma cannot stand for the experiment; beside it, as supporting evidence, it can
        assert "cannot stand for the performance claim" in _refused(lambda: seal([cert], {**cc, "checks": ["T"], "omitted": []}))
        # 2026-10-01: an unmeasured `compute` estimate is refused; a measured run, a registry size or the paper's
        # own statement of its compute is not
        omit = {"item": "CIFAR-10-C", "why": "needs 5 GB per run", "blocker": "compute"}
        assert "rests on a measurement" in _refused(lambda: seal([{**cert, "role": "supporting"}], {
            **cc, "checks": ["T"], "omitted": [omit]}))
        rec = seal([{**cert, "role": "supporting"}], {**cc, "checks": ["T"], "omitted": [
            {**omit, "blocker": "other"}]})
        assert rec["checks"][0]["role"] == "supporting"
        # an omission names what stops it, and 'data' rests on a search the harness ran
        assert "`blocker`" in _refused(lambda: seal([run], {**cc, "checks": ["R"], "scope": ["CIFAR-10-C", "MMLU"],
                                                          "omitted": [{"item": "MMLU", "why": "not linked in the paper"}]}))
        data = {"item": "MMLU", "why": "no public record", "blocker": "data"}
        base = {**cc, "checks": ["R"], "scope": ["CIFAR-10-C", "MMLU"]}
        assert "registry searches" in _refused(lambda: seal([run], {**base, "omitted": [data]}))
        none = discover.search(cfg, pid, "MMLU", get=lambda u: {"hits": {"hits": []}} if "zenodo" in u else
                               {"data": []} if "datacite" in u else [])
        seal([run], {**base, "omitted": [{**data, "discovery": [none["id"]]}]})            # searched, found nothing
        found = discover.search(cfg, pid, "CIFAR-10-C", get=_registry)
        assert "returned candidate records" in _refused(lambda: seal([run], {**base, "omitted": [{**data, "discovery": [found["id"]]}]}))
        seal([run], {**base, "omitted": [{**data, "discovery": [found["id"]], "not_the_dataset": "a different corruption suite"}]})
        cfg.allow_data_search = False                                                   # with search off, nothing can be required
        seal([run], {**base, "omitted": [data]})
        cfg.allow_data_search = True
        # a claim with no check at all still names its blocker
        assert "`blocker`" in _refused(lambda: seal([], {"quote": "Method C uses GPT-4.1 as its labeler", "claim_type": "performance",
                                                        "scope": ["GPT-4.1"], "checks": [], "why_unchecked": "paid API"}))
        gpt = {"quote": "Method C uses GPT-4.1 as its labeler", "claim_type": "performance", "scope": ["GPT-4.1"], "checks": [],
               "why_unchecked": "a paid closed API", "blocker": "credentials"}
        assert "hosted closed service" in _refused(lambda: seal([], gpt))     # credentials name the service or a gated record
        seal([], {**gpt, "service": True})


def test_completion_is_reported_apart_from_the_workflow_finishing_and_from_the_findings():
    """Sep-30: every check reached a terminal state and a lemma was contradicted, so the headline read
    CENTRAL_FAILURE_FOUND although the requested experiments had not run."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        thm = "Theorem 1 states that the bound holds for every n"
        cifar = "Method A beats method B on CIFAR-10-C with ResNet-32 by a wide margin"
        mmlu = "Our LLM monitor reaches 61.4 accuracy on MMLU in the paper"
        gpt = "Method C uses GPT-4.1 as its labeler"
        rel = lambda q, r: {"quote": q, "relation": r}
        run = {"role": "target", "criterion": "stated", "test": "performance"}
        checks = [{"id": "B", "kind": "CERTIFICATE", "claim_quote": thm, "statement_quote": thm, "role": "target",
                   "covers": ["Theorem 1"]},
                  {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": cifar, "target": rel(cifar, "acc_a > acc_b"), **run,
                   "covers": ["CIFAR-10-C"], "acquire": [{"source": "https://data.example.org/set.zip", "cited_in": "paper"}]},
                  {"id": "S", "kind": "CERTIFICATE", "claim_quote": cifar, "statement_quote": "The error bars use 95% CIs",
                   "role": "supporting"},
                  {"id": "M", "kind": "RECONSTRUCTION", "claim_quote": mmlu, "target": rel(mmlu, "acc_llm > acc_base"), **run,
                   "covers": ["MMLU"]}]
        central = [{"quote": thm, "claim_type": "theory", "checks": ["B"], "scope": ["Theorem 1"]},
                   {"quote": cifar, "claim_type": "performance", "checks": ["R", "S"], "scope": ["CIFAR-10-C"]},
                   {"quote": mmlu, "claim_type": "performance", "checks": ["M"], "scope": ["MMLU"]},
                   {"quote": gpt, "claim_type": "performance", "checks": [], "scope": ["GPT-4.1"],
                    "why_unchecked": "a paid closed API", "blocker": "credentials", "service": True}]
        _seal(cfg, pid, "plan", {"checks": checks, "central_claims": central}, td)
        done = {"runs_planned": 3, "runs_ended": 3, "runs_exited_ok": 3, "runs_failed": []}
        write = lambda cid, outcome, check=None: (state.write_json(td / pid / "checks" / cid / "outcome.json", outcome),
                                                  state.write_json(td / pid / "checks" / cid / "check.json", check or {}))
        write("C1", {"status": "COUNTEREXAMPLE_FOUND", "reason": "a violation", "values": [1], "execution": done})
        write("C2", {"status": "BLOCKED", "reason": "DATA BLOCKER (missing): x", "data_blocker": [{"source": "s", "class": "missing"}]})
        # target checks are admitted before supporting ones (2026-10-01): M is C3, the supporting S is C4
        write("C4", {"status": "NO_VIOLATION_FOUND", "reason": "held", "values": [0], "execution": done})
        write("C3", {"status": "RELATION_HOLDS", "reason": "held", "values": [0.1, 0.2, 0.15], "execution": done},
              {"deviations": [{"printed": "", "used": "a simulated stand-in for the MMLU items", "changes_claim": True}]})
        _audited(td / pid, "C1")                                            # 2026-10-02: the counterexample was audited
        led = report.ledger(tasks._Ctx(cfg, pid))
        pick = lambda prefix: next(r for r in led["completion"]["claims"] if r["claim"].startswith(prefix))
        assert led["scientific_status"] == "CENTRAL_FAILURE_FOUND"          # what was found about the theorem...
        th, cf, mm, gp = (pick(p) for p in ("Theorem 1", "Method A beats", "Our LLM monitor", "Method C uses"))
        assert th["experiment"] == "RAN_AS_SPECIFIED" and th["evidence"] == "FAILURE_FOUND"
        assert cf["experiment"] == "NOT_RUN" and cf["protocol_matched"] is None and cf["evidence"] == "NOTHING_DECIDED"   # ...says nothing about CIFAR
        assert cf["not_run"][0]["blocker"] == "data" and cf["not_run"][0]["basis"] == "harness" and cf["not_run"][0]["class"] == "missing"
        assert cf["supporting"] == [{"check": "C4", "kind": "CERTIFICATE", "status": "NO_VIOLATION_FOUND", "state": "COMPLETED"}]
        assert mm["experiment"] == "RAN_WITH_CHANGES" and mm["protocol_matched"] is False and "simulated stand-in" in mm["changes"][0]
        assert mm["evidence"] == "READING_CHANGED"                          # about the changed claim, never support of the printed one
        assert gp["experiment"] == "NOT_RUN" and gp["not_run"][0]["blocker"] == "credentials" and gp["not_run"][0]["basis"] == "planner"
        assert led["workflow"]["reached_terminal_state"] == 4               # every check is terminal, and it says nothing more
        line = report._completion_line(led["completion"])
        assert "3 — 1 ran with claim-changing changes" in line and "2 not run" in line and "ran as specified" not in line
        assert "data 1" in line and "credentials 1" in line and "not a measure of what was reproduced" in line
        table = report.table(led)
        assert "Completion, kept apart from what was found" in table and "experiment **NOT_RUN**" in table
        assert "supporting only: C4" in table and "reached a terminal state" in table
        # a supporting check never lets an empirical claim read as supported
        assert report._claim_status([{"id": "C3", "kind": "CERTIFICATE", "evidence": "x", "status": "NO_VIOLATION_FOUND",
                                      "deviations": [], "role": "supporting"}], claim_type="performance") == "NOT_CHECKED"
        # 2026-10-01 (invariant 24): nor a theorem — a simulation beside a theorem would otherwise read as more
        # support than the exact instances of its certificate
        assert report._claim_status([{"id": "C3", "kind": "RECONSTRUCTION", "evidence": "x", "status": "RELATION_HOLDS",
                                      "deviations": [], "role": "supporting"}], claim_type="theory") == "NOT_CHECKED"


def test_replicates_that_repeat_a_value_are_extended_once_when_only_more_of_them_can_decide():
    """The conformal recovery ratio was 1.0 in every replicate: three independent replicates cannot reach 95% by an
    exact sign test (it needs six). The harness runs the extra ones itself, within the check's time budget."""
    cfg = state.Config()
    check = {"kind": "RECONSTRUCTION", "stochastic": True, "seed_flow": True, "target": {"relation": "gain > 0"}, "runs": 3}
    detail = lambda n: [{"seed": s, "stage": "a", "reading": "", "out": {"gain": 0.2, "n": s}, "trials": {}, "data_fp": None}
                        for s in range(n)]
    st = lambda n: {"values": [0.2] * n, "staged": [["a", 0.2]] * n, "detail": detail(n), "seed": n, "pilot_s": 10}
    assert execute._extension(cfg, check, st(3), 3) == 6                                    # decided only by more replicates
    assert execute._extension(cfg, check, st(6), 6) == 0                                    # six is the sign test's minimum
    assert execute._extension(cfg, {**check, "stochastic": False}, st(3), 3) == 0           # a deterministic pipeline is not extended
    assert execute._extension(cfg, {**check, "test": "compatibility"}, st(3), 3) == 0
    assert execute._extension(cfg, check, {**st(3), "runs_extended": 6}, 6) == 0            # once
    assert execute._extension(cfg, check, {**st(3), "pilot_s": cfg.check_budget_s}, 3) == 0  # never past the time budget
    sure = {**st(3), "values": [0.5, 0.6, 0.55], "staged": [["a", 0.5], ["a", 0.6], ["a", 0.55]]}
    assert execute._extension(cfg, check, sure, 3) == 0                                     # a t-test already decides
    vague = {**st(3), "values": [0.1, 0.3, 0.2], "staged": [["a", 0.1], ["a", 0.3], ["a", 0.2]]}
    assert execute._extension(cfg, check, vague, 3) == 6                                    # within t(2)*SE: only more can decide
    assert execute._extension(cfg, check, {**vague, "failed_seeds": [2]}, 3) == 0           # a failed seed is not papered over
    unseeded = {**st(3), "detail": [{**r, "out": {"gain": 0.2}} for r in detail(3)]}
    assert execute._extension(cfg, {**check, "seed_flow": False}, unseeded, 3) == 0          # one run repeated: more of it decides nothing
    pr = execute.protocol({**check, "kind": "RECONSTRUCTION", "runs_source": "referee_floor"}, {"runs_extended": 6}, "rule")
    assert pr["runs"] == 6 and "from 3 to 6" in pr["replicates_extended"]


def test_a_claim_that_ran_only_on_a_substitute_gets_the_follow_up_round():
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        mmlu = "Our LLM monitor reaches 61.4 accuracy on MMLU in the paper"
        chk = {"id": "M", "kind": "RECONSTRUCTION", "claim_quote": mmlu, "covers": ["MMLU"], "role": "target",
               "criterion": "stated", "test": "performance", "target": {"quote": mmlu, "relation": "acc_llm > acc_base"}}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [
            {"quote": mmlu, "claim_type": "performance", "checks": ["M"], "scope": ["MMLU"]}]}, td)
        done = {"runs_planned": 3, "runs_ended": 3, "runs_exited_ok": 3, "runs_failed": []}
        out = {"status": "RELATION_HOLDS", "reason": "held", "values": [0.1, 0.2, 0.15], "execution": done}
        cdir = td / pid / "checks" / "C1"
        state.write_json(cdir / "outcome.json", out)
        state.write_json(cdir / "check.json", {"deviations": [{"printed": "", "used": "a simulated stand-in", "changes_claim": True}]})
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and [o["id"] for o in owed] == ["plan:2"]        # supported, but not the experiment the claim names
        prompt = Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        assert "the requested experiment: RAN_WITH_CHANGES" in prompt and "discover" in prompt
        assert "Still undecided:\n- 'Our LLM monitor" in prompt                 # listed as undecided although its status is not
        state.write_json(cdir / "check.json", {"deviations": []})               # the same result, run as specified
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert "Still undecided:\n- none" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")


def test_a_shared_cache_is_never_corrupted_by_concurrent_or_cut_transfers():
    """Sep-30 rerun, transformer: four checks fetched one Zenodo record at once into one cache with a fixed `.part`
    name. The same file arrived with a different md5 every time, some arrived empty, one fetch hit
    FileNotFoundError, and 2-8 of 25 files per check were lost."""
    import hashlib
    import threading
    import time
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        good = bytes(range(256)) * 4000                                     # ~1 MB
        md5 = hashlib.md5(good).hexdigest()
        calls = {"n": 0}

        def cut(h):                      # claims the whole file, sends half, closes
            calls["n"] += 1
            return (200, {"Content-Length": str(len(good))}, good[: len(good) // 2]) if calls["n"] == 1 else (200, {}, good)
        srv, host = _serve({"/cut.bin": cut})
        f = fetcher.Fetcher(str(td / "cache"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
        p, info = f.get(f"http://{host}/cut.bin", str(td / "d"))
        assert Path(p).read_bytes() == good and len(info["retries"]) == 1 and "of" in info["retries"][0]["error"]   # a short transfer is retried
        srv.shutdown()

        seen = {"n": 0}
        srv, host = _serve({"/bad.bin": lambda h: (200, {}, (good[::-1] if seen.__setitem__("n", seen["n"] + 1) or seen["n"] == 1 else good))})
        f = fetcher.Fetcher(str(td / "c2"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
        p, info = f.get(f"http://{host}/bad.bin", str(td / "d2"), md5)
        assert Path(p).read_bytes() == good and info["retries"][0]["class"] == "corrupt"                 # a corrupt transfer is retried
        srv.shutdown()

        srv, host = _serve({"/wrong.bin": lambda h: (200, {}, good[::-1])})
        f = fetcher.Fetcher(str(td / "c3"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
        try:
            f.get(f"http://{host}/wrong.bin", str(td / "d3"), md5)
            raise AssertionError("a file that never matches its published md5 was fetched")
        except fetcher.FetchError as e:
            assert e.klass == "content_invalid" and len(e.attempts) == 3
        assert not [x for x in (td / "c3").rglob("*") if x.is_file()]                                   # nothing wrong stays in the cache
        srv.shutdown()

        # a cached copy that disagrees with the published checksum is purged, not trusted
        srv, host = _serve({"/ok.bin": lambda h: (200, {}, good)})
        f = fetcher.Fetcher(str(td / "c4"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
        f.get(f"http://{host}/ok.bin", str(td / "d4"))
        (cached,) = [x for x in (td / "c4").rglob("ok.bin")]
        cached.write_bytes(b"poisoned by an earlier race")
        p, info = f.get(f"http://{host}/ok.bin", str(td / "d4b"), md5)
        assert Path(p).read_bytes() == good and "cache_purged" in info
        srv.shutdown()

        # four containers, one cache, one slow file: every one gets the whole file, and nothing is left half-written
        def slow(h):                     # streamed over ~0.4 s, so four writers overlap in time
            def chunks():
                step = len(good) // 8
                for i in range(8):
                    time.sleep(0.05)
                    yield good[i * step:(i + 1) * step] if i < 7 else good[7 * step:]
            return 200, {"Content-Length": str(len(good))}, chunks()
        srv, host = _serve({"/slow.bin": slow})
        got, errs = [], []

        def worker(i):
            try:
                p, _ = fetcher.Fetcher(str(td / "shared"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=10).get(
                    f"http://{host}/slow.bin", str(td / f"w{i}"), md5)
                got.append(Path(p).read_bytes() == good)
            except Exception as e:                                                   # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [x.start() for x in ts]
        temps: set = set()
        while any(x.is_alive() for x in ts):                              # watch the cache while they write
            temps |= {x.name for x in (td / "shared").rglob("*") if ".part." in x.name}
            time.sleep(0.01)
        [x.join() for x in ts]
        assert got == [True] * 4 and not errs and not [x for x in (td / "shared").rglob("*") if ".part" in x.name]
        assert len(temps) >= 2, temps             # writers never share one temp file (a fixed name interleaved their bytes)
        srv.shutdown()


def test_an_include_that_names_what_a_download_holds_is_applied_to_its_members():
    """Sep-30 rerun, conformal C8: the planner could not see the landing page, named the files INSIDE the dataset ZIP
    (`train.csv*`), matched no link, and got a data blocker although the same page was acquired for another check."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        zipped = _zip({"train.csv.zip": "nested", "solution.csv": "a,b\n", "readme.txt": "notes"})
        one, host = _serve({"/ds": lambda h: (200, {"Content-Type": "text/html"}, b'<a href="/static/ds.zip">zip</a><a href="/about">x</a>'),
                            "/static/ds.zip": lambda h: (200, {}, zipped)})
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/ds", "include": ["train.csv*"]}])[0]
        assert [a["file"] for a in rec["admitted"]] == ["train.csv.zip"] and rec["failure_class"] == ""   # only the member it named
        assert any("no link on the landing page matched" in r for r in rec["recovery"])
        rec = _fetch(fetcher, td / "b", [{"source": f"http://{host}/ds", "include": ["nothing-inside*"]}])[0]
        assert sorted(a["file"] for a in rec["admitted"]) == ["readme.txt", "solution.csv", "train.csv.zip"]   # none matched: all members
        rec = _fetch(fetcher, td / "c", [{"source": f"http://{host}/ds", "include": ["*.zip"]}])[0]
        assert len(rec["admitted"]) == 3 and not any("no link" in r for r in rec["recovery"])            # a link match does not filter members
        one.shutdown()
        many = "".join(f'<a href="/f{i}.zip">f</a>' for i in range(5))
        two, host2 = _serve({"/ds": lambda h: (200, {"Content-Type": "text/html"}, many.encode())})
        rec = _fetch(fetcher, td / "d", [{"source": f"http://{host2}/ds", "include": ["train.csv*"]}])[0]
        assert rec["admitted"] == [] and rec["failure_class"] == "no_data"                               # five candidates: no guessing
        two.shutdown()


def test_one_fetch_per_plan_in_flight_and_a_follow_up_that_covers_an_omission_removes_it():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        plan = [{"source": "https://zenodo.org/records/1", "include": ["*.csv"], "required": True}]
        state.write_json(root / "checks" / "C1" / "data.json", {"rec": {"container": "x"}, "sources": plan})   # C1 is fetching it
        cfg = state.Config()
        real = execute.docker_status, execute.start
        execute.docker_status = lambda: (True, "")
        started = []
        execute.start = lambda *a, **k: started.append(a) or {}
        try:
            assert execute.fetch(cfg, root, "C2", plan) is None and not started              # waits for C1's fetch, starts none
            assert execute.fetch(cfg, root, "C3", [{**plan[0], "include": ["other*"]}]) is None and len(started) == 1   # another plan: its own
        finally:
            execute.docker_status, execute.start = real
    first = {"checks": [{"id": "C1", "covers": []}], "dropped": [], "repo_is_authors": False,
             "central_claims": [{"quote": "q", "checks": ["C1"], "omitted": [{"item": "CIFAR10-C image monitoring", "why": "no url"},
                                                                             {"item": "URM", "why": "unspecified"}], "why_unchecked": ""}]}
    follow = {"checks": [{"id": "C7", "covers": ["CIFAR10-C image monitoring"]}], "dropped": [],
              "central_claims": [{"quote": "q", "checks": ["C7"], "omitted": [], "why_unchecked": ""}]}
    merged = report.merged(first, follow)
    assert merged["central_claims"][0]["checks"] == ["C1", "C7"]
    # 2026-10-01: an omission stays on record until the check that covers it RAN — planning a follow-up is not running it
    assert [o["item"] for o in merged["central_claims"][0]["omitted"]] == ["CIFAR10-C image monitoring", "URM"]
    cc = {**merged["central_claims"][0], "page": 1, "claim_type": "performance", "scope": ["CIFAR10-C image monitoring", "URM"]}
    chk = lambda cid, st, covers: {"id": cid, "kind": "RECONSTRUCTION", "role": "target", "state": st, "status": "RELATION_HOLDS",
                                   "values": [1.0] if st == "COMPLETED" else None, "deviations": [], "covers": covers,
                                   "data_changed": [], "reason": ""}
    ran = report._completion_row(cc, {"C1": chk("C1", "NOT_RUN", []), "C7": chk("C7", "COMPLETED", ["CIFAR10-C image monitoring"])})
    assert ran["scope_not_run"] == ["URM"] and ran["experiment"] == "RAN_PARTIAL" and ran["protocol_matched"] is False
    # Oct-02 PPRM review: "not run — LLM QA ...: data" was printed beside C7, which covered that item and COMPLETED.
    # An omission a completed target check covers ran: it is no longer a reason anything did not run.
    assert [b["item"] for b in ran["not_run"]] == ["URM"], ran["not_run"]
    planned = report._completion_row(cc, {"C1": chk("C1", "NOT_RUN", []), "C7": chk("C7", "NOT_STARTED", ["CIFAR10-C image monitoring"])})
    assert planned["experiment"] == "NOT_RUN" and planned["scope_not_run"] == ["CIFAR10-C image monitoring", "URM"]
    assert {b["item"] for b in planned["not_run"]} >= {"CIFAR10-C image monitoring", "URM"}      # planned is not run
    with tempfile.TemporaryDirectory() as t:                  # the report alone is written again; the old one is kept
        cfg, pid = _project(Path(t))
        root = state.pdir(cfg, pid)
        assert "no sealed report" in tasks.reopen(cfg, pid, "report", "x")["error"]
        (root / "sealed").mkdir(exist_ok=True)
        state.write_json(root / "sealed" / "report.json", {"prose": "p"})
        (root / "review.md").write_text("old review", encoding="utf-8")
        state.write_json(root / "seals.json", {"plan": 1, "report": state.sha256((root / "sealed" / "report.json").read_bytes())})
        out = tasks.reopen(cfg, pid, "report", "a report-code fix")
        assert out["kept"] == "review.reopened.1.md" and (root / "review.reopened.1.md").read_text(encoding="utf-8") == "old review"
        assert set(state.read_json(root / "seals.json")) == {"plan"} and (root / "sealed" / "report.withdrawn.1.json").exists()


def test_a_criterion_the_planner_supplied_makes_the_result_about_that_criterion():
    """Sep-30 rerun, transformer: 'provides a surprisingly good description' states no comparison; the planner's rival
    family decided it, one check labelled that a protocol choice, and the claim read FAILURE_FOUND."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        x = tasks._Ctx(cfg, pid)
        claim = "We report the mean over 5 random seeds"
        rel = {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": claim, "target": {"quote": claim, "relation": "chi2_formula < chi2_rival"},
               "role": "target", "test": "performance", "covers": ["the fit"]}
        central = [{"quote": claim, "claim_type": "performance", "checks": ["R"], "scope": ["the fit"]}]
        rec = tasks._seal_plan(x, "plan", {"checks": [{**rel, "criterion": "supplied"}], "central_claims": central}, final=False)
        assert rec["checks"][0]["criterion"] == "supplied"
        # 2026-10-01: an omitted criterion no longer defaults to 'stated' (which recorded no claim change)
        assert "`criterion`" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [rel], "central_claims": central}, final=False))
        assert tasks._seal_plan(x, "plan", {"checks": [{**rel, "criterion": "stated"}], "central_claims": central},
                                final=False)["checks"][0]["criterion"] == "stated"
        plan = {"checks": [rec["checks"][0]]}
        x2 = type("X", (), {"root": td, "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                            "plan": lambda self: plan})()
        binds = [{"kind": k, "impl_quote": q, "paper_quote": claim} for k, q in
                 (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "a = 1"), ("comparison_target", "print("))]
        script = "a = 1\nfit()\nload()\nrng = np.random.default_rng(args.seed)\nprint('REFEREE_RESULT', a, 'data_fingerprint')\n"
        g = {"fidelity": FID, "script": script, "runs": 1, "outputs": ["chi2_formula", "chi2_rival"], "bindings": binds, "stochastic": True}
        sealed = tasks._seal_gen(x2, "gen:C1.1", g, final=False)
        dev = [d for d in sealed["deviations"] if "supplied the decision criterion" in d["used"]]
        assert len(dev) == 1 and dev[0]["changes_claim"] is True                                # recorded by the harness, not chosen by the script
        c = {"id": "C1", "kind": "RECONSTRUCTION", "evidence": "x", "status": "RELATION_VIOLATED", "deviations": sealed["deviations"]}
        assert report._claim_status([c], claim_type="performance") == "READING_CHANGED"         # never FAILURE_FOUND of the printed claim


def test_an_experiment_that_trains_nothing_declares_training_not_applicable_instead_of_being_refused():
    """Sep-30 rerun, PPRM type-I check: a sequential test on simulated streams trains nothing, so the required
    `training` binding could only be faked or refused, and the honest refusal ended the check."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        plan = {"checks": [{"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        x = type("X", (), {"root": Path(t), "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                           "plan": lambda self: plan})()
        pq = "reaches 61.4 accuracy"
        binds = [{"kind": k, "impl_quote": q, "paper_quote": pq} for k, q in
                 (("method", "run()"), ("dataset", "load()"), ("metric", "acc = 61.0"), ("comparison_target", "print("))]
        script = "acc = 61.0\nrun()\nload()\nrng = np.random.default_rng(args.seed)\nprint('REFEREE_RESULT', acc, 'data_fingerprint')\n"
        g = {"fidelity": FID, "script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "stochastic": True}
        why = "nothing is trained: the stream is simulated and the test is a closed-form bound"
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "bindings": binds}, final=False)["refused"]           # no training binding at all
        ok = tasks._seal_gen(x, "gen:C1.1", {**g, "bindings": binds + [{"kind": "training", "not_applicable": why}]}, final=False)
        assert [b for b in ok["bindings"] if b["kind"] == "training"] == [
            {"kind": "training", "impl_quote": "", "paper_quote": "", "page": None, "not_applicable": why}]   # recorded for the verifier
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "bindings": binds + [{"kind": "training", "not_applicable": "none"}]},
                               final=False)["refused"]                                                      # a reason, not a flag
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "bindings": [b for b in binds if b["kind"] != "method"] + [
            {"kind": "training", "not_applicable": why}, {"kind": "method", "not_applicable": why}]}, final=False)["refused"]   # only training


def test_a_cut_in_the_files_followed_is_never_silent_and_a_records_own_listing_is_followed_whole():
    """Sep-30 rerun, transformer: a 25-file Zenodo record lost 5 files to a fixed cap of 20 with no trace; script
    authors saw 'the acquired set of 20 files' and refused. A cut is recorded; a repository record's own listing
    (sizes and checksums, bounded by the storage cap) is not cut at the landing-page limit."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td, n = Path(t), fetcher.MAX_FOLLOW + 5
        page = "".join(f'<a href="/f/{i}.csv">f{i}</a>' for i in range(n)).encode()
        routes = {"/ds": lambda h: (200, {"Content-Type": "text/html"}, page)}
        routes.update({f"/f/{i}.csv": (lambda h, i=i: (200, {}, f"a,b\n{i},1\n".encode())) for i in range(n)})
        srv, host = _serve(routes)
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/ds"}])[0]          # a landing page: its links are unvetted
        assert len(rec["admitted"]) == fetcher.MAX_FOLLOW
        assert rec["truncated"]["matched"] == n and rec["truncated"]["followed"] == fetcher.MAX_FOLLOW
        assert len(rec["truncated"]["not_followed"]) == n - fetcher.MAX_FOLLOW
        man = fetcher.manifest(str(td / "data"), [rec])
        assert man["status"] == "partial"                                           # not 'ok': files are missing
        srv.shutdown()

        listing = json.dumps({"files": [{"key": f"r{i}.csv", "size": len(f"a,b\n{i},1\n"), "checksum": "", "links": {"self": f"http://HOST/files/r{i}.csv"}}
                                        for i in range(n)]})
        box = {}
        routes = {"/records/9": lambda h: (200, {"Content-Type": "text/html"}, b"<html>no links</html>"),
                  "/api/records/9": lambda h: (200, {}, listing.replace("HOST", box["host"]).encode())}
        routes.update({f"/files/r{i}.csv": (lambda h, i=i: (200, {}, f"a,b\n{i},1\n".encode())) for i in range(n)})
        srv, host = _serve(routes)
        box["host"] = host
        fetcher.RECORD_APIS[host] = f"http://{host}/api/records/{{id}}"
        try:
            rec = _fetch(fetcher, td / "r", [{"source": f"http://{host}/records/9"}])[0]
            assert len(rec["admitted"]) == n and "truncated" not in rec              # all 25, not 20
            assert fetcher.manifest(str(td / "r" / "data"), [rec])["status"] == "ok"
            rec = _fetch(fetcher, td / "s", [{"source": f"http://{host}/records/9", "include": ["r1*"]}])[0]
            assert sorted(a["file"] for a in rec["admitted"]) == sorted(f"r{i}.csv" for i in range(n) if f"r{i}".startswith("r1"))
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()


def test_the_files_of_a_cited_record_can_be_listed_before_choosing_include():
    """A planner chose `include` blind: a bare '*.csv' took every file of that kind and the rest of a record was
    never seen. The listing is the record's own API (names, sizes, checksums), logged, bounded and permission-gated."""
    from harness import discover
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project_pages(Path(t), DATA_PAGES)
        seen = []
        listing = {"files": [{"key": "a.csv", "size": 5, "checksum": "md5:" + "1" * 32, "links": {"self": "https://zenodo.org/api/x/a.csv"}},
                             {"key": "b.zip", "size": 9, "checksum": "md5:" + "2" * 32, "links": {"self": "https://zenodo.org/api/x/b.zip"}}]}
        get = lambda u: seen.append(u) or listing
        rec = discover.files(cfg, pid, "https://doi.org/10.5281/zenodo.18281512", get=get)
        assert [f["name"] for f in rec["files"]] == ["a.csv", "b.zip"] and rec["total_bytes"] == 14 and rec["id"] == "F1"
        assert seen == ["https://zenodo.org/api/records/18281512"]                     # the documented API of that record
        assert "not a record page" in discover.files(cfg, pid, "https://example.org/page", get=get)["error"]   # no adapter: nothing sent
        assert seen == ["https://zenodo.org/api/records/18281512"]
        assert [r["id"] for r in discover.records(cfg, pid)] == ["F1"]                 # logged beside the searches
        x = tasks._Ctx(cfg, pid)
        assert "files of https://doi.org/10.5281/zenodo.18281512 -> 2 file(s)" in tasks._discovery_text(x)
        errs: list = []
        assert not tasks._acquire(x, {"acquire": [{"source": "https://zenodo.org/records/18281512", "cited_in": "discovery",
                                                    "discovery": "F1", "named_in_paper": "x", "include": ["a.csv"]}]}, errs, "C1")
        assert errs                                                                     # a listing is no search result to cite
        old = cfg.allow_data_search
        cfg.allow_data_search = False
        try:
            assert "off" in discover.files(cfg, pid, "https://zenodo.org/records/1", get=get)["error"] and len(seen) == 1
        finally:
            cfg.allow_data_search = old
        many = {"files": [{"key": f"f{i}", "size": 1, "checksum": "", "links": {"self": "https://zenodo.org/x"}} for i in range(3)]}
        cfg.max_discoveries = 2
        assert "error" not in discover.files(cfg, pid, "https://zenodo.org/records/5", get=lambda u: many)
        assert "budget" in discover.files(cfg, pid, "https://zenodo.org/records/6", get=lambda u: many)["error"]   # bounded


def test_a_check_that_ended_without_a_finding_can_be_reopened_but_a_finding_never_is():
    """After a harness fix the operator should redo a refused/blocked check, not a whole paper; a check that found
    something is never re-rolled, and what the old attempt said stays on disk."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        root = state.pdir(cfg, pid)
        for cid, status in (("C1", "NOT_CHECKABLE"), ("C2", "RELATION_HOLDS"), ("C3", "BLOCKED")):
            cdir = root / "checks" / cid
            state.write_json(cdir / "outcome.json", {"check": cid, "status": status, "reason": "r",
                                                     **({"data_blocker": [{"source": "s"}]} if cid == "C3" else {})})
            state.write_json(cdir / "data.json", {"n_files": 0})
            (cdir / "script.py").write_text("print(1)", encoding="utf-8")
            (root / "sealed").mkdir(exist_ok=True)
            for tid in (f"gen:{cid}.1", f"verify:{cid}.1", f"bind:{cid}"):
                state.write_json(root / "sealed" / f"{tasks._safe(tid)}.json", {"id": tid})
        state.write_json(root / "seals.json", {"plan": 1, "gen:C1.1": 1, "verify:C1.1": 1, "gen:C2.1": 1, "bind:C1": 1, "gen:C3.1": 1,
                                               "report": 1})
        state.write_json(root / "attempts.json", {"gen:C1.1": 3, "verify:C1.1": 1, "gen:C2.1": 2, "report": 1})
        assert "never reopened" in tasks.reopen(cfg, pid, "C2", "again")["error"]
        assert "no outcome" in tasks.reopen(cfg, pid, "C9", "x")["error"]
        res = tasks.reopen(cfg, pid, "C1", "training refusal fixed")
        assert sorted(res["seals_withdrawn"]) == ["bind:C1", "gen:C1.1", "report", "verify:C1.1"] and res["was"] == "NOT_CHECKABLE"
        seals = state.read_json(root / "seals.json")
        assert set(seals) == {"plan", "gen:C2.1", "gen:C3.1"}          # C1's script seals and the report, not C2's or C3's
        assert state.read_json(root / "attempts.json") == {"gen:C2.1": 2}                # C1 redone with its whole attempt budget
        cd = root / "checks" / "C1"
        assert not (cd / "outcome.json").exists() and (cd / "outcome.reopened.1.json").exists() and not (cd / "script.py").exists()
        assert (cd / "data.json").exists()                                              # data was fine: kept
        assert (root / "checks" / "C2" / "outcome.json").exists()
        tasks.reopen(cfg, pid, "C3", "source fixed")
        assert not (root / "checks" / "C3" / "data.json").exists()                     # a failed acquisition is tried again
        state.write_json(root / "checks" / "C4" / "outcome.json", {"check": "C4", "status": "NOT_CHECKABLE", "reason": "r"})
        state.write_json(root / "checks" / "C4" / "data.json", {"n_files": 20, "sources": [{"truncated": {"matched": 25, "followed": 20}}]})
        tasks.reopen(cfg, pid, "C4", "the cap was raised")
        assert not (root / "checks" / "C4" / "data.json").exists()                     # a cut acquisition is fetched again
        log = [json.loads(ln) for ln in (root / "log.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert [e for e in log if e.get("event") == "reopen"][0]["check"] == "C1"


def test_a_cut_acquisition_is_not_shared_with_another_check_but_a_whole_one_is():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        plan = [{"source": "https://zenodo.org/records/1", "include": ["*.csv"], "required": True}]
        cut = {"matched": 25, "followed": 20, "not_followed": ["x"]}
        man = {"sources": [{"source": plan[0]["source"], "dir": "0", "admitted_files": 20, "truncated": cut}], "n_files": 20,
               "fetched_at": "t", "plan": plan, "volume": "v1"}
        state.write_json(root / "checks" / "C1" / "data.json", man)
        cfg = state.Config()
        real = execute.docker_status, execute.start, execute._volume_gone
        execute.docker_status = lambda: (True, "")
        execute._volume_gone = lambda v: False
        started = []
        execute.start = lambda *a, **k: started.append(a) or {}
        try:
            assert execute.fetch(cfg, root, "C2", plan) is None and len(started) == 1        # fetched again, in its own volume
            state.write_json(root / "checks" / "C1" / "data.json", {**man, "sources": [{**man["sources"][0], "truncated": None}]})
            got = execute.fetch(cfg, root, "C3", plan)
            assert got and got["shared_with"] == "C1" and len(started) == 1                  # a complete set is shared
        finally:
            execute.docker_status, execute.start, execute._volume_gone = real


def test_a_closed_gate_refuses_only_the_sources_that_need_it_and_status_never_advances_a_paper():
    """Sep-30 rerun, PPRM: `run.py status <paper>` ran the protocol from a shell without SH_ALLOW_INSTALL; the gate
    check was all-or-nothing, so a plain Zenodo source was refused because an hf:// source sat beside it, and two checks
    ended as permanent data blockers for a difference in the caller's environment."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        plan = [{"source": "https://zenodo.org/records/1", "include": ["*.csv"], "required": True},
                {"source": "hf://datasets/o/n", "required": True}]
        cfg = state.Config()
        cfg.allow_network, cfg.allow_install = True, False
        real = execute.docker_status, execute.start
        execute.docker_status = lambda: (True, "")
        started = []
        execute.start = lambda *a, **k: started.append(k) or {}
        try:
            assert execute.fetch(cfg, root, "C1", plan) is None and len(started) == 1           # the plain source is fetched
            sent = json.loads(started[0]["env"]["REFEREE_SOURCES"])
            assert "refused" not in sent[0] and "SH_ALLOW_INSTALL" in sent[1]["refused"]       # only the hub download is refused
            cfg.allow_network = False
            d = execute.fetch(cfg, root, "C2", plan)                                             # every source shut: a manifest, not a run
            assert d["n_files"] == 0 and {s["failure_class"] for s in d["sources"]} == {"gate"} and len(started) == 1
        finally:
            execute.docker_status, execute.start = real
        td = Path(t) / "f"
        rec = _fetch(fetcher, td, [{"source": "http://127.0.0.1:1/x", "refused": "SH_ALLOW_INSTALL is not set"}])[0]
        assert rec["failure_class"] == "gate" and rec["attempts"] == [] and not rec["admitted"]   # nothing was sent
    import run
    with tempfile.TemporaryDirectory() as t:
        cfg2, pid = _project(Path(t))
        state.write_json(state.pdir(cfg2, pid) / "ledger.json", {"scientific_status": "NOT_CHECKED", "checks": [], "completion": {}})
        real_adv, real_cfg = tasks.advance, state.Config
        tasks.advance = lambda *a, **k: (_ for _ in ()).throw(AssertionError("status advanced the paper"))
        state.Config = lambda: cfg2
        try:
            assert run.main(["status", pid]) == 0
        finally:
            tasks.advance, state.Config = real_adv, real_cfg



# --- 2026-10-01 audit: classifications, blockers, completion and earned words ----------------------
# 2026-10-01 audit: classifications fail closed, blockers rest on the harness's records, completion is
# derived from what ran, support words are earned by the claim. Run: python tests/_t_lead.py

CIFAR = "Method A beats method B on CIFAR-10-C with ResNet-32 by a wide margin"
MMLU = "Our LLM monitor reaches 61.4 accuracy on MMLU in the paper"
THM = "Theorem 1 states that the bound holds for every n"
GPT = "Method C uses GPT-4.1 as its labeler"
RUN = {"role": "target", "criterion": "stated", "test": "performance"}


def _x(td: Path, extra: tuple = ()):
    cfg, pid = _project_pages(td, DATA_PAGES + list(extra))
    return cfg, pid, tasks._Ctx(cfg, pid)


def _run(cid="R", covers=("CIFAR-10-C",), **kw):
    return {"id": cid, "kind": "RECONSTRUCTION", "claim_quote": CIFAR, "target": {"quote": CIFAR, "relation": "acc_a > acc_b"},
            "covers": list(covers), **RUN, **kw}


def _claim(**kw):
    return {"quote": CIFAR, "claim_type": "performance", "checks": ["R"], "scope": ["CIFAR-10-C"], **kw}


def _plan(x, checks, claims, final=False, tid="plan"):
    return tasks._seal_plan(x, tid, {"checks": checks, "central_claims": claims}, final=final)


def test_an_other_blocker_sealed_on_a_spent_search_budget_says_so():
    """Oct-01 PPRM: the Qwen 3B/7B/32B predictors and the 7B filter were omitted as `other` because "the 12-search budget
    is spent" — a setting of this run, read by nobody as one. The seal records the harness fact beside every
    `other`/`protocol` omission, and the review prints it; `other` stays the planner's word."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        om = {"item": "Qwen 7B predictor", "why": "no successful search and the budget is spent", "blocker": "other"}
        claim = _claim(scope=["CIFAR-10-C", "Qwen 7B predictor"], omitted=[om])
        rec = _plan(x, [_run()], [claim])
        assert "budget_spent" not in rec["central_claims"][0]["omitted"][0]                     # budget left: no note
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        for i in range(cfg.max_discoveries):
            state.append_jsonl(x.root / "discovery.jsonl", {"id": f"D{i + 1}", "query": f"q{i}", "results": {"zenodo": {"candidates": []}}})
        rec = _plan(x, [_run()], [claim])
        o = rec["central_claims"][0]["omitted"][0]
        assert o["budget_spent"] == f"{cfg.max_discoveries} of {cfg.max_discoveries}", o
        row = report._completion_row({**rec["central_claims"][0], "page": 1}, {})
        b = next(b for b in row["not_run"] if b["item"] == "Qwen 7B predictor")
        assert b["blocker"] == "other" and b["budget_spent"]                                       # the planner's word, and the fact


def test_a_classification_left_out_is_refused_never_defaulted():
    """A missing role read as 'target', a missing criterion as 'stated', a missing claim type switched every
    empirical rule off, and on the planner's third attempt every claim-level error was sealed as it stood."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        _plan(x, [_run()], [_claim()])                                                   # complete: sealed
        for drop, word in (("role", "`role`"), ("criterion", "`criterion`"), ("test", "`test`")):
            c = _run()
            c.pop(drop)
            assert word in _refused(lambda: _plan(x, [c], [_claim()])), drop
        assert "`role`" in _refused(lambda: _plan(x, [_run(role="Supporting")], [_claim()]))   # closed vocabulary, exact
        cl = _claim()
        cl.pop("claim_type")
        assert "`claim_type`" in _refused(lambda: _plan(x, [_run()], [cl]))
        assert "`scope`" in _refused(lambda: _plan(x, [_run()], [_claim(scope=[])]))
        # the last attempt seals the strictest reading, visibly — never the error as it stood
        cert = {"id": "T", "kind": "CERTIFICATE", "claim_quote": CIFAR, "statement_quote": THM, "role": "target",
                "covers": ["CIFAR-10-C"]}
        rec = _plan(x, [cert], [{**cl, "checks": ["T"]}], final=True)
        cc = rec["central_claims"][0]
        assert cc["claim_type"] == "" and cc["checks"] == [] and cc["sealed_with_errors"]   # a certificate never stands for it
        assert rec["checks"][0]["central"] is False and rec["checks"][0]["incidental_why"]
        assert report._empirical(cc["claim_type"])                                     # downstream: untyped is empirical
        rec = _plan(x, [_run(covers=[])], [_claim()], final=True)                       # uncovered scope on the last attempt
        assert rec["central_claims"][0]["omitted"][0]["item"] == "CIFAR-10-C"
        assert rec["central_claims"][0]["omitted"][0]["blocker"] == "unstated"


def test_a_follow_up_round_never_retypes_a_claim():
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, x = _x(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        _seal(cfg, pid, "plan", {"checks": [_run()], "central_claims": [_claim()]}, td)
        x = tasks._Ctx(cfg, pid)
        f1 = _run("F1")
        assert "never retypes" in _refused(lambda: _plan(x, [f1], [_claim(checks=["F1"], claim_type="theory")], tid="plan:2"))
        assert _plan(x, [f1], [_claim(checks=["F1"])], tid="plan:2")["central_claims"][0]["claim_type"] == "performance"


def _discovery(cfg, pid, cands: list[dict]) -> str:
    return discover.search(cfg, pid, "Qwen2-VL-2B", registry="huggingface-models",
                           get=lambda u: [{"id": c["id"], "author": "Qwen"} for c in cands]) and _patch_candidates(cfg, pid, cands)


def _patch_candidates(cfg, pid, cands: list[dict]) -> str:
    """The registry record as the harness logged it, with the gating and size fields a hub returns."""
    f = discover.log_path(cfg, pid)
    rows = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for c in rows[-1]["results"]["huggingface-models"]["candidates"]:
        c.update(next(({k: v for k, v in d.items() if k != "id"} for d in cands if f"hf://models/{d['id']}" == c["source"]), {}))
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows[-1]["id"]


def test_a_blocker_rests_on_what_the_harness_holds():
    """Sep-30 rerun 2 PPRM: open-weight Qwen models were given up as 'credentials' and as 'compute' against the
    host's RAM with no measurement, and no registry search was run for them or for the QA benchmarks."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t), ("The full sweep took 6,000 GPU hours on 64 A100 cards.",))
        cfg.allow_network = cfg.allow_data_search = True
        did = _discovery(cfg, pid, [{"id": "Qwen/Qwen2-VL-2B", "gated": False, "private": False, "size_bytes": 4_400_000_000},
                                    {"id": "Qwen/Qwen2.5-VL-32B", "gated": False, "private": False, "size_bytes": 67_000_000_000},
                                    {"id": "meta/Gated-7B", "gated": "manual", "private": False, "size_bytes": 14_000_000_000}])
        omit = lambda **o: _claim(scope=["CIFAR-10-C", "Qwen"], omitted=[{"item": "Qwen", "why": "the predictor sweep", **o}])
        cred = {"blocker": "credentials", "discovery": [did]}
        assert "hosted closed service" in _refused(lambda: _plan(x, [_run()], [omit(blocker="credentials")]))
        assert "public and not gated" in _refused(lambda: _plan(x, [_run()], [omit(**cred, artifact="hf://models/Qwen/Qwen2-VL-2B")]))
        _plan(x, [_run()], [omit(**cred, artifact="hf://models/meta/Gated-7B")])         # gated: the blocker stands
        _plan(x, [_run()], [omit(blocker="credentials", service=True)])                  # a paid closed API
        real = getattr(execute, "host", None)
        execute.host = lambda cfg: {"cpus": 6, "ram_mb": 5926, "gpu": True, "vram_mb": 8188, "disk_free_gb": 200.0}
        try:
            comp = {"blocker": "compute", "discovery": [did]}
            assert "rests on a measurement" in _refused(lambda: _plan(x, [_run()], [omit(blocker="compute")]))
            assert "within this host's measured memory" in _refused(lambda: _plan(x, [_run()], [omit(
                **comp, artifact="hf://models/Qwen/Qwen2-VL-2B")]))
            _plan(x, [_run()], [omit(**comp, artifact="hf://models/Qwen/Qwen2.5-VL-32B")])   # 67 GB: beyond this host
        finally:
            if real is None:
                del execute.host
            else:
                execute.host = real
        _plan(x, [_run()], [omit(blocker="compute", paper_quote="The full sweep took 6,000 GPU hours on 64 A100 cards")])
        assert "states none" in _refused(lambda: _plan(x, [_run()], [omit(           # a sentence that states no compute
            blocker="compute", paper_quote="Method A beats method B on CIFAR-10-C")]))
        state.write_json(x.root / "checks" / "C9" / "outcome.json", {"status": "BLOCKED", "reason": "RESOURCE BLOCKER: pilot 2h"})
        _plan(x, [_run()], [omit(blocker="compute", failed_checks=["C9"])])               # a measured run past the budget
        assert "rests on a measurement" in _refused(lambda: _plan(x, [_run()], [omit(blocker="compute", failed_checks=["C1"])]))
        cfg.allow_data_search = False                       # with search off the harness holds nothing to check against
        _plan(x, [_run()], [omit(blocker="credentials")])
        rec = _plan(x, [_run()], [omit(blocker="credentials")], final=True)
        assert not rec["central_claims"][0]["omitted"][0].get("unverified")
        cfg.allow_data_search = True
        rec = _plan(x, [_run()], [omit(blocker="credentials")], final=True)             # sealed on the last attempt: flagged
        assert "hosted closed service" in rec["central_claims"][0]["omitted"][0]["unverified"]


def test_a_supporting_check_covers_none_of_the_claim():
    """Probe P7: a target covering CIFAR and a supporting simulation 'covering' MMLU read RAN_AS_SPECIFIED,
    SUPPORT_FOUND, with MMLU never run and the follow-up round skipped."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        sim = {**_run("S", covers=["MMLU"]), "role": "supporting"}
        both = _claim(checks=["R", "S"], scope=["CIFAR-10-C", "MMLU"])
        assert "linked TARGET check" in _refused(lambda: _plan(x, [_run(), sim], [both]))
        rec = _plan(x, [_run(), sim], [both], final=True)
        assert [o["item"] for o in rec["central_claims"][0]["omitted"]] == ["MMLU"]
        assert rec["checks"][0]["role"] == "target"                       # target checks are admitted first
        cfg.max_checks = 1
        rec = _plan(x, [sim, _run()], [both], final=True)
        assert [c["proposed_id"] for c in rec["checks"]] == ["R"] and rec["dropped"][0]["check"] == "S"


def test_an_engineering_claim_is_never_decided_by_another_kind_of_check():
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        state.write_json(x.root / "released.json", [{"path": "results/preds.csv"}])
        rd = {"id": "R", "kind": "RELEASED_DATA", "basis": "predictions", "claim_quote": CIFAR, "role": "target",
              "criterion": "stated", "covers": ["CIFAR-10-C"], "target": {"quote": CIFAR, "relation": "acc_a > acc_b"}}
        assert "compatibility test" in _refused(lambda: _plan(x, [rd], [_claim(claim_type="engineering")]))
        _plan(x, [rd], [_claim(claim_type="performance")])


def _gen_x(td: Path, check: dict):
    cfg, pid = _project_pages(td, DATA_PAGES)
    plan = {"checks": [check]}
    return type("X", (), {"root": td / pid, "paper": tasks._Ctx(cfg, pid).paper, "cfg": cfg, "sealed": lambda self, tid: plan,
                          "plan": lambda self: plan})()


BINDS = [{"kind": k, "impl_quote": q, "paper_quote": CIFAR} for k, q in
         (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "acc_a = 1"), ("comparison_target", "print("))]
SCRIPT = ("acc_a = 1\nfit()\nload()\nrng = np.random.default_rng(args.seed)\n"
          "print('REFEREE_RESULT', {'acc_a': acc_a, 'data_fingerprint': fp})\n")


def test_deviations_past_the_cap_are_refused_never_cut_unseen():
    """Sep-30 rerun 1, conformal Porto C4: 9-11 deviations were declared each round, the seal kept 8 silently, and
    the verifier's last REVISE asked for exactly the two it never saw: NOT_CHECKABLE."""
    with tempfile.TemporaryDirectory() as t:
        chk = {"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance",
               "target": {"quote": CIFAR, "relation": "acc_a > acc_b", "names": ["acc_a", "acc_b"]}}
        x = _gen_x(Path(t), chk)
        devs = [{"printed": "", "used": f"choice {i}", "why": "open", "changes_claim": False} for i in range(tasks.MAX_DEVIATIONS + 1)]
        g = {"fidelity": FID, "script": SCRIPT, "runs": 1, "outputs": ["acc_a", "acc_b"], "bindings": BINDS, "stochastic": True}
        assert "merge choices of one kind" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs}, final=False))
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs}, final=True)["refused"]
        ok = tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs[:tasks.MAX_DEVIATIONS]}, final=False)
        assert len(ok["deviations"]) == tasks.MAX_DEVIATIONS


def test_a_run_count_printed_as_a_word_is_the_papers_count():
    """Oct-06 GRACE C9: the paper prints "we run three independent simulation runs"; the seal required the digit 3 in the
    quoted sentence, so three attempts were refused ("runs > 1 needs runs_quote") and the check ended INCONCLUSIVE. A
    count written as a word is the same printed count; the refusal says which part failed."""
    from harness.evidence import count_in
    assert count_in("we run three independent simulation runs", 3) and count_in("over 5 random seeds", 5)
    assert count_in("averaged over one hundred runs", 100) and count_in("Twenty runs", 20)
    assert not count_in("we run three independent runs", 5) and not count_in("threefold", 3) and not count_in("3.5 runs", 3)
    with tempfile.TemporaryDirectory() as t:
        chk = {"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance",
               "target": {"quote": CIFAR, "relation": "acc_a > acc_b", "names": ["acc_a", "acc_b"]}}
        x = _gen_x(Path(t), chk)
        x.paper = Paper(DATA_PAGES + ["For each setting we run three independent simulation runs and report the mean."])
        g = {"fidelity": FID, "script": SCRIPT, "outputs": ["acc_a", "acc_b"], "bindings": BINDS, "stochastic": True}
        ok = tasks._seal_gen(x, "gen:C1.1", {**g, "runs": 3, "runs_quote": "we run three independent simulation runs"},
                             final=False)
        assert ok["runs"] == 3 and ok["runs_quote"].startswith("we run three"), ok
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "runs": 4, "runs_quote":
                                                               "we run three independent simulation runs"}, final=False))
        assert "does not print 4" in err, err
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "runs": 3, "runs_quote": "a sentence not in the paper"},
                                               final=False))
        assert "not found" in err or "re-found" in err, err


def test_readings_past_the_cap_are_refused_by_name_and_a_harness_rejection_is_not_the_authors_refusal():
    """Oct-05 changepoint C1: the script author declared 8 readings (a paper/code variant per factor); the seal kept the
    first 3 silently and then refused each deviation for naming "a reading the check does not declare" — one the author had
    declared. Three revisions could not find the fault, and the check ended NOT_CHECKABLE as "the script author refused",
    words the harness wrote. Readings past the cap are refused by name, a reading that was declared but not kept is said
    to be so, and a final answer the harness would not accept ends the check as a fault of this run, never as the
    author's refusal."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        lines = ["p = np.exp(r) / np.exp(r).sum()  # softmax of the chosen response",
                 "q = r / r.sum(axis=0)  # pooled over every position"]
        _checkout(td, pid, {"analyze.py": "import numpy as np\n" + "\n".join(lines) + "\n"})
        state.write_json(td / pid / "released.json", [{"path": "results/a.csv"}])
        plan = {"checks": [{"id": "C1", "kind": "RELEASED_DATA", "metric": "", "criterion": "stated",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        x = tasks._Ctx(cfg, pid)
        x.sealed = lambda tid: plan if tid == "plan" else None
        x.plan = lambda: plan
        script = 'd = open("results/a.csv").read()\nece = 1\nprint("REFEREE_RESULT", ece, "reading", "cohort")\n'
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("dataset", 'open("results/a.csv")'), ("metric", "ece = 1"), ("comparison_target", "print("))]
        readings = [{"name": "paper", "source": "paper", "quote": "We use generation of samples"},
                    {"name": "code", "source": "analyze.py", "quote": lines[0]},
                    {"name": "code_pooled", "source": "analyze.py", "quote": lines[1]},
                    {"name": "paper_seeds", "source": "paper", "quote": "We report the mean over 5 random seeds"}]
        dev = {"printed": "We use generation of samples", "used": "a softmax of the scores", "why": "positivity",
               "changes_claim": True, "reading": "paper_seeds"}
        g = {"script": script, "runs": 1, "metric": "ece", "outputs": ["ece"], "bindings": binds, "fidelity": FID,
             "readings": readings, "deviations": [dev]}
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.1", g, final=False))
        assert "4 readings" in err and "at most 3" in err and "paper_seeds" in err, err
        assert "does not declare" not in err, err                     # it was declared: the cap cut it, and says so
        assert "not kept" in err, err
        rec = tasks._seal_gen(x, "gen:C1.1", g, final=True)
        assert rec["refused"] and rec.get("by") == "harness", rec
        fine = tasks._seal_gen(x, "gen:C1.1", {**g, "readings": readings[:2], "deviations": [{**dev, "reading": "code"}]},
                               final=False)
        assert [r["name"] for r in fine["readings"]] == ["paper", "code"]
        # A reading that fails to re-find is not "undeclared" either: the deviation says it was declared but not kept.
        lost = [readings[0], readings[1], {**readings[2], "quote": "a line analyze.py never had"}]
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "readings": lost,
                                                               "deviations": [{**dev, "reading": "code_pooled"}]}, final=False))
        assert "code_pooled" in err and "not kept" in err and "does not declare" not in err, err
    # The step: a final answer the harness refused is a fault of this run (INCONCLUSIVE, reopenable), the harness's words.
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        c = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
             "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        _seal(cfg, pid, "plan", {"checks": [c], "central_claims": [{"quote": c["claim_quote"], "checks": ["B"],
                                                                    "claim_type": "theory", "scope": ["the loss"]}]}, td)
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0, 'premises_hold': 1})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        bad = {"script": script, "runs": 1, "outputs": ["violated"], "bindings": b,
               "readings": [{"name": "paper", "source": "paper", "quote": "The final loss is -0.52"}]}
        for _ in range(2):
            _refused(lambda: _seal(cfg, pid, "gen:C1.1", bad, td))
        _seal(cfg, pid, "gen:C1.1", bad, td)                            # the third attempt seals the harness's refusal
        tasks._plan(tasks._Ctx(cfg, pid))
        out = state.read_json(td / pid / "checks" / "C1" / "outcome.json")
        assert out["status"] == "INCONCLUSIVE" and "reason_by" not in out, out
        assert "script author refused" not in out["reason"] and "harness" in out["reason"], out
        assert "CERTIFICATE" in out["reason"], out                      # the errors it would not accept are shown


def test_released_data_needs_released_or_acquired_files():
    """Oct-05 GRACE follow-up: three RELEASED_DATA checks were planned for a checkout that releases no data files, with
    nothing to acquire; each script author could only refuse, and the whole follow-up round was spent on them. The plan
    seal refuses a RELEASED_DATA check with no released file and no acquisition."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        rd = {"id": "R", "kind": "RELEASED_DATA", "basis": "published_results", "claim_quote": CIFAR, "role": "target",
              "criterion": "stated", "covers": ["CIFAR-10-C"], "target": {"quote": CIFAR, "relation": "acc_a > acc_b"}}
        err = _refused(lambda: _plan(x, [rd], [_claim()]))
        assert "releases no data" in err and "acquire" in err, err
        rec = _plan(x, [rd], [_claim()], final=True)                    # last attempt: dropped, with the reason kept
        assert not rec["checks"] and "releases no data" in str(rec["dropped"]), rec
        state.write_json(x.root / "released.json", [{"path": "results/table2.csv"}])
        assert [c["proposed_id"] for c in _plan(x, [rd], [_claim()])["checks"]] == ["R"]


def test_a_released_record_file_can_be_a_reading_and_a_certificate_has_none():
    """No author repository: the released record's own README, notebook or column definition is the only
    implementation there is; it is kept as quote-only text and may be one of the readings."""
    with tempfile.TemporaryDirectory() as t:
        chk = {"id": "C1", "kind": "RELEASED_DATA", "metric": "acc", "target": {"quote": MMLU, "value": "61.4"}}
        x = _gen_x(Path(t), chk)
        src = x.root / "checks" / "C1" / "record_src" / "0"
        src.mkdir(parents=True)
        line = "acc = (df.answer == df.pred).mean()  # unparsed rows count as wrong"
        (src / "analysis.py").write_text(f"import pandas\n{line}\n", encoding="utf-8")
        (src / "unlisted.py").write_text(f"{line}\n", encoding="utf-8")         # in the folder, never acquired
        listing = [{"path": "0/analysis.py", "sha256": state.sha256((src / "analysis.py").read_bytes())}]
        state.write_json(x.root / "checks" / "C1" / "data.json", {"files": [{"path": "0/a.csv"}], "record_src": listing})
        script = ("rows = open('/work/data/0/a.csv').read()\nacc = 0.6\n"
                  "print('REFEREE_RESULT', {'acc': acc, 'reading': r, 'cohort': ids})\n")
        b = [{"kind": k, "impl_quote": q, "paper_quote": MMLU} for k, q in
             (("dataset", "open('/work/data/0/a.csv')"), ("metric", "acc = 0.6"), ("comparison_target", "print("))]
        paper = {"name": "paper", "source": "paper", "quote": "Our LLM monitor reaches 61.4 accuracy on MMLU"}
        g = {"fidelity": FID, "script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": b}
        rec = tasks._seal_gen(x, "gen:C1.1", {**g, "readings": [paper, {"name": "record", "source": "record:0/analysis.py",
                                                                         "quote": line}]}, final=False)
        assert [r["source"] for r in rec["readings"]] == ["paper", "record:0/analysis.py"]
        for bad in ({"quote": "acc = df.mean()  # something else entirely"}, {"source": "record:../../x.py"},
                    {"source": "record:0/missing.py"}, {"source": "record:0/unlisted.py"}):
            assert "released record" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "readings": [
                paper, {"name": "record", "source": "record:0/analysis.py", "quote": line, **bad}]}, final=False)), bad
        assert "same words" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "readings": [
            paper, {**paper, "name": "again"}]}, final=False))
        (src / "analysis.py").write_text(f"import pandas\n{line}\n# edited after the acquisition\n", encoding="utf-8")
        assert "released record" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "readings": [
            paper, {"name": "record", "source": "record:0/analysis.py", "quote": line}]}, final=False))   # sha256 changed
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        cert = {"id": "T", "kind": "CERTIFICATE", "claim_quote": THM, "statement_quote": THM, "role": "target", "covers": ["T1"],
                "readings": [{"name": "paper", "source": "paper", "quote": THM}, {"name": "other", "source": "paper", "quote": GPT}]}
        assert "CERTIFICATE states another reading" in _refused(lambda: _plan(x, [cert], [{
            "quote": THM, "claim_type": "theory", "checks": ["T"], "scope": ["T1"]}]))


def _chk(cid, **kw):
    base = {"id": cid, "kind": "RECONSTRUCTION", "role": "target", "state": "COMPLETED", "status": "RELATION_HOLDS",
            "values": [0.2, 0.3, 0.25], "deviations": [], "covers": ["CIFAR-10-C"], "data_changed": [], "evidence": "x",
            "reason": "", "data_identity": {}}
    return {**base, **kw}


def _cc(**kw):
    return {"quote": CIFAR, "page": 1, "claim_type": "performance", "checks": ["C1"], "scope": ["CIFAR-10-C"], "omitted": [], **kw}


def _decide(cc, checks):
    plan = {"central_claims": [cc], "checks": []}
    return report._central(plan, checks, [])[0]


def test_support_stands_only_on_the_requested_experiment_over_its_whole_scope():
    """Sep-30 rerun 2 transformer C1: RELATION_HOLDS with 5 of 20 datasets not matching the paper (and the record cut
    at 20 of 25 files) read SUPPORT_FOUND and lifted the headline; rerun 1: a sibling target that never ran was hidden
    and the claim read RAN_AS_SPECIFIED."""
    clean = _decide(_cc(), [_chk("C1")])
    assert clean["claim_status"] == "SUPPORT_FOUND" and clean["completion"]["experiment"] == "RAN_AS_SPECIFIED"
    moved = _decide(_cc(), [_chk("C1", data_changed=["dataset dp/flash: identity not affirmed"])])
    assert moved["claim_status"] == "READING_CHANGED" and moved["completion"]["experiment"] == "RAN_WITH_CHANGES"
    assert moved["completion"]["protocol_matched"] is False
    sib = _decide(_cc(checks=["C1", "C2"], scope=["CIFAR-10-C", "MMLU"]),
                  [_chk("C1"), _chk("C2", covers=["MMLU"], state="NOT_RUN", status="NOT_CHECKABLE", values=None, reason="refused")])
    assert sib["completion"]["experiment"] == "RAN_PARTIAL" and sib["completion"]["protocol_matched"] is False
    assert sib["claim_status"] == "PARTIAL_EVIDENCE" and sib["completion"]["scope_not_run"] == ["MMLU"]
    assert any(b["item"] == "C2" for b in sib["completion"]["not_run"])
    twin = _decide(_cc(checks=["C1", "C2"]), [_chk("C1"), _chk("C2", state="NOT_RUN", status="NOT_CHECKABLE", values=None,
                                                                    reason="refused")])   # same scope, one never ran
    assert twin["completion"]["experiment"] == "RAN_PARTIAL" and twin["completion"]["targets_not_run"] == ["C2"]
    fail = _decide(_cc(checks=["C1", "C2"], scope=["CIFAR-10-C", "MMLU"]),
                   [_chk("C1", status="RELATION_VIOLATED"), _chk("C2", covers=["MMLU"], state="NOT_RUN", status="BLOCKED",
                                                                 values=None, reason="RESOURCE BLOCKER")])
    assert fail["claim_status"] == "FAILURE_FOUND"                    # a failure found on what ran stands
    arith = _decide(_cc(claim_type="value"), [_chk("C1", kind="ARITHMETIC", status="ARITHMETIC_CONSISTENT")])
    assert arith["completion"]["experiment"] == "NOT_RUN" and arith["claim_status"] == "PARTIAL_EVIDENCE"
    lemma = _decide(_cc(), [_chk("C1", kind="CERTIFICATE", status="COUNTEREXAMPLE_FOUND")])   # a legacy plan's link
    assert lemma["claim_status"] == "NOT_CHECKED" and lemma["completion"]["supporting"][0]["check"] == "C1"
    sup = _decide(_cc(claim_type=""), [_chk("C1", role="supporting")])   # untyped: the strictest rules
    assert sup["claim_status"] == "NOT_CHECKED" and sup["completion"]["experiment"] == "NOT_RUN"
    th = _decide(_cc(claim_type="theory", scope=["T1"]), [_chk("C1", kind="CERTIFICATE", status="NO_VIOLATION_FOUND", covers=["T1"])])
    assert th["claim_status"] == "NO_VIOLATION_FOUND" and th["completion"]["experiment"] == "RAN_AS_SPECIFIED"


def test_data_the_run_never_compared_with_the_paper_is_not_the_requested_data():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        c = {"id": "C1", "kind": "RECONSTRUCTION", "acquire": [{"source": "s"}], "values": [1.0], "data_identity": {}}
        state.write_json(root / "checks" / "C1" / "data.json", {"sources": [{"source": "s", "admitted_files": 3}], "n_files": 3})
        assert any("no REFEREE_DATA" in g for g in report._data_changed(c, root))
        ok = {**c, "data_identity": {"cifar": {"matches": True}}}
        assert report._data_changed(ok, root) == []
        for v in (False, None, "no"):
            assert report._data_changed({**c, "data_identity": {"cifar": {"matches": v}}}, root)
        assert report._data_changed({**ok, "kind": "CERTIFICATE"}, root) == []


def test_support_words_are_earned_by_the_claim_not_only_by_the_check():
    """Probe: 'C1 reproduces the paper' passed the gate while C1's claim ran with changes; the honest 'Nothing here
    shows that any paper result was reproduced.' was withheld (a 30-character negation window)."""
    cc = lambda exp, st: {"quote": "q", "checks": ["C1"], "claim_status": st, "completion": {"experiment": exp}}
    chk = {"id": "C1", "kind": "RECONSTRUCTION", "status": "RELATION_HOLDS", "deviations": [], "data_changed": []}
    led = {"checks": [chk], "central_claims": [cc("RAN_WITH_CHANGES", "READING_CHANGED")], "concerns": []}
    assert report.unearned("C1 reproduces the paper's comparison.", led)
    led["central_claims"] = [cc("RAN_AS_SPECIFIED", "SUPPORT_FOUND")]
    assert not report.unearned("C1 reproduces the paper's comparison.", led)
    assert report.unearned("C1 reproduces it.", {**led, "checks": [{**chk, "data_changed": ["cut listing"]}]})
    assert not report.unearned("Nothing here shows that any paper result was reproduced.", led)
    assert report.unearned("Nothing failed, and the main result was reproduced.", led)      # another clause asserts it


def test_status_never_presents_an_interrupted_review_as_finished():
    """Sep-30 rerun 2 conformal: the ledger was written before plan:2 added three checks; `run.py status` printed it
    as CENTRAL_FAILURE_FOUND, 6 of 6 checks terminal, while 3 more were running. PPRM had no ledger and was not listed."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        root = td / pid
        state.write_json(root / "seals.json", {"plan": "x"})
        assert report.progress(root).startswith("IN PROGRESS")
        (root / "review.md").write_text("old", encoding="utf-8")
        state.write_json(root / "seals.json", {"plan": "x", "report": "y"})
        old = time.time() - 100
        os.utime(root / "review.md", (old, old))
        assert report.progress(root).startswith("IN PROGRESS")       # a seal written after the review
        os.utime(root / "review.md", None)
        assert report.progress(root) == "FINISHED"
        state.write_json(root / "checks" / "C9" / "outcome.json", {"status": "INCONCLUSIVE"})
        os.utime(root / "checks" / "C9" / "outcome.json", (time.time() + 50, time.time() + 50))
        assert report.progress(root).startswith("IN PROGRESS")       # an outcome newer than the review


def test_one_printed_definition_applied_differently_is_shown_side_by_side():
    """Sep-30 rerun 2 transformer: C1, C3 and C4 dropped the unparsed rows the paper says it discarded, C2, C5 and C7
    kept them, each re-finding the same sentence; nothing showed the disagreement."""
    printed = "The remaining 1% of cases were discarded while preparing the final graphs"
    d = lambda used: {"printed": printed, "page": 20, "used": used, "why": "", "changes_claim": False}
    checks = [{"id": "C1", "deviations": [d("unparsed rows dropped")]}, {"id": "C2", "deviations": [d("every row kept")]},
              {"id": "C3", "deviations": [d("unparsed rows dropped")]}]
    out = report.definition_choices(checks)
    assert len(out) == 1 and {tuple(u["checks"]) for u in out[0]["choices"]} == {("C1", "C3"), ("C2",)}
    assert report.definition_choices([checks[0], checks[2]]) == []          # the same choice twice: nothing to show
    assert report.definition_choices([{"id": "C1", "deviations": [d("a"), d("b")]}]) == []   # one check, not a disagreement


def test_a_reading_not_yet_decided_beside_a_decided_one_is_no_disagreement():
    """Replay of R0 conformal C5: 17 stages paired a decided reading with one that only more replicates could decide;
    counting them as READINGS_DIFFER would assert a disagreement nobody measured."""
    from harness.reconcile import READINGS_DIFFER, reconcile
    dec = lambda staged, **kw: reconcile("RECONSTRUCTION", "", [e[1] for e in staged], "", {}, True, True, "", "gain > 0",
                                         staged=staged, readings=["paper", "code"], **kw)
    holds = [["s", m, "paper"] for m in (0.3, 0.31, 0.29)]
    noisy = [["s", m, "code"] for m in (0.3, -0.2, 0.5)]
    fails = [["s", m, "code"] for m in (-0.3, -0.31, -0.29)]
    out = dec(holds + noisy)
    assert out["status"] == "INCONCLUSIVE" and out["readings_undecided_in"] == ["s"]
    assert dec(holds + fails)["status"] == READINGS_DIFFER                   # both decided, different findings
    point = reconcile("RELEASED_DATA", "0.22", [0.0135, 0.214], "", {}, False, True, "", staged=[
        ["s", 0.0135, "paper"], ["s", 0.214, "code"]], readings=["paper", "code"], deterministic=True)
    assert point["status"] == READINGS_DIFFER                                 # values apart beyond the printed precision


# --- 2026-10-01 audit: readings, replicate independence, proportions, certificates -----------------
# The decision kernel's statistics: readings per stage, run-time independence of replicates, proportions,
# certificates and the result schema (invariants 15, 16, 17, 19, 20, 21, 22). Each check fails if a guarantee breaks.
# Run: python tests/_t_stats.py   (or pytest).

def _rows(outs, stage="a", reading="", fps=None, trials=None, seeds=None):
    """Per-run detail as execute.result_detail records it: one row per result line."""
    return [{"seed": s, "stage": stage, "reading": reading, "out": o, "trials": dict(trials or {}),
             "data_fp": (fps or {}).get(s)} for s, o in zip(seeds or range(len(outs)), outs)]


def _rel(rel, detail, margins, stage="a", rng=None, det=False, kind="RECONSTRUCTION"):
    staged = [[stage, m] for m in margins]
    return reconcile(kind, "", margins, "", {}, kind == "RECONSTRUCTION", True, "", rel, staged=staged, detail=detail,
                     rng=rng, deterministic=det)


def _distinct(n):
    return {i: f"fp{i}" for i in range(n)}


# --- A. readings ---------------------------------------------------------------------------
def test_readings_are_decided_per_stage_over_the_union_of_stages():
    """Rerun compact C5: chain 1.294 vs trimmed 1.144 against a printed 1.0 (+-0.05), one stage each,
    read 'every reading gives the same finding'. New3 compact C5: 21 of 60 stages differ between readings
    while both aggregates read RELATION_VIOLATED; only the first reading's stages were shown."""
    p, c = [1.0, 1.3, 1.58], [1.1, 1.15, 1.2]
    st = [["trip", v, "chain"] for v in p] + [["trip", v, "trim"] for v in c]
    r = reconcile("RECONSTRUCTION", "1.0", [e[1] for e in st], "", {}, True, True, "", staged=st, readings=["chain", "trim"])
    assert r["status"] == READINGS_DIFFER and "trip" in r["reason"], r["reason"]
    assert abs(r["readings"]["chain"]["stages"]["trip"]["reproduced"] - 1.293333) < 1e-5
    assert abs(r["readings"]["trim"]["stages"]["trip"]["reproduced"] - 1.15) < 1e-9
    assert r["stages"]["trip"]["status"] == READINGS_DIFFER                       # the cross-reading stage table
    # two aggregates that agree while their stages do not
    rel = "x >= 0"
    dec = lambda staged, **kw: reconcile("RELEASED_DATA", "", [e[1] for e in staged], "", {}, False, True, "", rel,
                                         staged=staged, readings=["paper", "code"], deterministic=True, **kw)
    hidden = [["s1", 1, "paper"], ["s2", 1, "paper"], ["s3", -1, "paper"],
              ["s1", 1, "code"], ["s2", -1, "code"], ["s3", 1, "code"]]
    h = dec(hidden)
    # Oct-06 label ranking C4: both readings fail (each in some stage), so the claim fails under every reading — the
    # failure stands (and is audited) with the stages where they differ named, never read as an open interpretation.
    assert h["status"] == "RELATION_VIOLATED" and h["readings_differ_in"] == ["s2", "s3"], h
    assert "every reading" in h["reason"] and "s2" in h["reason"] and "s3" in h["reason"] and "s1" not in h["reason"], h
    assert h["readings"]["paper"]["stages"]["s3"]["status"] == "RELATION_VIOLATED"
    assert h["readings"]["code"]["stages"]["s2"]["status"] == "RELATION_VIOLATED"   # every reading's own table is kept
    assert h["stages"]["s1"]["status"] == "RELATION_HOLDS" and h["stages"]["s2"]["status"] == READINGS_DIFFER
    # a reading missing in a stage the other reading measured: undecided, the stage named
    miss = dec([["a", 2, "paper"], ["b", 2, "paper"], ["a", 2, "code"]])
    assert miss["status"] == "INCONCLUSIVE" and miss["missing_readings"] == {"b": ["code"]}, miss
    # agreement: the shared status stands, both tables are carried
    same = dec([["a", 2, "paper"], ["b", 1, "paper"], ["a", 3, "code"], ["b", 0.5, "code"]])
    assert same["status"] == "RELATION_HOLDS" and set(same["readings"]) == {"paper", "code"}
    assert same["readings"]["code"]["stages"]["b"]["margin"] == 0.5 and same["stages"]["b"]["status"] == "RELATION_HOLDS"
    # a point value per stage: equal statuses, values apart beyond the printed precision
    pts = reconcile("RELEASED_DATA", "0.32", [0.30, 0.34], "", {}, False, True, "", staged=[["s", 0.30, "a"], ["s", 0.34, "b"]],
                    readings=["a", "b"], deterministic=True)
    assert pts["status"] == READINGS_DIFFER and "s" in pts["stages"], pts
    close = reconcile("RELEASED_DATA", "0.3", [0.30, 0.31], "", {}, False, True, "", staged=[["s", 0.30, "a"], ["s", 0.31, "b"]],
                      readings=["a", "b"], deterministic=True)
    assert close["status"] == "RESOLVED_VERIFIED", close                      # within the printed 0.05
    # unstaged readings keep their own top-level values
    un = reconcile("RELEASED_DATA", "0.22", [0.0135, 0.214], "", {}, False, True, "",
                   staged=[["", 0.0135, "paper"], ["", 0.214, "code"]], readings=["paper", "code"], deterministic=True)
    assert un["status"] == READINGS_DIFFER and un["readings"]["paper"]["reproduced"] == 0.0135
    # a stage not completed under both readings is partial, not a missing reading; the completed stages still compare
    part = dec([["a", 1, "paper"], ["a", -1, "code"]], stage_errors={"b": "exit 1"}, failed={"0": "exit 1"})
    assert part["status"] == "PARTIAL" and part["status_on_completed"] == READINGS_DIFFER, part


def test_undeclared_reading_tags_are_never_pooled():
    """Two definitions printed under `reading` by a check that declared none were pooled into one sample
    (0.0135 and 0.214 vs 0.22 -> RESOLVED_VERIFIED; 2 readings x 3 seeds -> a t-test over n=6)."""
    two = [["", 0.0135, "paper"], ["", 0.214, "code"]]
    r = reconcile("RECONSTRUCTION", "0.22", [0.0135, 0.214], "", {}, True, True, "", staged=two)
    assert r["status"] == "INCONCLUSIVE" and "reading" in r["reason"] and r.get("n") != 2, r
    six = [["", v, rd] for rd, v in (("paper", 0.30), ("code", 0.31)) for _ in range(3)]
    r6 = reconcile("RECONSTRUCTION", "", [e[1] for e in six], "", {}, True, True, "", "x > 0", staged=six)
    assert r6["status"] == "INCONCLUSIVE" and r6.get("n") != 6, r6
    other = reconcile("RELEASED_DATA", "0.22", [0.22, 0.22, 0.5], "", {}, False, True, "",
                      staged=[["", 0.22, "paper"], ["", 0.22, "code"], ["", 0.5, "other"]], readings=["paper", "code"],
                      deterministic=True)
    assert other["status"] == "INCONCLUSIVE" and "other" in other["reason"], other
    chk = {"kind": "RECONSTRUCTION", "metric": "m", "stochastic": False}
    out = 'REFEREE_RESULT {"m": 0.0135, "reading": "paper"}\nREFEREE_RESULT {"m": 0.214, "reading": "code"}'
    assert any("`reading`" in d and "declares no readings" in d for d in execute.result_schema({"stdout": out}, chk))
    two_defs = {"kind": "RELEASED_DATA", "metric": "m", "readings": [{"name": "paper"}, {"name": "code"}]}
    ints = 'REFEREE_RESULT {"m": 1, "reading": "paper", "cohort": 200}\nREFEREE_RESULT {"m": 2, "reading": "code", "cohort": 200}'
    assert any("not a list of item ids" in d for d in execute.result_schema({"stdout": ints}, two_defs))
    apart = ints.replace('"cohort": 200}\nREFEREE_RESULT {"m": 2, "reading": "code", "cohort": 200',
                         '"cohort": 200}\nREFEREE_RESULT {"m": 2, "reading": "code", "cohort": 300')
    assert execute.cohort_mismatch(apart, two_defs) == ["(no stage)"]       # still compared, never blind


def test_a_certificate_reading_tag_never_makes_a_counterexample():
    out = ('REFEREE_RESULT {"violated": 0, "premises_hold": 1, "reading": "paper"}\n'
           'REFEREE_RESULT {"violated": 1, "premises_hold": 1, "reading": "code"}')
    rows = execute.cert_rows(out)
    assert rows[1]["reading"] == "code" and "reading" not in execute.cert_rows('REFEREE_RESULT {"violated": 0}')[0]
    r = reconcile("CERTIFICATE", "", [], "", {}, False, True, "", cert=rows, readings=["paper", "code"])
    assert r["status"] != "COUNTEREXAMPLE_FOUND" and r["status"] == "VIOLATION_UNDER_CHANGED_READING", r
    lit = [{"violated": 0, "premises": 1, "literal": "fails", "reading": "code", "exact": False}]
    assert certificate(lit, False, False)["status"] != "COUNTEREXAMPLE_FOUND"
    plain = [{"violated": 1, "premises": 1, "literal": None, "exact": False}]
    assert certificate(plain, False, False)["status"] == "COUNTEREXAMPLE_FOUND"     # an untagged instance still decides


def test_certificate_counts_are_instances_and_a_finished_one_is_redecided_from_its_records():
    """Oct-06 label ranking C2: 20 instances, each printed three result lines (the printed reading and two named
    readings), and every line carried `literal`; the page said "60 exact cases ... as printed: 24 holds, 36 fails" where
    12 of 20 instances fail as printed. Counts are of instances (the printed reading's lines), with the result lines and
    readings beside them. A finished certificate is re-decided from its recorded run outputs under the current kernel
    (no model, no container), the earlier outcome kept beside it."""
    inst = lambda fails, rd="": {"violated": int(bool(fails)) if not rd else 1, "premises": 1, "exact": False,
                                 "literal": "fails" if fails else "holds", **({"reading": rd} if rd else {})}
    rows = [x for i in range(20) for x in (inst(i < 12), inst(i < 12, "fixed"), inst(i < 12, "def5"))]
    r = certificate(rows, True, False)
    assert r["status"] == "COUNTEREXAMPLE_FOUND" and r["n"] == 60, r
    assert r["instances"] == 20 and r["literal"] == {"holds": 8, "fails": 12, "undefined": 0, "premise_not_met": 0}, r
    assert r["readings_per_instance"] == 3 and r["admissible_instances"] == 20, r
    tagged = [x for i in range(8) for x in ({"violated": 0, "premises": 1, "literal": "undefined", "reading": "zero_based"},
                                            {"violated": 0, "premises": 1, "literal": "undefined", "reading": "sigmoid"})]
    t = certificate(tagged, True, False)                     # every line tagged: one reading's lines are the instances
    assert t["instances"] == 8 and t["literal"]["undefined"] == 8, t
    from harness import reviewer
    page = "\n".join(reviewer.result_rows({"kind": "CERTIFICATE", **r, "values": [0] * 60}))
    assert "20 cases" in page and "60 result lines" in page and "12 fail" in page and "36" not in page, page
    with tempfile.TemporaryDirectory() as tmp:
        cfg, pid, root = _ready(Path(tmp))
        cdir = root / "checks" / "C1"
        cdir.mkdir(parents=True, exist_ok=True)
        state.write_json(cdir / "check.json", {"id": "C1", "kind": "CERTIFICATE", "script_sha256": "s1", "deviations": [
            {"changes_claim": True, "used": "x"}], "step": ""})
        old = {"check": "C1", "kind": "CERTIFICATE", "status": "COUNTEREXAMPLE_FOUND", "n": 60, "literal": {"holds": 24, "fails": 36},
               "values": [0] * 60, "runs": 20, "authorized": True, "execution": {"runs_planned": 20, "runs_ended": 20}}
        state.write_json(cdir / "outcome.json", old)
        line = lambda d: "REFEREE_RESULT " + json.dumps(d)
        for seed in range(20):
            out = "\n".join(line({"violated": v, "premises_hold": 1, "literal": lit, **({"reading": rd} if rd else {})})
                            for v, lit, rd in ((int(seed < 12), "fails" if seed < 12 else "holds", ""),
                                               (1, "fails" if seed < 12 else "holds", "fixed")))
            state.append_jsonl(root / "execution.jsonl", {"target": "C1", "mode": "evidence", "seed": seed, "script_sha256": "s1",
                                                          "returncode": 0, "stdout": out, "stderr": "", "timed_out": False})
        state.append_jsonl(root / "execution.jsonl", {"target": "C1", "mode": "try", "seed": 0, "script_sha256": "s1",
                                                      "returncode": 0, "stdout": line({"violated": 1, "premises_hold": 1}),
                                                      "stderr": ""})                       # a draft run decides nothing
        res = execute.redecide(cfg, pid, "C1", "certificate counts are of instances (test)")
        new = state.read_json(cdir / "outcome.json")
        assert res["status"] == new["status"] == "COUNTEREXAMPLE_FOUND" and new["instances"] == 20, (res, new)
        assert new["literal"]["fails"] == 12 and new["redecided"]["was"]["literal"] == {"holds": 24, "fails": 36}, new
        assert (cdir / "outcome.redecided.1.json").exists() and new["runs"] == 20 and new["execution"]["runs_ended"] == 20
        assert "error" in execute.redecide(cfg, pid, "C9", "no such check")


def test_a_finished_script_check_is_redecided_from_its_saved_seeds():
    """After a fix of the decision rules alone, a finished RELEASED_DATA / RECONSTRUCTION check is decided again from
    the seeds it saved (seeds.jsonl), by the very function a finished check uses: no model, no container, nothing re-run.
    The earlier outcome is kept beside it; fields that are not the decision (execution, data identity) stay."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, root = _ready(Path(t))
        cdir = root / "checks" / "C1"
        cdir.mkdir(parents=True, exist_ok=True)
        chk = {"id": "C1", "kind": "RELEASED_DATA", "script_sha256": "s", "runs": 1, "stochastic": None, "deviations": [],
               "target": {"relation": "x >= 0", "names": ["x"]}, "readings": [{"name": "paper", "source": "paper"},
                                                                         {"name": "code", "source": "a.py"}]}
        state.write_json(cdir / "check.json", chk)
        staged = [["s1", 1, "paper"], ["s2", 1, "paper"], ["s3", -1, "paper"], ["s1", 1, "code"], ["s2", -1, "code"], ["s3", 1, "code"]]
        state.append_jsonl(cdir / "seeds.jsonl", {"key": "s", "seed": 0, "values": [e[1] for e in staged], "staged": staged,
                                                  "seconds": 5, "error": "", "units": ["s1", "s2", "s3"], "detail": []})
        old = {"check": "C1", "kind": "RELEASED_DATA", "status": READINGS_DIFFER, "authorized": True, "values": [1] * 6,
               "data_identity": {"d": {"matches": True}}, "execution": {"runs_planned": 1, "runs_ended": 1}}
        state.write_json(cdir / "outcome.json", old)
        res = execute.redecide(cfg, pid, "C1", "every failing reading is a failure (test)")
        new = state.read_json(cdir / "outcome.json")
        assert res["status"] == new["status"] == "RELATION_VIOLATED" and new["readings_differ_in"] == ["s2", "s3"], new
        assert new["data_identity"] == old["data_identity"] and new["redecided"]["was"]["status"] == READINGS_DIFFER
        assert (cdir / "outcome.redecided.1.json").exists()


def _replay():
    spec = importlib.util.spec_from_file_location("replay", Path(__file__).resolve().parent.parent / "tools" / "replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_dir(root: Path, cid: str, check: dict, rows: list, recs: list, outcome: dict) -> Path:
    cdir = root / "checks" / cid
    cdir.mkdir(parents=True, exist_ok=True)
    state.write_json(cdir / "check.json", {"id": cid, **check})
    state.write_json(cdir / "outcome.json", outcome)
    (cdir / "seeds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with (root / "execution.jsonl").open("a", encoding="utf-8") as f:
        f.writelines(json.dumps({"target": cid, "mode": "evidence", "returncode": 0, "stderr": "", "seconds": 5, **r}) + "\n"
                     for r in recs)
    return cdir


def test_replay_decides_by_the_same_path_as_execute():
    """tools/replay.py passed no `readings` cohort mismatch and counted only the first reading's stages."""
    replay = _replay()
    with tempfile.TemporaryDirectory() as t:
        root = Path(t) / "p"
        root.mkdir()
        defs = [{"name": "paper", "source": "paper"}, {"name": "code", "source": "a.py"}]
        moved = ('REFEREE_RESULT {"ece": 0.0135, "reading": "paper", "cohort": ["m1", "m2"]}\n'
                 'REFEREE_RESULT {"ece": 0.214, "reading": "code", "cohort": ["m2", "m3"]}')
        chk = {"kind": "RELEASED_DATA", "metric": "ece", "printed": "0.22", "runs": 1, "script_sha256": "s1", "readings": defs}
        staged = execute.staged_values(moved, "", "ece")
        cdir = _check_dir(root, "C1", chk, [{"key": "s1", "seed": 0, "values": [0.0135, 0.214], "staged": staged,
                                             "seconds": 5, "error": "", "stage_errors": {}, "units": [], "schema": [],
                                             "cohort_mismatch": ["(no stage)"], "detail": execute.result_detail(moved, 0)}],
                          [{"script_sha256": "s1", "seed": 0, "stdout": moved}], {"status": READINGS_DIFFER})
        got = replay.replay(cdir)
        st = {}
        execute.reuse_checkpoints(root, cdir, {"id": "C1", **chk}, st)
        want = execute._reconciled({"id": "C1", **chk}, st, True, "", failure="")
        assert got["new_status"] == want["status"] == "INCONCLUSIVE", (got["new_status"], want["status"])
        # identical reruns are counted under every reading, not under the first one only
        same = lambda s: 'REFEREE_RESULT {"stage": "a", "x": 0.5, "reading": "paper", "cohort": [1]}\n' \
                         'REFEREE_RESULT {"stage": "a", "x": 0.7, "reading": "code", "cohort": [1]}'
        chk2 = {"kind": "RECONSTRUCTION", "metric": "x", "printed": "0.5", "runs": 3, "script_sha256": "s2", "readings": defs,
                "stochastic": True}
        rows = [{"key": "s2", "seed": s, "values": [0.5, 0.7], "staged": execute.staged_values(same(s), "", "x"),
                 "seconds": 5, "error": "", "stage_errors": {}, "units": [], "schema": [], "cohort_mismatch": [],
                 "detail": execute.result_detail(same(s), s)} for s in range(3)]
        c2 = _check_dir(root, "C2", chk2, rows, [{"script_sha256": "s2", "seed": s, "stdout": same(s)} for s in range(3)],
                        {"status": "INCONCLUSIVE"})
        g2 = replay.replay(c2)
        assert g2["identical_stages"] == 2, g2


# --- B. independence of replicates ---------------------------------------------------------
def test_static_seed_flow_never_makes_repeated_lines_replicates():
    """Synthetic: stage B draws from default_rng(0) inside a script whose --seed seeds stage A. The static
    flow is True for the whole script, so 6 identical duplicates passed the sign test (n_independent 6) and
    0/400 was pooled to 0/2400."""
    same = _rows([{"gain": 0.2}] * 6)
    a = independence.assess(same, "a", "", True)
    assert not a["independent"] and a["state"] == "unproven" and "static" in a["basis"], a
    r = _rel("gain > 0", same, [0.2] * 6, rng=True)["stages"]["a"]
    assert r["status"] == "INCONCLUSIVE" and r["n_independent"] == 1, r
    b = _rel("fa < delta", _rows([{"fa": 0.0, "delta": 0.005}] * 6, trials={"fa": 400}), [0.005] * 6, rng=True)["stages"]["a"]
    assert b["status"] == "INCONCLUSIVE" and b["trials"] == 400 and b["n_independent"] == 1, b
    twin = _rows([{"gain": 0.2}] * 6, fps={i: "same" for i in range(6)})     # identical lines, identical fingerprints
    assert independence.assess(twin, "a", "", True)["state"] == "identical"
    assert _rel("gain > 0", twin, [0.2] * 6, rng=True)["stages"]["a"]["n_independent"] == 1
    shown = _rows([{"gain": 0.2}] * 6, fps=_distinct(6))                     # the run's own evidence decides
    d = _rel("gain > 0", shown, [0.2] * 6, rng=False)["stages"]["a"]
    assert d["status"] == "RELATION_HOLDS" and d["n_independent"] == 6, d
    # a stochastic reconstruction owes a data fingerprint on every result line
    chk = {"kind": "RECONSTRUCTION", "stochastic": True, "metric": "acc", "test": "performance"}
    fp = lambda out: [x for x in execute.result_schema({"stdout": out}, chk) if "data_fingerprint" in x]
    assert fp('REFEREE_RESULT {"acc": 0.8}\nREFEREE_RESULT {"acc": 0.7, "data_fingerprint": "ab"}')
    assert not fp('REFEREE_RESULT {"acc": 0.8, "data_fingerprint": "ab"}')
    assert not [x for x in execute.result_schema({"stdout": 'REFEREE_RESULT {"acc": 0.8}'}, {**chk, "stochastic": False})
                if "data_fingerprint" in x]
    assert not [x for x in execute.result_schema({"stdout": 'REFEREE_RESULT {"acc": 0.8}'}, {**chk, "test": "compatibility"})
                if "data_fingerprint" in x]
    with tempfile.TemporaryDirectory() as t:        # the harness's draft run is held to it too
        root = Path(t)
        plan = {"checks": [{"id": "C1", "kind": "RECONSTRUCTION", "metric": "", "test": "performance"}], "central_claims": []}
        seals = {}
        for tid, obj in (("plan", plan), ("gen:C1.1", {"metric": "acc", "stochastic": True})):
            path = root / "sealed" / f"{tasks._safe(tid)}.json"
            state.write_json(path, obj)
            seals[tid] = state.sha256(path.read_bytes())
        state.write_json(root / "seals.json", seals)
        assert execute._smoke_check(root, "C1", 1).get("stochastic") is True


def test_a_fixed_dataset_with_seeded_training_is_different_runs():
    """Acquired data are fixed (one fingerprint), training is seeded: the other outputs differ, so the runs differ."""
    fixed = _rows([{"gain": 0.2, "loss": v} for v in (.31, .29, .35, .30, .33, .28)], fps={i: "same" for i in range(6)})
    assert independence.assess(fixed, "a", "", None)["state"] == "different"
    r = _rel("gain > 0", fixed, [0.2] * 6)["stages"]["a"]
    assert r["status"] == "RELATION_HOLDS" and r["n_independent"] == 6, r


def test_a_compared_proportion_is_a_proportion_and_pooled_only_when_seeds_agree():
    """Rerun compact C6: `binomial: {"margin": 20000}` on margin = coverage - phi; a seed's margin -0.0229 was
    counted as -458 events and 10 seeds pooled to 10685/200000 while the seeds spread by ~0.05."""
    mg = [0.00555, 0.09835, 0.06935, 0.1398, 0.0388, 0.0952, -0.0229, 0.04245, -0.0154, 0.08305]
    neg = _rel("margin >= 0", _rows([{"margin": m} for m in mg], fps=_distinct(10), trials={"margin": 20000}), mg)["stages"]["a"]
    assert "events" not in neg and neg["rule"].startswith("mean paired margin"), neg
    chk = {"kind": "RECONSTRUCTION", "stochastic": True, "target": {"relation": "margin >= 0"}}
    line = 'REFEREE_RESULT {"margin": -0.0229, "binomial": {"margin": 20000}, "data_fingerprint": "x"}'
    assert any("binomial" in d for d in execute.result_schema({"stdout": line}, chk))
    # every value a whole count of its trials, but the seeds disagree far beyond binomial noise: not pooled
    cov = [0.70, 0.66, 0.74]
    het = _rel("coverage >= phi", _rows([{"coverage": c, "phi": 0.69} for c in cov], fps=_distinct(3),
                                         trials={"coverage": 20000}), [c - 0.69 for c in cov])["stages"]["a"]
    assert het["status"] == "INCONCLUSIVE" and "events" not in het and het.get("binomial_not_pooled"), het
    pos = [0.20555, 0.25, 0.18, 0.22, 0.24, 0.19]
    p5 = _rel("margin >= 0", _rows([{"margin": m} for m in pos], fps=_distinct(6), trials={"margin": 20000}), pos)["stages"]["a"]
    assert "events" not in p5 and p5["status"] == "RELATION_HOLDS" and p5["rule"].startswith("mean paired margin"), p5
    # seeds that agree are pooled; zero events in every seed agree
    hom = _rel("fa < delta", _rows([{"fa": k / 400, "delta": 0.25} for k in (2, 3, 2)], fps=_distinct(3),
                                   trials={"fa": 400}), [0.25 - k / 400 for k in (2, 3, 2)])["stages"]["a"]
    assert hom["status"] == "RELATION_HOLDS" and hom["events"] == 7 and hom["trials"] == 1200, hom
    zero = _rel("fa < delta", _rows([{"fa": 0.0, "delta": 0.25}] * 3, fps=_distinct(3), trials={"fa": 400}), [0.25] * 3)
    assert zero["stages"]["a"]["trials"] == 1200 and zero["stages"]["a"]["status"] == "RELATION_HOLDS"
    third = _rel("fa < delta", _rows([{"fa": 1 / 3, "delta": 0.5}] * 3, fps=_distinct(3), trials={"fa": 400}),
                 [0.5 - 1 / 3] * 3)["stages"]["a"]
    assert "events" not in third, third                                       # 133.3 of 400 is not a count


def test_checkpointed_seeds_without_detail_are_rebuilt_or_missing_never_identical():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t) / "p"
        root.mkdir()
        out = lambda s: f'REFEREE_PROGRESS {{"units": ["a"]}}\nREFEREE_RESULT {{"stage": "a", "gain": 0.2, "n": {s}}}'
        chk = {"kind": "RECONSTRUCTION", "stochastic": True, "seed_flow": True, "runs": 3, "script_sha256": "s1",
               "target": {"relation": "gain > 0"}}
        rows = [{"key": "s1", "seed": s, "values": [0.2], "staged": [["a", 0.2]], "seconds": 7, "error": "",
                 "stage_errors": {}, "units": ["a"]} for s in range(3)]                  # written before `detail` existed
        cdir = _check_dir(root, "C1", chk, rows, [{"script_sha256": "s1", "seed": s, "stdout": out(s)} for s in range(2)],
                          {"status": "INCONCLUSIVE"})
        st = {}
        execute.reuse_checkpoints(root, cdir, {"id": "C1", **chk}, st)
        assert [r for r in st["detail"] if r["seed"] == 1] == execute.result_detail(out(1), 1)
        assert any(r.get("missing") and r["seed"] == 2 for r in st["detail"]), st["detail"]
        res = execute._reconciled({"id": "C1", **chk}, st, True, "", failure="")["stages"]["a"]
        assert res["status"] == "INCONCLUSIVE" and res.get("n_independent") != 1 and "did not vary" not in res["reason"], res
        assert "seed" in res["reason"] and "2" in res["reason"]
        assert st["seed_seconds"] == {"0": 7, "1": 7, "2": 7}
        with (root / "execution.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"target": "C1", "mode": "evidence", "returncode": 0, "stderr": "", "script_sha256": "s1",
                                "seed": 2, "stdout": out(2)}) + "\n")
        st = {}
        execute.reuse_checkpoints(root, cdir, {"id": "C1", **chk}, st)
        res = execute._reconciled({"id": "C1", **chk}, st, True, "", failure="")["stages"]["a"]
        assert res["n_independent"] == 3 and res.get("needs_replicates") == 6, res


def test_the_extension_to_six_counts_the_time_already_spent():
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects, cfg.parallel, cfg.check_budget_s = Path(t), 2, 7200
        state.write_json(Path(t) / ".gpu.json", False)
        check = {"kind": "RECONSTRUCTION", "stochastic": True, "target": {"relation": "gain > 0"}, "runs": 3}
        st = lambda pilot, **kw: {"values": [0.2] * 3, "staged": [["a", 0.2]] * 3, "seed": 3, "pilot_s": pilot, "width": 2,
                                  "detail": _rows([{"gain": 0.2, "n": s} for s in range(3)]), **kw}
        assert execute._extension(cfg, check, st(100), 3) == 6
        # three more at 2000 s, two at a time, fit alone (3000 s); with the 6000 s already spent they do not
        assert execute._extension(cfg, check, st(2000), 3) == 0
        # the recorded seconds of the seeds count, not the pilot's alone
        assert execute._extension(cfg, check, st(1000, seed_seconds={"0": 1000, "1": 3000, "2": 3000}), 3) == 0
        assert execute._extension(cfg, check, st(1000, seed_seconds={"0": 1000, "1": 1000, "2": 1000}), 3) == 6
        # 2026-10-02: a refused extension leaves a record — a setting, not the evidence, kept it undecided
        s = st(2000)
        assert execute._extension(cfg, check, s, 3) == 0 and s["extension_refused"]["setting"] == "SH_CHECK_BUDGET_S", s
        pr = execute.protocol({**check, "runs_source": "referee_floor"}, s, "rule")
        assert "SH_CHECK_BUDGET_S" in pr["extension_refused"] and "a setting of this run" in pr["extension_refused"], pr


def test_independent_replicates_of_one_point_value_are_decided_by_an_exact_test():
    """Six independent replicates at 0.80 vs a printed 0.95 stayed INCONCLUSIVE ('a deterministic run')."""
    d6 = _rows([{"acc": 0.8}] * 6, stage="", fps=_distinct(6))
    pt = lambda printed, n, detail, **kw: reconcile("RECONSTRUCTION", printed, [0.8] * n, "", {}, True, True, "",
                                                    detail=detail, **kw)
    six = pt("0.95", 6, d6)
    assert six["status"] == "FAILED_REPRODUCTION" and six["n_independent"] == 6 and "sign test" in six["rule"], six
    three = pt("0.95", 3, d6[:3])
    assert three["status"] == "INCONCLUSIVE" and three.get("needs_replicates") == 6 and "sign test" in three["reason"], three
    assert pt("0.80", 3, d6[:3])["status"] == "RESOLVED_VERIFIED"
    one = pt("0.95", 3, _rows([{"acc": 0.8}] * 3, stage=""))
    assert one["status"] == "INCONCLUSIVE" and one["n_independent"] == 1 and "deterministic run" not in one["reason"], one
    assert "one measurement" in one["reason"]
    db = _rows([{"acc": 0.8}] * 3, stage="", fps=_distinct(3), trials={"acc": 100})
    cp = pt("0.95", 3, db, metric="acc")
    assert cp["status"] == "FAILED_REPRODUCTION" and cp["trials"] == 300, cp
    assert pt("0.82", 3, db, metric="acc")["status"] == "RESOLVED_VERIFIED"
    # outside the pooled interval [0.750, 0.844] but inside one replicate's own [0.708, 0.873]: noise, not a failure
    near = pt("0.86", 3, db, metric="acc")
    assert near["status"] == "INCONCLUSIVE" and near["one_run_ci"][1] > 0.86, near
    cfg = state.Config()
    st = {"values": [0.8] * 3, "staged": [["", 0.8]] * 3, "detail": d6[:3], "seed": 3, "pilot_s": 10}
    assert execute._extension(cfg, {"kind": "RECONSTRUCTION", "stochastic": True, "printed": "0.95", "runs": 3}, st, 3) == 6


def test_a_time_like_output_is_a_whole_token():
    differ = lambda key: independence.assess(_rows([{"fa": 0.0, key: v} for v in (3, 4, 5)]), "a", "", None)["state"]
    for key in ("intersection_size", "n_timesteps", "consecutive_alarms"):
        assert differ(key) == "different", key
    for key in ("seconds", "elapsed_s", "wall_time", "time", "runtime", "trainTime", "fit_secs"):
        assert differ(key) != "different", key
    # unresolved: `detection_time` (a delay in samples) shares the whole token `time` with a wall clock; it stays ignored,
    # the conservative side (one wall-clock output must never turn duplicates into "different runs")
    assert differ("detection_time") != "different"


# --- C. certificates -----------------------------------------------------------------------
def test_a_certificate_instance_is_admissible_only_when_its_premises_were_evaluated_and_hold():
    row = lambda v, p, lit=None, **kw: {"violated": v, "premises": p, "literal": lit, "exact": False, **kw}
    unsaid = certificate([row(0, None)] * 2, False, False)
    assert unsaid["status"] == "INCONCLUSIVE" and unsaid["admissible"] == 0, unsaid
    assert reconcile("CERTIFICATE", "", [0, 0], "", {}, False, True, "")["status"] == "INCONCLUSIVE"
    mixed = certificate([row(0, 1), row(0, None)], False, False)
    assert mixed["status"] == "NO_VIOLATION_FOUND" and "all 1 admissible" in mixed["reason"], mixed
    fuzzy = row(0, 1, "fails", lhs=1.0, rhs=1.0 + 1e-12)
    f = certificate([fuzzy], False, False)
    assert f["status"] == "INCONCLUSIVE" and f.get("below_precision") == 1, f
    assert certificate([{**fuzzy, "exact": True}], False, False)["status"] == "COUNTEREXAMPLE_FOUND"
    assert certificate([row(0, 1, "fails", lhs=1.0, rhs=2.0)], False, False)["status"] == "COUNTEREXAMPLE_FOUND"
    defects = execute.result_schema({"stdout": 'REFEREE_RESULT {"violated": 0}'}, {"kind": "CERTIFICATE"})
    assert any("premises_hold" in d for d in defects), defects
    assert execute.result_schema({"stdout": 'REFEREE_RESULT {"violated": 0, "premises_hold": 1}'}, {"kind": "CERTIFICATE"}) == []


# --- D. data identity ----------------------------------------------------------------------
def test_a_check_that_acquired_data_owes_a_data_identity_line():
    chk = {"kind": "RECONSTRUCTION", "stochastic": False, "metric": "acc", "acquire": [{"url": "https://zenodo.org/r/1"}]}
    ids = lambda out, err="", c=chk: [d for d in execute.result_schema({"stdout": out, "stderr": err}, c) if "REFEREE_DATA" in d]
    res = 'REFEREE_RESULT {"acc": 1}'
    assert ids(res)
    assert not ids(res, 'REFEREE_DATA {"dataset": "d", "matches": true}')          # either stream
    assert ids(res + '\nREFEREE_DATA {"dataset": "d", "matches": "yes"}')
    assert not ids(res, c={**chk, "acquire": []})


# --- 2026-10-01 audit: public-data acquisition -----------------------------------------------------
# Public-data acquisition: citations, partial sets, failure classes, content validation, hub downloads,
# discovery metadata, host facts and quote-only released code. Run: python tests/_t_data.py [test_name ...]

# --- 1. citation edges -------------------------------------------------------------------------
def test_a_source_copied_from_prose_loses_the_sentences_punctuation_only():
    """Conformal paper: "https://doi.org/10.24432/C55W25." was planned with its full stop; the dotted URL 404s and
    read as a permanent-looking DATA BLOCKER."""
    from harness.evidence import clean_source
    for raw, want in (("https://doi.org/10.24432/C55W25.", "https://doi.org/10.24432/C55W25"),
                      ("https://x.org/a.csv),", "https://x.org/a.csv"), ("(https://x.org/a.csv)", "https://x.org/a.csv"),
                      ('"https://x.org/a.zip";', "https://x.org/a.zip"), ("<https://x.org/a>", "https://x.org/a"),
                      ("https://x.org/a]", "https://x.org/a"), ("https://x.org/a}:", "https://x.org/a"),
                      ("https://x.org/a’", "https://x.org/a"), ("https://zeno­do.org/records/1", "https://zenodo.org/records/1"),
                      ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
                      ("https://en.wikipedia.org/wiki/Foo_(bar)).", "https://en.wikipedia.org/wiki/Foo_(bar)"),
                      ("hf://datasets/o/n.", "hf://datasets/o/n"), ("https://x.org/data/", "https://x.org/data/")):
        assert clean_source(raw) == want, (raw, clean_source(raw), want)
    p = Paper(["The data are at https://doi.org/10.24432/C55W25. We use it."])
    hit = p.cites("https://doi.org/10.24432/C55W25.")
    assert hit and not any(v.endswith(".") for v in hit["variants"]), hit


CITE_PAGES = ["Weights: https://huggingface.co/acme/net-7b and corpus huggingface.co/datasets/acme/corpus.\n"
              "Records: https://zenodo.org/record/7654321 and www.zenodo.org/records/5550001 and "
              "https://zeno­do.org/records/4440001 too.\nDOI https://doi.org/10.5555/ABC.Def is printed in capitals."]


def test_one_record_cited_in_another_url_form_is_the_same_citation():
    p = Paper(CITE_PAGES)
    hit = p.cites("https://zenodo.org/records/7654321")
    assert hit and "https://zenodo.org/record/7654321" in hit["variants"], ("record/ vs records/", hit)   # the printed reading kept
    assert p.cites("https://zenodo.org/record/5550001"), "records/ printed, record/ planned"
    www = p.cites("https://zenodo.org/records/5550001")
    assert www and www["span"].startswith("www."), ("an optional www.", www)
    assert p.cites("https://www.zenodo.org/records/7654321"), "www. planned, none printed"
    assert p.cites("hf://models/acme/net-7b"), "a printed model URL cites hf://models"
    assert p.cites("hf://datasets/acme/corpus"), "a printed dataset URL cites hf://datasets"
    soft = p.cites("https://zenodo.org/records/4440001")
    assert soft and not any("­" in v for v in soft["variants"]), ("a soft hyphen is folded out of variants", soft)
    case = p.cites("https://doi.org/10.5555/abc.def")
    assert case and "https://doi.org/10.5555/ABC.Def" in case["variants"], ("the printed case is an alternate", case)
    for wrong in ("https://zenodo.org/records/765432", "https://zenodo.org/record/765432", "https://zenodo.org/records/76543210",
                  "https://example.org/record/7654321", "https://ww.zenodo.org/records/5550001", "hf://datasets/acme/net-7b",
                  "hf://models/acme/corpus", "hf://models/acme/net-7"):
        assert p.cites(wrong) is None, wrong                                     # another record, host or hub kind is refused


# --- 2. a partial file set is partial ----------------------------------------------------------------
def _record_server(files: dict, routes_extra: dict | None = None, md5s: dict | None = None):
    """A repository with a records API: files {name: bytes | None (listed, 404)}; md5s overrides a published md5."""
    from harness import fetcher
    box: dict = {}
    listing = lambda: json.dumps({"files": [
        {"key": n, "size": len(b) if b is not None else 7, "checksum": "md5:" + ((md5s or {}).get(n) or hashlib.md5(b or b"").hexdigest()),
         "links": {"self": f"http://{box['host']}/files/{n}"}} for n, b in files.items()]}).encode()
    routes = {"/records/5": lambda h: (200, {"Content-Type": "text/html"}, b"<html>record</html>"),
              "/api/records/5": lambda h: (200, {}, listing())}
    routes.update({f"/files/{n}": (lambda h, b=b: (200, {}, b)) for n, b in files.items() if b is not None})
    routes.update(routes_extra or {})
    srv, host = _serve(routes)
    box["host"] = host
    fetcher.RECORD_APIS[host] = f"http://{host}/api/records/{{id}}"
    return srv, host


def test_a_set_with_a_named_file_missing_is_partial_never_ok_never_shared_and_blocks_a_required_source():
    """Sep-30 rerun, transformer C3: 11 of 20 files of a record were admitted (the rest failed their published md5),
    yet the manifest said `ok`, data_blocker was empty and the set was shared with other checks."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _record_server({"a.csv": b"a,b\n1,2\n", "b.csv": b"a,b\n3,4\n", "c.csv": None},
                                   md5s={"b.csv": "0" * 32})
        try:
            src = {"source": f"http://{host}/records/5", "include": ["*.csv", "d.csv", "*.parquet"]}
            rec = _fetch(fetcher, td, [src])[0]
            assert [a["file"] for a in rec["admitted"]] == ["a.csv"], rec["admitted"]
            miss = {m["file"]: m["class"] for m in rec.get("missing") or []}
            assert miss == {"b.csv": "content_invalid", "c.csv": "missing"}, ("named files not admitted are recorded", rec.get("missing"))
            assert rec.get("unmatched_include") == ["d.csv", "*.parquet"], rec.get("unmatched_include")
            man = fetcher.manifest(str(td / "data"), [rec])
            assert man["status"] == "partial", man["status"]
            plan = [{**src, "required": True}]
            blk = execute.data_blocker(plan, man)
            assert len(blk) == 1 and blk[0]["class"] == "missing" and blk[0]["incomplete"], ("a required incomplete source blocks", blk)
            assert {m["file"] for m in blk[0]["missing"]} == {"b.csv", "c.csv", "d.csv"}, blk[0]["missing"]   # a glob is a gap, not a name
            assert execute.data_blocker([{**src, "required": False}], man) == []
            gaps = execute.data_gaps(man)
            assert len(gaps) == 4 and any("b.csv" in g for g in gaps) and any("*.parquet" in g for g in gaps), gaps
            whole = _fetch(fetcher, td / "w", [{"source": f"http://{host}/records/5", "include": ["a.csv"]}])[0]
            wman = fetcher.manifest(str(td / "w" / "data"), [whole])
            assert wman["status"] == "ok" and execute.data_gaps(wman) == [] and not execute.data_blocker(
                [{"source": whole["source"], "required": True}], wman), wman
            # a partial set is never shared with another check of the same plan; a complete one is
            root = td / "proj"
            state.write_json(root / "checks" / "C1" / "data.json", {**man, "fetched_at": "t", "plan": plan, "volume": "v1"})
            real = execute.docker_status, execute.start, execute._volume_gone
            execute.docker_status, execute._volume_gone = (lambda: (True, "")), (lambda v: False)
            started: list = []
            execute.start = lambda *a, **k: started.append(a) or {"container": "x"}
            try:
                got = execute.fetch(state.Config(), root, "C2", plan)
                assert got is None and len(started) == 1, ("a partial set was shared", got)
                state.write_json(root / "checks" / "C1" / "data.json", {**wman, "fetched_at": "t", "plan": plan, "volume": "v1"})
                got = execute.fetch(state.Config(), root, "C3", plan)
                assert got and got.get("shared_with") == "C1", got
            finally:
                execute.docker_status, execute.start, execute._volume_gone = real
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()


def test_a_named_file_that_only_failed_in_transit_is_a_fault_of_this_run():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _record_server({"a.csv": b"a,b\n1,2\n", "busy.csv": b"x"},
                                   routes_extra={"/files/busy.csv": lambda h: (503, {}, b"busy")})
        try:
            src = {"source": f"http://{host}/records/5", "include": ["*.csv"], "required": True}
            rec = _fetch(fetcher, td, [src])[0]
            man = fetcher.manifest(str(td / "data"), [rec])
            blk = execute.data_blocker([src], man)
            assert blk and blk[0]["class"] == "transient", ("a 503 on one file is INCONCLUSIVE, never BLOCKED", blk)
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()


def test_an_include_that_names_a_member_of_a_matched_archive_is_matched():
    """R2 conformal C5 shape: include ['*.zip', 'train*'] — `train*` names a member of the zip it took whole."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _serve({"/ds": lambda h: (200, {"Content-Type": "text/html"}, b'<a href="/static/ds.zip">zip</a>'),
                            "/static/ds.zip": lambda h: (200, {}, _zip({"train.csv.zip": "nested", "solution.csv": "a,b\n"}))})
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/ds", "include": ["*.zip", "train*"]}])[0]
        man = fetcher.manifest(str(td / "data"), [rec])
        assert len(rec["admitted"]) == 2 and not rec.get("unmatched_include") and man["status"] == "ok", (rec.get("unmatched_include"), man["status"])
        srv.shutdown()


# --- 3. failure classes --------------------------------------------------------------------------
def test_an_alternate_reading_never_outranks_the_primary_readings_failure():
    """The always-generated `zen-odo.org` reading fails DNS (transient) and outranked the record's own 404: a missing
    source read INCONCLUSIVE."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _serve({"/busy": lambda h: (503, {}, b"busy")})
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/gone", "alternates": ["http://127.0.0.1:1/gone"]}])[0]
        assert rec["failure_class"] == "missing", ("the primary reading's 404 decides", rec["failure_class"], rec["attempts"])
        rec = _fetch(fetcher, td / "b", [{"source": f"http://{host}/busy", "alternates": [f"http://{host}/gone"]}])[0]
        assert rec["failure_class"] == "transient", rec["failure_class"]
        srv.shutdown()


def test_a_follow_up_plan_made_on_a_harness_fault_is_planned_again_never_one_with_a_finding():
    """2026-10-01 run, PPRM: the follow-up round gave the LLM QA claims up because the harness had charged failed searches
    to its budget ("one search left"). After the fix the operator re-plans the round; never once it found something."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        run = lambda cid: {"id": cid, "kind": "RECONSTRUCTION", "claim_quote": CIFAR, "covers": ["CIFAR-10-C"], **RUN,
                           "target": {"quote": CIFAR, "relation": "acc_a > acc_b"}}
        _seal(cfg, pid, "plan", {"checks": [run("R")], "central_claims": [_claim()]}, td)
        root = td / pid
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "INCONCLUSIVE", "reason": "x"})
        _seal(cfg, pid, "plan:2", {"checks": [run("F1")], "central_claims": [_claim(checks=["F1"])]}, td)
        state.write_json(root / "checks" / "C7" / "data.json", {"sources": []})
        state.write_json(root / "checks" / "C7" / "exec.json", {"token": "t"})          # still executing: stop it first
        assert "executing" in tasks.reopen(cfg, pid, "plan:2", "x")["error"]
        # Oct-01 PPRM: C8/C9 had ended BLOCKED and kept their exec.json; an ended check is not executing.
        state.write_json(root / "checks" / "C7" / "outcome.json", {"status": "BLOCKED", "reason": "RESOURCE BLOCKER: x",
                                                                   "pilot_stages": {"a": {"n": 1, "mean": 0.75}}})
        out = tasks.reopen(cfg, pid, "plan:2", "the follow-up was planned on failed searches read as searches")
        assert out["withdrawn"] == "plan:2" and out["checks_set_aside"] == ["C7"]
        assert (root / "checks" / "C7.withdrawn.1" / "data.json").exists() and (root / "sealed" / "plan__2.withdrawn.1.json").exists()
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and [o["id"] for o in owed] == ["plan:2"]        # the round is planned again
        _seal(cfg, pid, "plan:2", {"checks": [run("F1")], "central_claims": [_claim(checks=["F1"])]}, td)
        # The new round's C7 is a live check; the withdrawn C7 is never one, and stays visible with what it measured.
        led = report.ledger(tasks._Ctx(cfg, pid))
        assert [c["id"] for c in led["checks"]] == ["C1", "C7"] and led["checks"][1]["status"] == "PENDING"
        w = led["withdrawn_checks"]
        assert [x["folder"] for x in w] == ["checks/C7.withdrawn.1"] and w[0]["status"] == "BLOCKED"
        assert "failed searches" in w[0]["withdrawn_because"] and w[0]["pilot_stages"]["a"]["n"] == 1
        md = report.render(tasks._Ctx(cfg, pid), led, None)
        assert "checks/C7.withdrawn.1" in md and "measured before the limit, deciding nothing: a n=1 mean 0.75" in md
        state.write_json(root / "checks" / "C7" / "outcome.json", {"status": "RELATION_HOLDS", "values": [1]})
        assert "never withdrawn" in tasks.reopen(cfg, pid, "plan:2", "again")["error"]   # a finding is never re-rolled


def test_a_resource_blocker_is_reopened_on_the_same_approved_script():
    """Oct-01 PPRM C7/C8/C9: two checks' runs shared one 8 GB GPU until both passed the per-run limit. The fault was the
    host's scheduling, not the script: the reopened check runs the SAME approved script again (gen/verify seals kept,
    completed seeds kept for reuse, its data not re-acquired), so a measurement is never re-rolled by a rewrite. A
    crashed script is still written and approved again."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project_pages(td, DATA_PAGES)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        _seal(cfg, pid, "plan", {"checks": [_run("R")], "central_claims": [_claim()]}, td)
        root, cdir = td / pid, td / pid / "checks" / "C1"
        script = "import argparse\np = argparse.ArgumentParser(); p.add_argument('--seed', type=int); p.parse_args()\n"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "script.1.py").write_text(script, encoding="utf-8")
        sha = state.sha256(script)
        seals = state.read_json(root / "seals.json", {})
        for tid, obj in ((f"gen:C1.1", {"script_sha256": sha, "runs": 1, "runs_quote": "", "metric": "", "outputs": ["acc_a", "acc_b"],
                                        "deviations": [], "bindings": [], "stochastic": True, "seed_flow": "x"}),
                         (f"verify:C1.1", {"verdict": "APPROVE", "script_sha256": sha, "required_changes": "", "notes": "ok"})):
            p = root / "sealed" / f"{tasks._safe(tid)}.json"
            state.write_json(p, obj)
            seals[tid] = state.sha256(p.read_bytes())
        state.write_json(root / "seals.json", seals)
        tasks._plan(tasks._Ctx(cfg, pid))
        first = state.read_json(cdir / "exec.json")
        assert first and state.read_json(cdir / "check.json")["script_sha256"] == sha        # approved: started
        state.append_jsonl(cdir / "seeds.jsonl", {"key": sha, "seed": 0, "values": [0.15], "seconds": 2682})
        state.write_json(cdir / "outcome.json", {"check": "C1", "status": "BLOCKED", "resource": "per_run_timeout",
                                                 "authorized": True, "reason": "RESOURCE BLOCKER: a single run exceeded"})
        state.write_json(cdir / "data.json", {"n_files": 3, "fetched_at": "t", "sources": [{"unmatched_include": ["*.csv"]}]})
        res = tasks.reopen(cfg, pid, "C1", "two checks shared one GPU")
        assert res["same_approved_script"] and "gen:C1.1" not in res["seals_withdrawn"], res
        assert tasks._sealed(root, "gen:C1.1") and tasks._sealed(root, "verify:C1.1")
        assert (cdir / "seeds.jsonl").exists() and (cdir / "data.json").exists()            # seeds reused, data kept
        assert not (cdir / "exec.json").exists() and (cdir / "outcome.reopened.1.json").exists()
        tasks._plan(tasks._Ctx(cfg, pid))                                                  # the same script runs again
        assert state.read_json(cdir / "exec.json") and state.read_json(cdir / "check.json")["script_sha256"] == sha
        # A script that crashed is the script's fault: it is written and approved again.
        state.write_json(cdir / "outcome.json", {"check": "C1", "status": "INCONCLUSIVE", "authorized": True,
                                                 "reason": "the script crashed: ZeroDivisionError"})
        res = tasks.reopen(cfg, pid, "C1", "x")
        assert not res.get("same_approved_script") and {"gen:C1.1", "verify:C1.1"} <= set(res["seals_withdrawn"])


def test_a_gpu_holds_one_run_and_the_budget_counts_its_runs_one_at_a_time():
    """Oct-01 PPRM: C7 and C8 each loaded Qwen2-VL-2B on one 8 GB GPU; both crawled past the per-run limit. A run given the
    GPU holds it alone, and a check whose runs take the GPU is projected one run at a time against its budget."""
    def docker(a, timeout):                       # two running containers, the second given the GPU
        return (0, "aaa\nbbb\n") if a[:2] == ["docker", "ps"] else (0, "null\n[{\"Driver\":\"\",\"Count\":-1}]\n")
    real = execute._docker, execute.gpu
    try:
        execute._docker = docker
        assert execute.gpu_busy()                                                   # one container holds the GPU
        execute._docker = lambda a, t: (0, "aaa\n") if a[1] == "ps" else (0, "null\n")
        assert not execute.gpu_busy()                                               # running, none with a GPU
        execute._docker = lambda a, t: (0, "") if a[1] == "ps" else (0, "")
        assert not execute.gpu_busy()                                               # nothing running
        execute._docker = lambda a, t: (1, "Cannot connect to the Docker daemon")
        assert execute.gpu_busy()                                                   # unknown is busy
        cfg = state.Config()
        cfg.parallel = 2
        recon = {"kind": "RECONSTRUCTION"}
        st = {"pilot_s": 2682, "seed": 1}
        execute.gpu = lambda c: True
        need, limit, _, w = execute._projected(cfg, recon, st, 3, 2682)
        assert w == 1 and need == 2682 + 2 * 2682 and need > limit                  # 8046 s > 7200 s: blocked by budget
        execute.gpu = lambda c: False
        assert execute._projected(cfg, recon, st, 3, 2682)[3] == 2                  # a CPU host still runs two at a time
        assert execute.gpu_run(cfg, recon) is False and execute.gpu_run(cfg, {"kind": "CERTIFICATE"}) is False
    finally:
        execute._docker, execute.gpu = real


def test_a_draft_never_shares_the_gpu_with_a_timed_run_and_a_skipped_one_costs_no_try():
    """Oct-01 PPRM: drafts (`run.py try`) of C7/C8 ran on the GPU beside C9's timed evidence run (gpu_overlap.txt). A
    synchronous draft on a GPU host now waits its turn: it is not run while another run holds the GPU, says so, and
    does not count against the draft budget; it is itself visible to gpu_busy (label referee.sync)."""
    seen = []
    real = execute.docker_status, execute.gpu, execute.gpu_busy, execute._docker
    try:
        execute.docker_status, execute.gpu, execute.gpu_busy = (lambda: (True, "")), (lambda c: True), (lambda: True)
        with tempfile.TemporaryDirectory() as t:
            cfg, pid = _project(Path(t))
            cfg.allow_script_exec = True
            res = execute.try_script(cfg, pid, "C1", "import torch\nprint(1)", {"kind": "RECONSTRUCTION"})
            assert res["retry"] and "GPU" in res["error"], res
            cpu = execute.try_script(cfg, pid, "C1", "print(1)", {"kind": "CERTIFICATE"})   # not given the GPU: not held back
            assert not cpu.get("retry")
            # 2026-10-02: a reconstruction that cannot use the GPU never waits for it either
            assert not execute.try_script(cfg, pid, "C1", "import numpy\nprint(1)", {"kind": "RECONSTRUCTION"}).get("retry")
        def docker(a, timeout):                       # a sync draft holding the GPU is seen
            seen.append(a)
            if a[:2] == ["docker", "ps"]:
                return (0, "") if "label=referee=1" in a else (0, "sss\n")
            return 0, "[{\"Driver\":\"\",\"Count\":-1}]\n"
        execute._docker, execute.gpu_busy = docker, real[2]
        assert execute.gpu_busy() and any("label=referee.sync=1" in a for a in seen)
    finally:
        execute.docker_status, execute.gpu, execute.gpu_busy, execute._docker = real
    launch = []
    real_run = subprocess.run
    try:
        subprocess.run = lambda argv, **k: launch.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        execute.run(["true"], mounts=[], workdir="/", image="x", network=False, timeout=5, mode="try", target="t", gpus=True)
    finally:
        subprocess.run = real_run
    assert "referee.sync=1" in launch[0]                                            # the draft carries its label
    with tempfile.TemporaryDirectory() as t:                                        # a skipped draft is not counted
        cfg, pid = _project(Path(t))
        root = state.pdir(cfg, pid)
        (root / "tasks").mkdir(exist_ok=True)
        (root / "tasks" / "gen__C1.1.md").write_text("task", encoding="utf-8")
        (Path(t) / "s.py").write_text("print(1)", encoding="utf-8")
        saved = execute.try_script
        try:
            execute.try_script = lambda *a, **k: {"error": "the GPU is held by a timed run", "retry": True}
            assert tasks.try_(cfg, pid, "gen:C1.1", str(Path(t) / "s.py"))["retry"]
            assert not (root / "checks" / "C1" / "tries.jsonl").exists()
        finally:
            execute.try_script = saved


def test_the_remaining_runs_are_projected_from_the_seeds_own_times():
    """Oct-02 PPRM C8: the extension to 6 replicates was refused on the pilot's 2682 s, measured while another check
    shared the GPU; seeds 1 and 2 took 284 s and 290 s alone. Once two or more seeds have completed, the projection
    uses their median (never the minimum); a slower later seed raises it."""
    cfg = state.Config()
    cfg.parallel = 1
    recon = {"kind": "RECONSTRUCTION"}
    fast = {"pilot_s": 2682, "seed": 3, "seed_seconds": {"0": 2682, "1": 284, "2": 290}}
    need, limit, _, _ = execute._projected(cfg, recon, fast, 6, spent=execute._spent(fast))
    assert need == 2682 + 284 + 290 + 3 * 290 and need <= limit                    # extension fits: 4126 s
    slow = {"pilot_s": 300, "seed": 3, "seed_seconds": {"0": 300, "1": 2600, "2": 2700}}
    need, limit, _, _ = execute._projected(cfg, recon, slow, 6, spent=execute._spent(slow))
    assert need == 300 + 2600 + 2700 + 3 * 2600 and need > limit                   # a slow later seed raises it
    assert execute._projected(cfg, recon, {"pilot_s": 500, "seed": 1, "seed_seconds": {"0": 500}}, 3)[0] == 1000   # one seed: pilot
    two = lambda a, b: execute._projected(cfg, recon, {"pilot_s": a, "seed": 2, "seed_seconds": {"0": a, "1": b}}, 3)[0]
    assert two(300, 2600) == 2600 and two(2600, 300) == 2600                    # two seeds: the slower, never the minimum


def test_a_tls_failure_is_a_fault_of_this_run_never_a_data_blocker():
    """2026-10-01 run, conformal C5: the Porto zip that arrived on 09-30 failed with `TLSV1_ALERT_DECODE_ERROR`; the class
    `protocol` was never retried and ended the check as a DATA BLOCKER."""
    import ssl
    import urllib.error
    from harness import fetcher
    assert fetcher.classify(ssl.SSLError(1, "[SSL: TLSV1_ALERT_DECODE_ERROR] tlsv1 alert decode error"))[0] == "transient"
    assert fetcher.classify(urllib.error.URLError(ssl.SSLError(1, "decode error")))[0] == "transient"
    with tempfile.TemporaryDirectory() as t:
        cdir = Path(t)
        blk = [{"source": "https://x.org/d.zip", "class": "transient", "detail": "TLS: decode error", "rejected": []}]
        tasks._data_blocked(cdir, {"id": "C1", "kind": "RECONSTRUCTION"}, blk)
        assert state.read_json(cdir / "outcome.json")["status"] == "INCONCLUSIVE"        # this run's fault, not the source's


def test_a_search_that_failed_is_no_search():
    """2026-10-01 run, PPRM: every Hugging Face search answered HTTP 400 (the list endpoints refuse `expand=usedStorage`),
    and the planner gave up MMLU and the Qwen models as `data` on those failed searches; the seal accepted them."""
    from harness import discover
    seen = []
    def hub(url):
        seen.append(url)
        if "usedStorage" in url or "expand=bogus" in url:
            raise OSError("HTTPError: HTTP Error 400: Bad Request")
        return [{"id": "Qwen/Qwen2-VL-2B-Instruct", "author": "Qwen", "gated": False, "private": False,
                 "safetensors": {"parameters": {"BF16": 2208985600}, "total": 2208985600}}] if "expand" in url else [
                {"id": "Qwen/Qwen2-VL-2B-Instruct", "author": "Qwen"}]
    got = discover._hub("models", "huggingface-models", "Qwen2-VL-2B-Instruct", hub)
    assert "usedStorage" not in seen[0] and got[0]["gated"] is False and got[0]["size_bytes"] == 2 * 2208985600
    real = discover._HF_EXPAND
    discover._HF_EXPAND = {**real, "models": ("bogus",)}                  # a hub that refuses an expansion
    try:
        plain = discover._hub("models", "huggingface-models", "Qwen2-VL-2B-Instruct", hub)
    finally:
        discover._HF_EXPAND = real
    assert plain[0]["source"] == "hf://models/Qwen/Qwen2-VL-2B-Instruct" and plain[0]["gated"] is None   # unknown, never false
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        cfg.allow_network = cfg.allow_data_search = True
        bad = discover.search(cfg, pid, "MMLU", registry="huggingface", get=lambda u: (_ for _ in ()).throw(
            OSError("HTTPError: HTTP Error 400: Bad Request")))
        omit = _claim(scope=["CIFAR-10-C", "MMLU"], omitted=[{"item": "MMLU", "why": "no record", "blocker": "data",
                                                               "discovery": [bad["id"]]}])
        assert "every search it cites failed" in _refused(lambda: _plan(x, [_run()], [omit]))
        cfg.max_discoveries = 2
        for _ in range(3):                                         # failed searches never spend the budget
            assert "budget" not in str(discover.search(cfg, pid, "MMLU", registry="huggingface", get=lambda u: (
                _ for _ in ()).throw(OSError("HTTPError: HTTP Error 503"))).get("error", ""))
        cfg.max_discoveries = 12
        plan = {"central_claims": [{**omit, "page": 1, "omitted": [dict(o) for o in omit["omitted"]]}], "checks": []}
        report._failed_searches(x.root, plan)
        assert "failed" in plan["central_claims"][0]["omitted"][0]["unverified"]       # a plan sealed before: flagged
        ok = discover.search(cfg, pid, "MMLU", registry="zenodo", get=lambda u: {"hits": {"hits": []}})
        _plan(x, [_run()], [{**omit, "omitted": [{**omit["omitted"][0], "discovery": [bad["id"], ok["id"]]}]}])   # one answered
        half = {"central_claims": [{**omit, "page": 1, "omitted": [{**omit["omitted"][0], "discovery": [bad["id"], ok["id"]]}]}]}
        report._failed_searches(x.root, half)                     # ...but the hub that hosts such data never did: flagged
        assert "huggingface" in half["central_claims"][0]["omitted"][0]["unverified"]


def test_hub_errors_are_classified_by_their_http_status():
    from harness import fetcher

    class HfHubHTTPError(OSError):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    class RepositoryNotFoundError(HfHubHTTPError):
        pass

    class GatedRepoError(RepositoryNotFoundError):
        pass

    class LocalEntryNotFoundError(HfHubHTTPError, FileNotFoundError):
        pass

    class ReadTimeout(Exception):          # an httpx-style timeout is no OSError
        pass
    r = lambda c: types.SimpleNamespace(status_code=c)
    for e, want in ((RepositoryNotFoundError("x", r(401)), "inaccessible"), (GatedRepoError("x", r(403)), "inaccessible"),
                    (RepositoryNotFoundError("x", r(404)), "missing"), (HfHubHTTPError("x", r(503)), "transient"),
                    (HfHubHTTPError("x", r(429)), "transient"), (RepositoryNotFoundError("no response"), "missing"),
                    (GatedRepoError("no response"), "inaccessible"), (LocalEntryNotFoundError("offline"), "transient"),
                    (ReadTimeout("slow"), "transient")):
        assert fetcher.classify(e)[0] == want, (type(e).__name__, getattr(e, "response", None), fetcher.classify(e), want)


def test_a_short_transfer_that_repeats_is_transient_not_invalid_content():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td, good = Path(t), b"x" * 50000
        srv, host = _serve({"/cut.bin": lambda h: (200, {"Content-Length": str(len(good))}, good[:1000])})
        f = fetcher.Fetcher(str(td / "cache"), 1 << 30, [], sleep=lambda s: None, tries=3, timeout=5)
        try:
            f.get(f"http://{host}/cut.bin", str(td / "d"))
            raise AssertionError("a cut transfer was admitted")
        except fetcher.FetchError as e:
            assert e.klass == "transient" and len(e.attempts) == 3, (e.klass, len(e.attempts))
        srv.shutdown()


# --- 4. one HTML detector; archives by suffix; extraction cap; include in the fallback ---------------------
def test_one_html_detector_and_archives_unpacked_by_suffix_only():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        pages = {"bom.csv": b"\xef\xbb\xbf<!DOCTYPE html><html><body>log in</body></html>",
                 "comment.csv": b"<!-- served by a proxy -->\n<html><body>log in</body></html>",
                 "xhtml.csv": b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
                              b'"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n<html xmlns="http://www.w3.org/1999/xhtml"></html>'}
        files = {**pages, "webdata.jsonl": b'{"url": "u", "page": "<html><body>hi</body></html>"}\n',
                 "arrays.npz": _zip({"a.npy": "x" * 50}), "book.xlsx": _zip({"xl/workbook.xml": "<w/>"}),
                 "model.pt": _zip({"archive/data.pkl": "p"}), "set.xml": b'<?xml version="1.0"?>\n<dataset><row>1</row></dataset>'}
        routes = {"/": lambda h: (200, {"Content-Type": "text/html"}, "".join(f'<a href="/{n}">{n}</a>' for n in files).encode())}
        routes.update({f"/{n}": (lambda h, b=b: (200, {"Content-Type": "application/octet-stream"}, b)) for n, b in files.items()})
        srv, host = _serve(routes)
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/", "include": list(files)}])[0]
        got = sorted(a["file"] for a in rec["admitted"])
        assert got == ["arrays.npz", "book.xlsx", "model.pt", "set.xml", "webdata.jsonl"], ("pages rejected, zips by suffix kept whole", got)
        assert sorted(r["file"] for r in rec["rejected"]) == sorted(pages), rec["rejected"]
        for n, b in pages.items():
            (td / n).write_bytes(b)
            assert fetcher.is_html(str(td / n), {}), n                    # the same detector, wherever a page is judged
        srv.shutdown()


def test_extracted_bytes_count_against_the_storage_cap():
    """A 1 MB member compressed to ~1 KB: extraction is bounded by the storage cap, not by the archive's size."""
    import zipfile
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", b"\0" * (1 << 20))
        srv, host = _serve({"/bomb.zip": lambda h: (200, {}, buf.getvalue())})
        f = fetcher.Fetcher(str(td / "cache"), 200_000, [], sleep=lambda s: None, tries=3, timeout=5)
        rec = fetcher.acquire(f, {"source": f"http://{host}/bomb.zip"}, str(td / "data" / "0"), str(td / "tmp"), "0")
        assert rec["admitted"] == [] and not (td / "data" / "0" / "zeros.bin").exists(), ("a zip bomb was unpacked", rec["admitted"])
        assert rec["failure_class"] == "storage", rec["failure_class"]
        srv.shutdown()


def test_the_landing_page_fallback_never_admits_a_file_include_does_not_name():
    """Sep-30 rerun, conformal C8: include named the members of the dataset ZIP; the fallback followed every data link and
    admitted the site's web-app manifest.json as data."""
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _serve({"/ds": lambda h: (200, {"Content-Type": "text/html"},
                                              b'<a href="/manifest.json">m</a><a href="/static/ds.zip">zip</a>'),
                            "/manifest.json": lambda h: (200, {}, b'{"name": "web app", "icons": []}'),
                            "/static/ds.zip": lambda h: (200, {}, _zip({"train.csv.zip": "n", "other.csv": "a\n"}))})
        rec = _fetch(fetcher, td, [{"source": f"http://{host}/ds", "include": ["train.csv*"]}])[0]
        assert [a["file"] for a in rec["admitted"]] == ["train.csv.zip"], rec["admitted"]
        srv.shutdown()


# --- 5. a records listing reaches a file inside one of its archives -----------------------------------------
def test_include_reaches_a_file_inside_a_records_archive():
    from harness import fetcher
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        bundle = _zip({"inner/train.csv": "a\n1\n", "inner/test.csv": "a\n2\n"})
        srv, host = _record_server({"a.csv": b"a\n0\n", "bundle.zip": bundle})
        try:
            rec = _fetch(fetcher, td, [{"source": f"http://{host}/records/5", "include": ["a.csv", "train.csv"]}])[0]
            got = sorted(a["file"] for a in rec["admitted"])
            assert got == ["a.csv", "inner/train.csv"], ("include named a member of the record's archive", got)
            assert not rec.get("unmatched_include") and any("members" in r for r in rec["recovery"]), rec
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()
        many = {f"part{i}.zip": _zip({f"x{i}.csv": "1"}) for i in range(fetcher.MAX_ARCHIVES + 1)}
        srv, host = _record_server({"a.csv": b"a\n0\n", **many})
        try:
            rec = _fetch(fetcher, td / "m", [{"source": f"http://{host}/records/5", "include": ["a.csv", "train.csv"]}])[0]
            assert [a["file"] for a in rec["admitted"]] == ["a.csv"] and rec["unmatched_include"] == ["train.csv"], rec   # no guessing
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()


# --- 6. the Hugging Face path -----------------------------------------------------------------------
def _fake_hub(repos: dict, raise_on: dict | None = None):
    """A stand-in huggingface_hub: repos {repo: {path: bytes}}; snapshot_download also leaves the hub's own
    `.cache/huggingface` metadata (an empty .lock among it) in local_dir, as the real one does."""
    import fnmatch
    import os
    mod = types.ModuleType("huggingface_hub")

    class HfApi:
        def repo_info(self, repo, repo_type=None, revision=None, files_metadata=False):
            if repo in (raise_on or {}):
                raise raise_on[repo]
            return types.SimpleNamespace(sha="abc123", siblings=[types.SimpleNamespace(rfilename=k, size=len(v))
                                                                 for k, v in repos[repo].items()])

    def snapshot_download(repo, repo_type=None, revision=None, allow_patterns=None, ignore_patterns=None, local_dir=None):
        for k, v in repos[repo].items():
            if any(fnmatch.fnmatchcase(k, p) for p in allow_patterns or ["*"]) and not any(
                    fnmatch.fnmatchcase(k, p) for p in ignore_patterns or []):
                os.makedirs(os.path.dirname(os.path.join(local_dir, k)) or local_dir, exist_ok=True)
                Path(local_dir, k).write_bytes(v)
        meta = Path(local_dir, ".cache", "huggingface", "download")
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "x.lock").write_bytes(b"")
        (meta.parent / ".gitignore").write_text("*")
        return local_dir
    mod.HfApi, mod.snapshot_download = HfApi, snapshot_download
    return mod


def _with_hub(mod, fn):
    from harness import fetcher
    saved, run = sys.modules.get("huggingface_hub"), fetcher.subprocess.run
    sys.modules["huggingface_hub"] = mod
    fetcher.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0)   # never a real pip install
    try:
        return fn()
    finally:
        fetcher.subprocess.run = run
        if saved is None:
            sys.modules.pop("huggingface_hub", None)
        else:
            sys.modules["huggingface_hub"] = saved


def test_a_transient_hub_failure_is_retried_and_the_retry_resumes():
    """Oct-05 label ranking C6: the hub client lost one `.incomplete` file 13 minutes into a 3 GB snapshot
    (FileNotFoundError, classified transient) and the acquisition ended there: the hf:// path had no retry, while every
    http(s) download had bounded retries. A transient hub failure is retried (bounded), in the same local folder so the
    files that arrived are not fetched again, and the retry is recorded; a non-transient one is not retried."""
    from harness import fetcher
    repos = {"o/set": {"eval/a.json": b'{"x": 1}', "eval/b.json": b'{"x": 2}'}}
    calls = []

    def flaky(n_fail, exc):
        mod = _fake_hub(repos)
        real = mod.snapshot_download

        def snap(repo, **kw):
            calls.append(kw["local_dir"])
            if len(calls) <= n_fail:
                Path(kw["local_dir"], "eval").mkdir(parents=True, exist_ok=True)
                Path(kw["local_dir"], "eval", "a.json").write_bytes(b'{"x": 1}')       # one file arrived before the cut
                raise exc
            return real(repo, **kw)
        mod.snapshot_download = snap
        return mod
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        go = lambda d: lambda: fetcher.acquire(fetcher.Fetcher(str(td / "cache"), 10 ** 6, [], sleep=lambda s: None, tries=3,
                                                               timeout=5), {"source": "hf://datasets/o/set", "include": ["eval/*"]},
                                               str(td / d / "0"), str(td / "tmp"), "0")
        rec = _with_hub(flaky(2, FileNotFoundError(2, "No such file or directory", "x.incomplete")), go("d1"))
        assert len(rec["admitted"]) == 2 and not rec["failure_class"], rec
        assert len(set(calls)) == 1 and len(calls) == 3                     # the same folder: a resume, not a fresh start
        assert sum("retried" in r for r in rec["recovery"]) == 2, rec["recovery"]
        calls.clear()
        rec = _with_hub(flaky(5, FileNotFoundError(2, "No such file or directory", "x.incomplete")), go("d2"))
        assert rec["failure_class"] == "transient" and len(calls) == 3       # bounded
        calls.clear()
        rec = _with_hub(flaky(1, type("GatedRepoError", (Exception,), {})("gated repo")), go("d3"))
        assert len(calls) == 1 and rec["failure_class"] != "transient"      # not transient: never retried


def test_hub_snapshot_files_are_validated_and_the_storage_cap_is_cumulative():
    """R2 PPRM C3: the hub's `.cache/huggingface/*` metadata and 0-byte `.lock` files were admitted as data; and the cap
    test compared each repository alone with the cap (`need > cap`), never the running total."""
    from harness import fetcher
    repos = {"o/set": {"data/train.parquet": b"P" * 120, "data/test.parquet": b"Q" * 80, "README.md": b"# set\n", ".gitattributes": b"*"},
             "o/two": {"w.safetensors": b"W" * 150}}
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)

        def go():
            f = fetcher.Fetcher(str(td / "cache"), 300, [], sleep=lambda s: None, tries=1, timeout=5)
            a = fetcher.acquire(f, {"source": "hf://datasets/o/set", "include": ["*.parquet"]}, str(td / "data" / "0"), str(td / "tmp"), "0")
            b = fetcher.acquire(f, {"source": "hf://models/o/two"}, str(td / "data" / "1"), str(td / "tmp"), "1")
            return a, b
        a, b = _with_hub(_fake_hub(repos), go)
        on_disk = sorted(p.relative_to(td / "data" / "0").as_posix() for p in (td / "data" / "0").rglob("*") if p.is_file())
        assert on_disk == ["data/test.parquet", "data/train.parquet"], ("only validated repository files", on_disk)
        assert sorted(x["file"] for x in a["admitted"]) == ["data/test.parquet", "data/train.parquet"] and a["failure_class"] == ""
        assert b["admitted"] == [] and b["failure_class"] == "storage", ("200 + 150 bytes pass a cap of 300", b["failure_class"])

        class RepositoryNotFoundError(OSError):
            response = types.SimpleNamespace(status_code=404)
        r = _with_hub(_fake_hub(repos, {"o/none": RepositoryNotFoundError("404")}), lambda: _fetch(fetcher, td / "n", [{"source": "hf://models/o/none"}]))
        assert r[0]["failure_class"] == "missing", r[0]["failure_class"]


# --- 7. include patterns match case-sensitively, as in the Linux container --------------------------------
def test_include_patterns_are_case_sensitive_on_every_host():
    from harness import fetcher
    assert fetcher.pick(["http://h/A.CSV", "http://h/b.csv"], ["*.csv"]) == ["http://h/b.csv"], fetcher.pick(["http://h/A.CSV", "http://h/b.csv"], ["*.csv"])
    assert not fetcher._named("Data/X.CSV", ["*.csv"]) and fetcher._named("Data/X.CSV", ["*.CSV"])


# --- 8. discovery of models and gated artifacts --------------------------------------------------------
def _hub_registry(seen: list):
    def get(url: str):
        seen.append(url)
        if "huggingface.co/api/models" in url:
            return [{"id": "acme/net-7b", "author": "acme", "createdAt": "2025-01-01T00:00:00Z", "downloads": 9, "tags": ["text"],
                     "gated": False, "private": False, "usedStorage": 30_000_000_000,
                     "safetensors": {"parameters": {"BF16": 7_000_000_000, "F32": 1000}, "total": 7_000_001_000}},
                    {"id": "acme/closed", "gated": "manual", "private": False, "usedStorage": 5_000}]
        if "huggingface.co/api/datasets" in url:
            return [{"id": "acme/corpus", "author": "acme", "gated": "auto", "private": False, "usedStorage": 123_456}]
        if "zenodo" in url:
            return {"hits": {"hits": []}}
        return {"data": []}
    return get


def test_hub_candidates_carry_gating_and_size_and_a_repeated_query_is_not_searched_again():
    from harness import discover
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project_pages(Path(t), CITE_PAGES)
        seen: list = []
        rec = discover.search(cfg, pid, "acme net", get=_hub_registry(seen))
        m = {c["source"]: c for c in rec["results"]["huggingface-models"]["candidates"]}
        net = m.get("hf://models/acme/net-7b") or {}
        assert net.get("gated") is False and net.get("private") is False and net.get("params") == 7_000_001_000, net
        assert net.get("size_bytes") == 14_000_004_000 and net.get("size_basis") == "safetensors", net
        assert m["hf://models/acme/closed"].get("gated") == "manual" and m["hf://models/acme/closed"].get("size_bytes") == 5_000
        ds = rec["results"]["huggingface"]["candidates"][0]
        assert ds.get("gated") == "auto" and ds.get("size_bytes") == 123_456 and ds.get("size_basis") == "usedStorage", ds
        hub = [u for u in seen if "huggingface.co" in u]
        assert all("expand=gated" in u and "expand=author" in u for u in hub) and "safetensors" not in next(
            u for u in hub if "/api/datasets" in u), hub
        assert discover.returned(cfg, pid, rec["id"], "hf://models/acme/net-7b")["size_bytes"] == 14_000_004_000
        n = len(seen)
        again = discover.search(cfg, pid, "  ACME   net ", get=_hub_registry(seen))
        assert again["id"] == rec["id"] and len(seen) == n and len(discover.records(cfg, pid)) == 1, ("a repeated query re-searched", again.get("id"))
        cfg.max_discoveries = 1
        assert discover.search(cfg, pid, "acme net", get=_hub_registry(seen))["id"] == rec["id"]    # reuse costs no budget
        assert "budget" in discover.search(cfg, pid, "acme other", get=_hub_registry(seen))["error"]
        cfg.max_discoveries = 5
        assert discover.search(cfg, pid, "acme net", registry="zenodo", get=_hub_registry(seen))["id"] == "D2"   # another registry set
        listing = {"files": [{"key": "a.csv", "size": 5, "checksum": "md5:" + "1" * 32, "links": {"self": "https://zenodo.org/x/a.csv"}}]}
        calls: list = []
        one = discover.files(cfg, pid, "https://zenodo.org/records/42", get=lambda u: calls.append(u) or listing)
        two = discover.files(cfg, pid, "https://zenodo.org/records/42", get=lambda u: calls.append(u) or listing)
        assert one["id"] == two["id"] and len(calls) == 1, (one.get("id"), two.get("id"), calls)


# --- 9. host facts a compute blocker can be compared against ---------------------------------------------
def test_the_host_is_measured_with_gpu_memory_and_free_disk_once_per_process():
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects = Path(t)
        calls: list = []
        real = execute._docker, execute.gpu, execute._gpu_mb

        def docker(argv, timeout):
            calls.append(argv)
            return (0, f"8 {16000 * 2 ** 20} {t}\n") if argv[:2] == ["docker", "info"] else (1, "no")
        execute._docker, execute.gpu, execute._gpu_mb = docker, (lambda c: True), (lambda: "24576")
        try:
            getattr(execute, "_HOST", {}).clear()
            h = execute.host(cfg)
            assert h["cpus"] == 8 and h["ram_mb"] == 16000 and h["gpu"] is True and h["vram_mb"] == 24576, h
            assert isinstance(h["disk_free_gb"], float) and h["disk_free_gb"] > 0, h
            facts = execute.host_facts(cfg)
            assert "24576 MB" in facts and "GB free" in facts, facts
            n = len(calls)
            execute.host(cfg), execute.host_facts(cfg)
            assert len(calls) == n, "measured again within one process"
            execute._HOST.clear()
            away: list = []
            execute._docker, execute.gpu = (lambda a, timeout: away.append(a) or (1, "daemon away")), (lambda c: False)
            h = execute.host(cfg)
            assert h == {"cpus": None, "ram_mb": None, "gpu": False, "vram_mb": None, "disk_free_gb": None}, h
            assert "unknown" in execute.host_facts(cfg)
            assert len(away) == 2, ("a failed measurement was kept for the whole process", len(away))
        finally:
            execute._docker, execute.gpu, execute._gpu_mb = real
            execute._HOST.clear()


# --- 10. released code as quote-only text on the host -------------------------------------------------------
def test_released_code_is_never_data_but_its_text_reaches_the_host_for_quoting():
    import contextlib
    from harness import fetcher
    nb = json.dumps({"cells": [{"cell_type": "markdown", "source": ["# Fit\n"]},
                               {"cell_type": "code", "source": ["q = fit(c)\n", "print(q)\n"]}]}).encode()
    big = b"x = 1\n" * 60000                                                    # 360 KB of code: cut at the per-file cap
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        srv, host = _record_server({"data.csv": b"a\n1\n", "analysis.py": b"def fit(c):\n    return c ** 0.5\n",
                                    "login.py": b"<!DOCTYPE html><html><body>Please sign in</body></html>",
                                    "notebook.ipynb": nb, "README.md": b"# Released\nrun analysis.py\n", "huge.py": big,
                                    "bundle.zip": _zip({"rows.csv": "a\n2\n", "src/model.py": "class M: pass\n"})})
        try:
            src = {"source": f"http://{host}/records/5", "include": ["*.csv", "*.zip"]}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                man = fetcher.main({"REFEREE_CAP": str(1 << 30), "REFEREE_SOURCES": json.dumps([src])}, out_dir=str(td / "data"),
                                   tmp=str(td / "tmp"), cache=str(td / "cache"))
        finally:
            fetcher.RECORD_APIS.pop(host, None)
            srv.shutdown()
        on_disk = sorted(p.relative_to(td / "data").as_posix() for p in (td / "data").rglob("*") if p.is_file())
        assert on_disk == ["0/data.csv", "0/rows.csv"], ("code never reaches /work/data", on_disk)
        listed = {x["path"]: x for x in man.get("record_src") or []}
        assert set(listed) == {"0/analysis.py", "0/notebook.ipynb", "0/README.md", "0/huge.py", "0/src/model.py"}, sorted(listed)
        assert listed["0/huge.py"].get("cut") and listed["0/huge.py"]["bytes"] <= fetcher.SRC_FILE, listed["0/huge.py"]
        assert any(c.startswith("0/login.py") for c in man.get("record_src_cut") or []) and man.get("record_src_n_cut") == 1, man.get("record_src_cut")
        # the host side: execute.fetch collects the container's stdout and writes the text under checks/<id>/record_src
        root, plan = td / "proj", [{**src, "required": True}]
        real = execute.docker_status, execute.start, execute.collect
        execute.docker_status = lambda: (True, "")
        execute.start = lambda *a, **k: {"container": "x"}
        execute.collect = lambda rec, timeout: {**rec, "returncode": 0, "stdout": out.getvalue(), "stderr": "", "seconds": 1}
        try:
            assert execute.fetch(state.Config(), root, "C1", plan) is None
            d = execute.fetch(state.Config(), root, "C1", plan)
        finally:
            execute.docker_status, execute.start, execute.collect = real
        rs = root / "checks" / "C1" / "record_src"
        assert d and {x["path"] for x in d.get("record_src") or []} == set(listed), d and d.get("record_src")
        text = (rs / "0" / "notebook.ipynb").read_text(encoding="utf-8")
        assert "q = fit(c)" in text and '"cells"' not in text, text                         # the cells' sources, as plain text
        assert (rs / "0" / "src" / "model.py").read_text(encoding="utf-8") == "class M: pass\n"
        for x in d["record_src"]:
            assert hashlib.sha256((rs / x["path"]).read_bytes()).hexdigest() == x["sha256"], x
        assert not d.get("record_src_lost"), d.get("record_src_lost")
        # a stdout whose text line was cut off: the listed files that never arrived are recorded, never silently absent
        execute.docker_status, execute.start = (lambda: (True, "")), (lambda *a, **k: {"container": "y"})
        execute.collect = lambda rec, timeout: {**rec, "returncode": 0, "seconds": 1, "stderr": "", "stdout": "\n".join(
            ln for ln in out.getvalue().splitlines() if not ln.startswith("REFEREE_RECORD_SRC "))}
        try:
            assert execute.fetch(state.Config(), root, "C2", [{**plan[0], "include": ["*.csv", "*.zip", "x"]}]) is None
            d2 = execute.fetch(state.Config(), root, "C2", [{**plan[0], "include": ["*.csv", "*.zip", "x"]}])
        finally:
            execute.docker_status, execute.start, execute.collect = real
        assert d2["record_src"] == [] and sorted(d2.get("record_src_lost") or []) == sorted(listed), d2.get("record_src_lost")


def test_record_src_from_a_container_is_written_only_inside_its_folder_and_only_as_listed():
    import base64
    import zlib
    with tempfile.TemporaryDirectory() as t:
        cdir = Path(t) / "checks" / "C1"
        good = "print('ok')\n"
        items = [{"dir": "0", "path": "a.py", "text": good}, {"dir": "0", "path": "../../evil.py", "text": "x"},
                 {"dir": "0", "path": "b.py", "text": "tampered"}]
        line = "REFEREE_RECORD_SRC " + base64.b64encode(zlib.compress(json.dumps(items).encode())).decode()
        listing = [{"path": "0/a.py", "bytes": len(good), "sha256": hashlib.sha256(good.encode()).hexdigest()},
                   {"path": "0/../../evil.py", "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()},
                   {"path": "0/b.py", "bytes": 2, "sha256": hashlib.sha256(b"original").hexdigest()}]
        kept = execute.record_src(cdir, line + "\n", listing)
        assert [x["path"] for x in kept] == ["0/a.py"], kept
        assert not (Path(t) / "evil.py").exists() and not (cdir / "record_src" / "0" / "b.py").exists()

# --- 2026-10-02 diagnosis of the 10-01 run: claims and failures kept apart from what REFEREE chose ---------------------
def _ready(td: Path, pages=None):
    """A project past its lenses and critic (no concerns), on DATA_PAGES unless `pages` is given."""
    cfg, pid = _project_pages(td, pages or DATA_PAGES)
    state.write_json(td / ".gpu.json", False)
    for lens in tasks.LENSES:
        _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
    _seal(cfg, pid, "critic", {"reviews": []}, td)
    return cfg, pid, td / pid


def _audit_ok(cfg, pid, td, cid):
    """An independent audit that finds the failure stands (the quote it relies on re-found)."""
    _seal(cfg, pid, f"audit:{cid}", {"verdict": "STANDS", "depends_on": [], "quotes": [THM]}, td)


def test_a_violation_of_a_changed_claim_is_never_listed_as_a_failure_found():
    """Oct-02 transformer review: '## Failures found' listed C2/C3/C8, whose relations REFEREE's planner supplied (each
    claim READING_CHANGED), beside C6's counterexample to the printed exponent, with nothing telling them apart."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        state.write_json(root / "released.json", [{"path": "results/preds.csv"}])
        sup = dict(_run("A"), kind="RELEASED_DATA", basis="predictions", criterion="supplied")
        sup.pop("test")
        cert = {"id": "B", "kind": "CERTIFICATE", "claim_quote": THM, "statement_quote": THM, "role": "target",
                "covers": ["the bound"]}
        _seal(cfg, pid, "plan", {"checks": [sup, cert], "central_claims": [
            _claim(checks=["A"]), {"quote": THM, "claim_type": "theory", "checks": ["B"], "scope": ["the bound"]}]}, td)
        dev = {"printed": "", "used": "REFEREE's planner supplied the decision criterion", "why": "", "changes_claim": True}
        state.write_json(root / "checks" / "C1" / "check.json", {"deviations": [dev]})
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "RELATION_VIOLATED", "values": [-1.0],
                                                                   "reason": "acc_a > acc_b does not hold"})
        state.write_json(root / "checks" / "C2" / "outcome.json", {"status": "COUNTEREXAMPLE_FOUND", "values": [1],
                                                                   "reason": "8 admissible instances violate it"})
        _audit_ok(cfg, pid, td, "C2")
        x = tasks._Ctx(cfg, pid)
        led = report.ledger(x)
        md = report.render(x, led, None)
        found = md.split("## Failures found", 1)[1].split("\n## ", 1)[0]
        assert "**C2**" in found and "**C1**" not in found, found
        changed = md.split("## Violations of a changed claim", 1)[1].split("\n## ", 1)[0]
        assert "**C1**" in changed and "about the changed claim" in changed, changed


def test_a_compatibility_condition_is_the_harness_protocol_never_a_change_of_the_engineering_claim():
    """Oct-06 GRACE K2: "the GRACE layer can be substituted for an FCNN layer and trained end-to-end" was tested by the
    compatibility test invariant 23 prescribes (swapped into two architectures, the loss fell in 3 of 3 runs each), but
    its per-run condition was recorded as "REFEREE's planner supplied the decision criterion" (claim-changing), so the
    claim read READING_CHANGED — no engineering claim could ever be verified. The condition is the harness's own
    protocol for an engineering claim; it speaks for compatibility only (a performance claim never takes it)."""
    rel = "loss_first - loss_last > 0"
    compat = {"kind": "RECONSTRUCTION", "test": "compatibility", "target": {"relation": rel}}
    d = tasks._supplied(compat)
    assert d["changes_claim"] is False and "compatibility" in d["used"] and rel in d["used"], d
    assert tasks._supplied({**compat, "test": "performance"})["changes_claim"] is True        # a performance criterion
    devs, crit = tasks._two_keys({**compat, "criterion": "stated"}, {"deviations": []}, {"criterion": "supplied"})
    assert not any(x["changes_claim"] for x in devs), devs
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        chk = {"id": "A", "kind": "RECONSTRUCTION", "test": "compatibility", "role": "target", "criterion": "supplied",
               "claim_quote": CIFAR, "covers": ["CIFAR-10-C"], "target": {"quote": CIFAR, "relation": rel},
               "define": {"loss_first": "first logged loss", "loss_last": "last logged loss"}}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [_claim(checks=["A"], claim_type="engineering")]}, td)
        old = {"printed": "", "used": f"REFEREE's planner supplied the decision criterion ({rel}) for a claim whose "
               "sentence states no such comparison or number", "why": "", "changes_claim": True}   # recorded before the fix
        state.write_json(root / "checks" / "C1" / "check.json", {"test": "compatibility", "deviations": [old]})
        state.write_json(root / "checks" / "C1" / "outcome.json", {
            "status": "RELATION_HOLDS", "values": [0.83, 0.84, 0.82], "evidence": "PAPER_DERIVED_IMPLEMENTATION",
            "reason": "the condition held in every run", "runs": 3, "execution": {"runs_planned": 3, "runs_ended": 3,
                                                                                  "runs_exited_ok": True}})
        led = report.ledger(tasks._Ctx(cfg, pid))
        cc = led["central_claims"][0]
        assert cc["claim_status"] == "SUPPORT_FOUND", cc["claim_status"]
        assert cc["decision"]["decision"] == "VERIFIED", cc["decision"]


def test_a_withdrawn_follow_up_plan_never_takes_its_claims_out_of_the_accounting():
    """Oct-01 PPRM: the operator withdrew a follow-up plan made on failed searches; the re-planned round did not list two
    central claims the withdrawn one had added ("PPRM can reduce the time to alarm by making use of the..."), and they were
    in no final ledger or review — a claim once sealed vanished because its round was planned again."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        run = lambda cid, q=CIFAR, cov="CIFAR-10-C": {"id": cid, "kind": "RECONSTRUCTION", "claim_quote": q, "covers": [cov],
                                                     **RUN, "target": {"quote": q, "relation": "acc_a > acc_b"}}
        _seal(cfg, pid, "plan", {"checks": [run("R")], "central_claims": [_claim()]}, td)
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "INCONCLUSIVE", "reason": "x"})
        added = {"quote": MMLU, "claim_type": "performance", "checks": ["F1"], "scope": ["MMLU"]}
        _seal(cfg, pid, "plan:2", {"checks": [run("F1", MMLU, "MMLU")], "central_claims": [added]}, td)
        assert tasks.reopen(cfg, pid, "plan:2", "planned on failed searches")["withdrawn"] == "plan:2"
        # Between the withdrawal and the new round, the claim is still accounted, and the new round is told of it.
        led = report.ledger(tasks._Ctx(cfg, pid))
        assert MMLU in [c["quote"] for c in led["central_claims"]], [c["quote"] for c in led["central_claims"]]
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and owed[0]["id"] == "plan:2"
        assert MMLU in Path(owed[0]["prompt"]).read_text(encoding="utf-8").split("Still undecided", 1)[1]
        _seal(cfg, pid, "plan:2", {"checks": [run("F1")], "central_claims": [_claim(checks=["F1"])]}, td)   # it is not listed again
        led = report.ledger(tasks._Ctx(cfg, pid))
        cc = next(c for c in led["central_claims"] if c["quote"] == MMLU)
        assert cc["claim_status"] == "NOT_CHECKED" and "withdrawn" in cc["why_unchecked"], cc
        assert cc["completion"]["not_run"][0]["blocker"] == "withdrawn", cc["completion"]["not_run"]
        md = report.render(tasks._Ctx(cfg, pid), led, None)
        assert MMLU in md.split("## Not checked or not decided", 1)[1]
        # a withdrawn plan whose bytes changed after it was set aside is not read as one
        f = root / "sealed" / "plan__2.withdrawn.1.json"
        f.write_text(f.read_text(encoding="utf-8").replace("MMLU", "MMLX"), encoding="utf-8")
        assert MMLU not in [c["quote"] for c in report.ledger(tasks._Ctx(cfg, pid))["central_claims"]]


def test_a_follow_up_quote_inside_a_first_round_claim_is_that_claim():
    """Probe on the 10-01 PPRM plan: a follow-up quote that is a sub-span of a first-round claim sealed as a new `theory`
    claim beside the `performance` original — a narrower duplicate that escaped the no-retype rule and counted twice."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        _seal(cfg, pid, "plan", {"checks": [_run()], "central_claims": [_claim()]}, td)
        x = tasks._Ctx(cfg, pid)
        part = "beats method B on CIFAR-10-C with ResNet-32"
        assert "never retypes" in _refused(lambda: _plan(x, [_run("F1")], [_claim(quote=part, checks=["F1"],
                                                                                  claim_type="theory")], tid="plan:2"))
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "INCONCLUSIVE", "reason": "x"})
        _seal(cfg, pid, "plan:2", {"checks": [_run("F1")], "central_claims": [_claim(quote=part, checks=["F1"])]}, td)
        plan = tasks._Ctx(cfg, pid).plan()
        assert len(plan["central_claims"]) == 1 and plan["central_claims"][0]["checks"] == ["C1", "C7"], plan["central_claims"]


def test_central_claims_past_the_cap_are_refused_never_silently_cut():
    """claims_in[:8] cut a 9th central claim on every attempt and recorded nothing (probe on HEAD da068d1)."""
    with tempfile.TemporaryDirectory() as t:
        lines = [f"Claim number {w} is shown by the experiments here." for w in
                 ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")]
        cfg, pid, x = _x(Path(t), ("\n".join(lines),))
        claims = [{"quote": q, "claim_type": "theory", "checks": [], "scope": ["it"], "why_unchecked": "no check fits",
                   "blocker": "other"} for q in lines]
        assert "at most 8" in _refused(lambda: _plan(x, [], claims))
        rec = _plan(x, [], claims, final=True)
        assert len(rec["central_claims"]) == 8 and any(lines[8][:30] in d["why"] for d in rec["dropped"]), rec["dropped"]


def test_a_follow_up_claim_may_rest_on_a_check_that_already_ran():
    """Oct-01 PPRM: the follow-up claim "outperformed purely supervised or unsupervised baselines" omitted channel
    equalization as `other` ("already run in round 1 as C3"): a follow-up claim could not link a first-round check, so a
    scope item that ran read "not run"."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        _seal(cfg, pid, "plan", {"checks": [_run()], "central_claims": [_claim()]}, td)
        x = tasks._Ctx(cfg, pid)
        new = {"quote": GPT, "claim_type": "performance", "checks": ["C1", "F1"], "scope": ["CIFAR-10-C", "MMLU"]}
        f1 = _run("F1", covers=("MMLU",), claim_quote=GPT, target={"quote": GPT, "relation": "acc_a > acc_b"})
        rec = _plan(x, [f1], [new], tid="plan:2")
        assert rec["central_claims"][0]["checks"] == ["C1", "C7"], rec["central_claims"]
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "RELATION_HOLDS", "values": [1.0], "runs": 3})
        _seal(cfg, pid, "plan:2", {"checks": [f1], "central_claims": [new]}, td)
        state.write_json(root / "checks" / "C7" / "outcome.json", {"status": "RELATION_HOLDS", "values": [1.0], "runs": 3})
        led = report.ledger(tasks._Ctx(cfg, pid))
        row = next(c for c in led["central_claims"] if c["quote"] == GPT)["completion"]
        assert row["scope_not_run"] == [] and row["ran"] == ["C1", "C7"], row


def test_a_failure_counts_only_after_an_independent_audit_and_never_when_it_rests_on_an_open_reading():
    """Oct-01 conformal: C9's sampler 'counterexample' held only when a walk may pass through t and come back — under first
    arrival (the paper's "repeat this till u = t") every instance had zero discrepancy, an output the harness never read —
    and C6's 'ratio not exactly 1' rested on a min-cut procedure substituted for the LP without the paper's trimming step,
    marked not claim-changing. Both read FAILURE_FOUND: nobody looked at a failure once it was found."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        cert = {"id": "B", "kind": "CERTIFICATE", "claim_quote": THM, "statement_quote": THM, "role": "target",
                "covers": ["the bound"]}
        _seal(cfg, pid, "plan", {"checks": [cert], "central_claims": [
            {"quote": THM, "claim_type": "theory", "checks": ["B"], "scope": ["the bound"]}]}, td)
        dev = {"printed": "", "used": "walks may pass through the target and return", "why": "open", "changes_claim": False}
        state.write_json(root / "checks" / "C1" / "check.json", {"deviations": [dev], "script_sha256": "s"})
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "COUNTEREXAMPLE_FOUND", "values": [1, 1],
                                                                   "reason": "8 admissible instances violate it"})
        state.append_jsonl(root / "execution.jsonl", {"target": "C1", "mode": "evidence", "script_sha256": "s", "seed": 0,
                                                      "stdout": 'REFEREE_RESULT {"premises_hold": 1, "violated": 1, '
                                                                '"violated_first_arrival": 0}\n'})
        x = tasks._Ctx(cfg, pid)
        phase, owed, _ = tasks._plan(x)
        assert [o["id"] for o in owed] == ["audit:C1"], owed                  # owed before any follow-up or report
        brief = Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        assert '"violated_first_arrival": 0' in brief and "walks may pass through" in brief   # unmasked instances, deviations
        led = report.ledger(x)
        assert led["central_claims"][0]["claim_status"] == "PENDING" and led["scientific_status"] == "CHECKS_PENDING"
        x = tasks._Ctx(cfg, pid)
        assert "verdict" in _refused(lambda: tasks._seal_audit(x, "audit:C1", {"verdict": "maybe"}, final=False))
        assert "`quotes`" in _refused(lambda: tasks._seal_audit(x, "audit:C1", {"verdict": "STANDS"}, final=False))
        unfound = {"printed": "the walk stops at its first arrival", "tested_as": "revisits", "alternative": "first arrival only"}
        assert "DEPENDS" in _refused(lambda: tasks._seal_audit(x, "audit:C1", {"verdict": "DEPENDS", "depends_on": [unfound]},
                                                               final=False))                    # not the paper's words
        nodev = {"printed": "", "deviation": 7, "tested_as": "revisits", "alternative": "first arrival only"}
        assert "`deviation`" in _refused(lambda: tasks._seal_audit(x, "audit:C1", {"verdict": "DEPENDS", "depends_on": [nodev]},
                                                                   final=False))
        assert tasks._seal_audit(x, "audit:C1", {"verdict": "STANDS"}, final=True)["verdict"] == "UNRESOLVED"   # fail closed
        dep = {"printed": "the bound holds for every n", "tested_as": "a walk may revisit the target",
               "alternative": "walks stop at their first arrival; then no instance violates it"}
        _seal(cfg, pid, "audit:C1", {"verdict": "DEPENDS", "depends_on": [dep, {**dep, "printed": "", "deviation": 0}]}, td)
        x = tasks._Ctx(cfg, pid)
        led = report.ledger(x)
        assert led["central_claims"][0]["claim_status"] == "READING_CHANGED", led["central_claims"][0]["claim_status"]
        assert led["scientific_status"] == "CENTRAL_READING_CHANGED"
        md = report.render(x, led, None)
        assert "## Failures found" not in md
        rest = md.split("## Failures that rest on a reading or choice", 1)[1].split("\n## ", 1)[0]
        assert "**C1**" in rest and "walks stop at their first arrival" in rest and "declared deviation 0" in rest, rest
        assert report.unearned("C1 is a counterexample to Theorem 1.", led)          # a failure word it no longer earns
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and "walks stop at their first arrival" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as t:                  # the same failure, audited STANDS: it counts
        td = Path(t)
        cfg, pid, root = _ready(td)
        _seal(cfg, pid, "plan", {"checks": [cert], "central_claims": [
            {"quote": THM, "claim_type": "theory", "checks": ["B"], "scope": ["the bound"]}]}, td)
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "COUNTEREXAMPLE_FOUND", "values": [1],
                                                                   "reason": "8 admissible instances violate it"})
        _audit_ok(cfg, pid, td, "C1")
        led = report.ledger(tasks._Ctx(cfg, pid))
        assert led["scientific_status"] == "CENTRAL_FAILURE_FOUND" and not report.unearned("C1 is a counterexample.", led)
        state.write_json(root / "checks" / "C2" / "outcome.json", {"status": "RELATION_VIOLATED", "values": [-1]})
        assert not report.needs_audit(root, {"id": "C2", "kind": "RECONSTRUCTION", "role": "supporting"})   # never counts


def test_a_configured_cap_or_a_fault_of_this_run_is_never_reported_as_a_hardware_or_data_blocker():
    """Oct-01 PPRM: C1 ended at the configured per-run limit (SH_RUN_TIMEOUT_S, a setting) and the review counted "compute 3"
    — itself, and two follow-up omissions that cited it although it never ran their items. A transient fetch fault, the
    data cap and a denied source all read `data`; a check cut by SH_MAX_CHECKS left its item `unstated` (the planner's)."""
    rb = lambda cid, res: {"id": cid, "kind": "RECONSTRUCTION", "role": "target", "status": "BLOCKED", "resource": res,
                           "reason": f"RESOURCE BLOCKER: {res}", "values": None, "covers": []}
    db = lambda cid, cls, det="": {"id": cid, "kind": "RECONSTRUCTION", "role": "target", "status": "INCONCLUSIVE", "values": None,
                                  "reason": "DATA ...", "covers": [], "data_blocker": [{"source": "s", "class": cls, "detail": det}]}
    out = {b["item"]: b for b in report._blockers({"checks": ["C1"]}, [
        rb("C1", "per_run_timeout"), rb("C2", "memory"), db("C3", "transient"), db("C4", "missing"),
        db("C5", "storage", "passed the storage cap (SH_MAX_DATA_GB)"), db("C6", "denied")])}
    assert out["C1"]["blocker"] == "cap" and out["C1"]["class"] == "per_run_timeout", out["C1"]
    assert out["C2"]["blocker"] == "compute" and out["C3"]["blocker"] == "fault" and out["C4"]["blocker"] == "data"
    assert out["C5"]["blocker"] == "cap" and out["C6"]["blocker"] == "cap", (out["C5"], out["C6"])
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        _seal(cfg, pid, "plan", {"checks": [_run(), _run("M", covers=("MMLU",), claim_quote=MMLU,
                                                         target={"quote": MMLU, "relation": "acc_a > acc_b"})],
                                 "central_claims": [_claim(), _claim(quote=MMLU, checks=["M"], scope=["MMLU"])]}, td)
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "BLOCKED", "resource": "per_run_timeout",
                                                                   "reason": "RESOURCE BLOCKER: a single run exceeded ..."})
        state.write_json(root / "checks" / "C2" / "outcome.json", {"status": "INCONCLUSIVE", "reason": "DATA ACQUISITION FAILED",
                                                                   "data_blocker": [{"source": "s", "class": "transient"}]})
        x = tasks._Ctx(cfg, pid)
        om = lambda item, b, **kw: {"item": item, "why": "it could not run here", "blocker": b, **kw}
        new = lambda o, scope: {"quote": GPT, "claim_type": "performance", "checks": ["F1"], "scope": scope, "omitted": [o]}
        f1 = _run("F1", covers=("GPT labeler",), claim_quote=GPT, target={"quote": GPT, "relation": "acc_a > acc_b"})
        err = _refused(lambda: _plan(x, [f1], [new(om("CIFAR-10-C", "compute", failed_checks=["C1"]), ["GPT labeler", "CIFAR-10-C"])],
                                     tid="plan:2"))
        assert "`cap`" in err and "SH_RUN_TIMEOUT_S" in err, err                      # a setting, not this host's hardware
        rec = _plan(x, [f1], [new(om("CIFAR-10-C", "cap", failed_checks=["C1"]), ["GPT labeler", "CIFAR-10-C"])], tid="plan:2")
        assert not rec["central_claims"][0]["omitted"][0].get("unverified"), rec["central_claims"][0]["omitted"]
        err = _refused(lambda: _plan(x, [f1], [new(om("Imagenet", "cap", failed_checks=["C1"]), ["GPT labeler", "Imagenet"])],
                                     tid="plan:2"))
        assert "did not run" in err, err                                              # evidence only for what it ran
        err = _refused(lambda: _plan(x, [f1], [new(om("MMLU", "data", failed_checks=["C2"]), ["GPT labeler", "MMLU"])],
                                     tid="plan:2"))
        assert "fault of this run" in err, err                                        # a transient fault is no data fact
        assert "no configured cap" in _refused(lambda: _plan(x, [f1], [new(om("GPT labeler 2", "cap"),
                                                                           ["GPT labeler", "GPT labeler 2"])], tid="plan:2"))
        # a plan sealed before this rule (the 10-01 PPRM follow-up) is flagged when its ledger is written
        sealed = {"checks": [{"id": "C1", "covers": ["CIFAR-10-C"]}], "central_claims": [{"quote": GPT, "omitted": [
            om("CIFAR-10-C", "compute", failed_checks=["C1"]), om("Imagenet", "compute", failed_checks=["C1"]),
            om("MMLU", "data", failed_checks=["C2"])]}]}
        report._failed_searches(root, sealed)
        u = [o.get("unverified", "") for o in sealed["central_claims"][0]["omitted"]]
        assert "configured cap" in u[0] and "not this item" in u[1] and "fault" in u[2], u
    with tempfile.TemporaryDirectory() as t:                  # a check cut by SH_MAX_CHECKS: the harness says so
        cfg, pid, x = _x(Path(t))
        cfg.max_checks = 1
        claims = [_claim(), _claim(quote=MMLU, checks=["M"], scope=["MMLU"])]
        m = _run("M", covers=("MMLU",), claim_quote=MMLU, target={"quote": MMLU, "relation": "acc_a > acc_b"})
        assert "SH_MAX_CHECKS" in _refused(lambda: _plan(x, [_run(), m], claims))
        rec = _plan(x, [_run(), m], claims, final=True)
        o = rec["central_claims"][1]["omitted"]
        assert o and o[0]["blocker"] == "cap" and o[0]["basis"] == "harness" and "SH_MAX_CHECKS" in o[0]["why"], o
        for i in range(cfg.max_discoveries):                 # the search budget spent: `cap` stands on that fact
            state.append_jsonl(x.root / "discovery.jsonl", {"id": f"D{i + 1}", "query": f"q{i}", "results": {"zenodo": {"candidates": []}}})
        cfg.max_checks = 6
        rec = _plan(x, [_run()], [_claim(scope=["CIFAR-10-C", "Qwen 7B"], omitted=[om("Qwen 7B", "cap")])])
        assert "SH_MAX_DISCOVERIES" in rec["central_claims"][0]["omitted"][0]["cap"], rec["central_claims"][0]["omitted"]
        whole = {"quote": MMLU, "claim_type": "performance", "checks": [], "scope": ["MMLU"], "why_unchecked": "searches",
                 "blocker": "other"}
        assert _plan(x, [_run()], [_claim(), whole])["central_claims"][1]["budget_spent"]   # a whole claim too


def test_a_data_fetch_or_a_draft_never_takes_an_evidence_slot():
    """Oct-01 run: from 07:30 to 08:16Z two fetches were always running and no evidence run started in any of the three
    papers — PPRM C5/C6 (6-15 s runs, approved by 07:37) waited 41 minutes — because the SH_PARALLEL pool counted every
    REFEREE container except installs."""
    real = execute._docker
    running = {"label=referee.mode=evidence": "", "label=referee.mode=try": "", "label=referee=1": "f1 f2"}
    execute._docker = lambda argv, timeout: (0, running.get(argv[-1], ""))
    try:
        cfg = state.Config()
        cfg.parallel = 2
        assert execute.evidence_slots(cfg) == 2, execute.evidence_slots(cfg)           # two fetches take no slot
        running.update({"label=referee.mode=try": "t1"})
        assert execute.evidence_slots(cfg) == 1      # a draft runs the real script on the host's CPU and memory: it does
        running.update({"label=referee.mode=evidence": "e1"})
        assert execute.evidence_slots(cfg) == 0
        execute._docker = lambda argv, timeout: (1, "")
        assert execute.evidence_slots(cfg) == 0                                         # a daemon that cannot answer: none
    finally:
        execute._docker = real


def test_the_gpu_is_held_only_by_a_run_that_can_use_it():
    """Oct-01 PPRM: every RECONSTRUCTION on a GPU host was given the GPU, so CPU-only simulations and their drafts queued
    behind a ResNet-1202 run — one expensive experiment serialized every independent one."""
    numpy = "import numpy as np\nprint('REFEREE_RESULT', {})\n"
    assert not execute.wants_gpu(numpy, False)
    assert execute.wants_gpu("import torch\n", False) and execute.wants_gpu("from transformers import AutoModel\n", False)
    assert execute.wants_gpu("# REFEREE_PACKAGES: torch==2.3.0\nimport numpy\n", False)
    assert execute.wants_gpu("m = __import__('tor' + 'ch')\n", False)                  # a dynamic import: unknown is yes
    assert execute.wants_gpu(numpy, True)                                               # it may drive the authors' code
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects = Path(t)
        state.write_json(Path(t) / ".gpu.json", True)
        assert not execute.gpu_run(cfg, {"kind": "RECONSTRUCTION", "gpu": False})
        assert execute.gpu_run(cfg, {"kind": "RECONSTRUCTION"})                         # an older record: yes, one at a time
        assert execute.gpu_run(cfg, {"kind": "AUTHOR_CODE", "gpu": False})              # authors' code: always offered it
        assert not execute.gpu_run(cfg, {"kind": "CERTIFICATE"})


def test_a_task_no_worker_can_answer_ends_its_check_and_the_review_goes_on():
    """Oct-01 PPRM: a permission block kept two workers from answering gen:C7.2; the workflow gave the task up and then
    stopped — although C8's draft and C9's evidence run were in flight — because the harness reported executions running
    only when no task was owed. The paper never reached its follow-up or report until the operator relaunched it."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        cert = {"id": "B", "kind": "CERTIFICATE", "claim_quote": THM, "statement_quote": THM, "role": "target",
                "covers": ["the bound"]}
        _seal(cfg, pid, "plan", {"checks": [cert], "central_claims": [
            {"quote": THM, "claim_type": "theory", "checks": ["B"], "scope": ["the bound"]}]}, td)
        real = execute.script_env
        execute.script_env = lambda *a, **k: (td / ".script-env", {"ok": True, "detail": "baseline"})
        try:
            phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
            assert [o["id"] for o in owed] == ["gen:C1.1"], owed
            assert "not a pending task" in tasks.abandon(cfg, pid, "gen:C9.1", "x")["error"]
            out = tasks.abandon(cfg, pid, "gen:C1.1", "two workers produced no sealable answer (a permission block)")
            assert out["abandoned"] == "gen:C1.1", out
            o = state.read_json(root / "checks" / "C1" / "outcome.json")
            assert o["status"] == "INCONCLUSIVE" and "permission block" in o["reason"] and "nothing about the paper" in o["reason"], o
            assert tasks._plan(tasks._Ctx(cfg, pid))[0] == "plan"                       # the review goes on: its follow-up
            assert tasks.reopen(cfg, pid, "C1", "the permission block was lifted")["reopened"] == "C1"   # never a finding
        finally:
            execute.script_env = real
    assert tasks.blocked_reason(["C8", "C9"]) .startswith("executions running")         # said whenever anything runs


def test_a_record_file_the_plan_neither_requests_nor_excludes_is_a_gap_of_the_acquisition():
    """Oct-01 transformer: Zenodo 18281512 lists 25 files; the plan took 20 although its own `why` said 24 task-by-model
    CSVs. The acquisition read complete against that plan, and after the post-run fix the 5 were shown but counted for
    nothing: a claim could still read RAN_AS_SPECIFIED on the 20."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        rec = "https://zenodo.org/records/18281512"
        state.append_jsonl(x.root / "discovery.jsonl", {"id": "F1", "files_of": rec, "api": "x", "files": [
            {"name": n, "bytes": 1, "md5": ""} for n in ("a.csv", "b.csv", "c.csv", "README.md")]})
        acq = {"source": rec, "cited_in": "paper", "include": ["a.csv", "b.csv"]}
        chk = lambda **a: dict(_run(), kind="RELEASED_DATA", basis="predictions", acquire=[{**acq, **a}])
        err = _refused(lambda: _plan(x, [chk()], [_claim()]))
        assert "c.csv" in err and "`exclude`" in err and "README" not in err, err          # code and docs are text, not data
        assert "reason" in _refused(lambda: _plan(x, [chk(exclude=[{"pattern": "c.csv"}])], [_claim()]))
        why = "the claim compares tasks a and b only"
        sealed = _plan(x, [chk(exclude=[{"pattern": "c.csv", "why": why}])], [_claim()])
        assert sealed["checks"][0]["acquire"][0]["exclude"] == [{"pattern": "c.csv", "why": why}]
        assert sealed["checks"][0]["acquire"][0]["listed_at_seal"] is True               # the planner was shown the listing
        root = x.root
        data = {"sources": [{"source": rec, "dir": "0", "listing": ["a.csv", "b.csv", "c.csv", "README.md"], "admitted_files": 2,
                             "admitted": [{"file": "a.csv"}, {"file": "b.csv"}]}], "n_files": 2, "fetched_at": "t"}
        state.write_json(root / "checks" / "C1" / "data.json", data)
        c = {"id": "C1", "kind": "RELEASED_DATA", "acquire": [{**acq, "listed_at_seal": True}],
             "data_identity": {"a": {"matches": True}}, "values": [1]}
        gaps = report._data_changed(c, root)                                             # sealed leniently, on its last attempt
        assert any("c.csv" in g and "neither requested nor excluded" in g for g in gaps), gaps
        c["acquire"] = [{**acq, "listed_at_seal": True, "exclude": [{"pattern": "c.csv", "why": why}]}]
        assert report._data_changed(c, root) == []                                       # left out for a stated reason
        lines = report._unrequested_lines({**c, "not_requested": execute.unrequested(root, data)})
        assert any("excluded by the plan" in ln and why in ln for ln in lines), lines
        c["acquire"] = [acq]       # a listing only the fetcher saw (a hub repo's other configurations): shown, never a gap
        assert report._data_changed(c, root) == []


def test_a_scope_entry_that_lists_several_datasets_is_several_items():
    """Oct-01 PPRM: one scope item named five benchmarks, C7 covered it, and Social-IQA never arrived; only the script's
    own deviation and its REFEREE_DATA line (`not_available: ['Social-IQA']`) showed the dropped dataset."""
    assert tasks._parts("MMLU, CMExam, CommonsenseQA, Social-IQA and PubMedQA benchmark questions") == [
        "MMLU", "CMExam", "CommonsenseQA", "Social-IQA", "PubMedQA benchmark questions"]
    assert tasks._parts("Qwen predictors Q2.5-VL3B, Q2-VL7B and Q2.5-VL32B") == ["Qwen predictors Q2.5-VL3B", "Q2-VL7B", "Q2.5-VL32B"]
    for one in ("Airport to Center trips (about 300, 40%/60% mixture, 100 train / 100 test)", "1,000 held-out samples",
                "increasing psi_t and non-monotonic psi_t", "core density alpha in {0.2,0.4,0.6,0.8}"):
        assert tasks._parts(one) == [one], (one, tasks._parts(one))
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, x = _x(Path(t))
        five = "MMLU, CMExam, CommonsenseQA, Social-IQA and PubMedQA"
        m = _run("M", covers=(five,), claim_quote=MMLU, target={"quote": MMLU, "relation": "acc_a > acc_b"})
        claim = _claim(quote=MMLU, checks=["M"], scope=[five])
        assert "Social-IQA" in _refused(lambda: _plan(x, [m], [claim]))
        rec = _plan(x, [m], [claim], final=True)
        assert rec["central_claims"][0]["scope"] == ["MMLU", "CMExam", "CommonsenseQA", "Social-IQA", "PubMedQA"]
        cc = {**rec["central_claims"][0], "checks": ["C1"]}
        ran = {"id": "C1", "kind": "RECONSTRUCTION", "role": "target", "state": "COMPLETED", "status": "RELATION_HOLDS",
               "values": [1.0], "deviations": [], "covers": [five], "data_changed": [], "reason": "",
               "data_identity": {"question_pool": {"matches": True, "not_available": ["Social-IQA"]}}}
        row = report._completion_row({**cc, "page": 1}, {"C1": ran})
        assert row["scope_not_run"] == ["Social-IQA"] and row["experiment"] == "RAN_PARTIAL", row
        b = next(b for b in row["not_run"] if b["item"] == "Social-IQA")
        assert "REFEREE_DATA" in b["why"], b
        # as the 10-01 C7 printed it: under `observed`; an old, unsplit scope entry naming it is not run either
        ran["data_identity"] = {"question_pool": {"matches": False, "observed": {"not_available": ["Social-IQA"]}}}
        row = report._completion_row({**cc, "scope": [five], "page": 1}, {"C1": ran})
        assert row["scope_not_run"] == [five], row


def test_the_criterion_and_what_changes_the_claim_need_the_verifiers_key_too():
    """Oct-01: transformer C7 tested "Such a formula does not fit the empirical data well" by a planner relation labelled
    `stated` (C1 had labelled the same relation `supplied`); conformal C4/C6 computed "the LP solution" by a substituted
    min-cut procedure without the paper's trimming step, marked not claim-changing by its author, and C6 read
    FAILURE_FOUND. Each label was one model's word; now the verifier states both independently, and either key moves the
    result to the changed claim — never the other way."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid, root = _ready(td)
        _seal(cfg, pid, "plan", {"checks": [_run("R")], "central_claims": [_claim()]}, td)
        cdir = root / "checks" / "C1"
        script = "import argparse\np = argparse.ArgumentParser(); p.add_argument('--seed', type=int); p.parse_args()\n"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "script.1.py").write_text(script, encoding="utf-8")
        sha = state.sha256(script)
        devs = [{"printed": "", "used": "the LP solution is computed by a parametric min-cut", "why": "equivalent", "changes_claim": False},
                {"printed": "", "used": "a grid of phi values", "why": "open", "changes_claim": False}]
        seals = state.read_json(root / "seals.json", {})
        p = root / "sealed" / "gen__C1.1.json"
        state.write_json(p, {"script_sha256": sha, "runs": 3, "runs_quote": "", "metric": "", "outputs": ["acc_a", "acc_b"],
                             "deviations": devs, "bindings": [], "stochastic": True, "seed_flow": True})
        seals["gen:C1.1"] = state.sha256(p.read_bytes())
        state.write_json(root / "seals.json", seals)
        x = tasks._Ctx(cfg, pid)
        ok = {"verdict": "APPROVE", "quotes": [CIFAR], "criterion": "stated", "claim_changing": []}
        assert "`criterion`" in _refused(lambda: tasks._seal_verify(x, "verify:C1.1", {**ok, "criterion": ""}, final=False))
        assert "`claim_changing`" in _refused(lambda: tasks._seal_verify(
            x, "verify:C1.1", {k: v for k, v in ok.items() if k != "claim_changing"}, final=False))
        assert "indices" in _refused(lambda: tasks._seal_verify(x, "verify:C1.1", {**ok, "claim_changing": [5]}, final=False))
        strict = tasks._seal_verify(x, "verify:C1.1", {"verdict": "APPROVE", "quotes": [CIFAR]}, final=True)
        assert strict["criterion"] == "supplied" and strict["claim_changing"] == [0, 1], strict   # the strictest reading
        rec = tasks._seal_verify(x, "verify:C1.1", {**ok, "criterion": "supplied", "claim_changing": [0]}, final=False)
        p = root / "sealed" / "verify__C1.1.json"
        state.write_json(p, rec)
        seals["verify:C1.1"] = state.sha256(p.read_bytes())
        state.write_json(root / "seals.json", seals)
        real = execute.poll
        execute.poll = lambda *a, **k: False
        try:
            tasks._plan(tasks._Ctx(cfg, pid))
        finally:
            execute.poll = real
        chk = state.read_json(cdir / "check.json")
        assert chk["criterion"] == "supplied" and chk["deviations"][0]["changes_claim"] is True, chk
        assert chk["deviations"][0]["changes_claim_by"] == "verifier" and chk["deviations"][1]["changes_claim"] is False
        assert any("decision criterion" in d["used"] and d["changes_claim"] for d in chk["deviations"]), chk["deviations"]
        assert "gpu" in chk


def test_a_revision_never_drops_a_claim_changing_choice_without_saying_why():
    """Oct-01 conformal C5: round 1 declared a claim-changing deviation; rounds 2 and 3 declared none and round 3 was
    approved, with nothing recording why it went (the filter was fixed — or only its label was dropped). Self-correction
    keeps the scope: a revision that drops or relabels a claim-changing choice says what in the script changed."""
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        plan = {"checks": [{"id": "C1", "kind": "CERTIFICATE", "metric": "", "statement": "Lemma 1", "step": ""}]}
        dev = {"printed": "", "used": "trips are filtered by a 4 km radius around the airport", "why": "open", "changes_claim": True}
        prev = {"deviations": [{**dev, "page": None}], "runs": 8}
        x = type("X", (), {"root": Path(t), "paper": Paper(PAGES, ROWS), "cfg": cfg,
                           "sealed": lambda self, tid: prev if tid == "gen:C1.1" else plan, "plan": lambda self: plan})()
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0, 'premises_hold': 1, 'literal': 'holds'})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        g = {"script": script, "runs": 8, "outputs": ["violated"], "bindings": b, "deviations": []}
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.2", g, final=False))
        assert "revision_notes" in err and "4 km radius" in err, err
        assert "revision_notes" in _refused(lambda: tasks._seal_gen(x, "gen:C1.2", {**g, "deviations": [{**dev, "changes_claim": False}]},
                                                                    final=False))          # relabelled, not removed: the same
        assert "runs" in _refused(lambda: tasks._seal_gen(x, "gen:C1.2", {**g, "runs": 3, "deviations": [dev]}, final=False))
        note = {"was": "trips are filtered by a 4 km radius", "why": "the filter now reproduces the paper's 300 trips exactly"}
        rec = tasks._seal_gen(x, "gen:C1.2", {**g, "revision_notes": [note]}, final=False)
        assert rec["revisions"]["dropped_claim_changing"] == [dev["used"]] and rec["revisions"]["notes"][0]["why"] == note["why"]
        strict = tasks._seal_gen(x, "gen:C1.2", g, final=True)                         # the last attempt: carried, strictest
        assert strict.get("refused") or any(d["used"] == dev["used"] and d["changes_claim"] for d in strict["deviations"]), strict


def test_a_follow_up_rounds_re_examined_omission_is_the_one_reported():
    """Oct-01 PPRM: the follow-up round re-examined the Qwen omissions (first sealed `data` on searches that had failed);
    `merged` kept the first round's entry, so the ledger still reported the blocker the re-plan was made to replace."""
    first = {"checks": [], "dropped": [], "central_claims": [{"quote": "q", "checks": [], "why_unchecked": "",
             "omitted": [{"item": "Qwen 7B", "why": "no record", "blocker": "data", "unverified": "searches failed"}]}]}
    follow = {"checks": [], "dropped": [], "central_claims": [{"quote": "q", "checks": [], "why_unchecked": "",
              "omitted": [{"item": "Qwen 7B", "why": "the search budget is spent", "blocker": "other"}]}]}
    om = report.merged(first, follow)["central_claims"][0]["omitted"]
    assert len(om) == 1 and om[0]["blocker"] == "other" and om[0]["earlier"]["blocker"] == "data", om


# --- 2026-10-05 review: replicates, undefined units, counts ------------------------------------------
def test_replicates_equal_up_to_round_off_are_one_measurement():
    """Sep-29 changepoint C7: three seeds of a deterministic pipeline printed the same variances; a sum taken in another
    order makes them differ in the last bits. Equal-to-round-off lines are one run repeated, never three replicates
    whose standard error is ~1e-16 (that read RELATION_HOLDS with t(2)=4.3 and a band of 0)."""
    rel = "var_lb - var_km > 0"
    outs = [{"var_km": 829.023247152567 + k * 1.1e-13, "var_lb": 1205.235555555558} for k in range(3)]
    margins = [o["var_lb"] - o["var_km"] for o in outs]
    assert len(set(margins)) > 1                                          # not bit-identical
    ind = independence.assess(_rows(outs), "a", "", True)
    assert ind["state"] != "different" and not ind["independent"], ind
    r = _rel(rel, _rows(outs), margins, rng=True)
    assert r["status"] == "INCONCLUSIVE" and r["stages"]["a"]["n_independent"] == 1, r
    det = _rel(rel, _rows(outs), margins, det=True)                       # declared deterministic: one measurement
    assert det["status"] == "RELATION_HOLDS" and det["stages"]["a"]["n_independent"] == 1, det
    real = [{"var_km": 829.0 + k, "var_lb": 1205.2} for k in range(3)]    # runs that really differ stay replicates
    assert independence.assess(_rows(real), "a", "", True)["independent"]


def test_an_undefined_unit_is_counted_apart_from_a_missing_one():
    """Sep-29 changepoint C7: 14 settings where KM's estimate is undefined (no alarm in any sequence) printed no result
    line and read NOT_COMPLETED beside 14 genuinely missing ones, so the check read PARTIAL and the ledger could not say
    how many settings were expected, measured, undefined or lost. A unit that says why its quantity is undefined is
    completed and decides nothing; a unit with no line is not completed."""
    from harness.reconcile import PARTIAL
    rel = "var_lb - var_km > 0"
    out = "\n".join(['REFEREE_PROGRESS {"units": ["a", "b", "c", "d"]}',
                     'REFEREE_RESULT {"stage": "a", "var_km": 1, "var_lb": 2, "data_fingerprint": "x"}',
                     'REFEREE_RESULT {"stage": "b", "var_km": 1, "var_lb": 3, "data_fingerprint": "x"}',
                     'REFEREE_RESULT {"stage": "c", "undefined": "no alarm in any sequence: KM is undefined"}'])
    assert execute.undefined_units(out) == {"c": "no alarm in any sequence: KM is undefined"}
    staged = execute.staged_values(out, rel, "")
    errs, _ = execute.split_units(execute.units({"stdout": out}), staged, False, "no line", execute.undefined_units(out))
    assert errs == {"d": "no line"}, errs                                  # c is undefined, d is missing
    dec = lambda st, undef, errs: reconcile("RELEASED_DATA", "", [e[1] for e in st], "", {}, False, True, "", rel,
                                           staged=st, stage_errors=errs, deterministic=True, undefined=undef)
    full = dec(staged, {"c": "no alarm"}, {})
    assert full["status"] == "RELATION_HOLDS" and full["stages"]["c"]["status"] == "UNDEFINED", full
    assert full["undefined_stages"] == ["c"] and "undefined" in full["reason"]
    part = dec(staged, {"c": "no alarm"}, {"d": "no line"})
    assert part["status"] == PARTIAL and part["stages"]["d"]["status"] == "NOT_COMPLETED"
    assert dec([], {"c": "x"}, {})["status"] == "INCONCLUSIVE"            # undefined everywhere decides nothing
    # The counts reconcile: units declared = with a result + undefined + not completed; launches and independent runs apart.
    o = {**part, "values": [1, 2] * 3, "runs": 3, "execution": {"runs_planned": 3, "runs_ended": 3, "runs_exited_ok": 3},
         "units": ["a", "b", "c", "d"]}
    n = report.counts({"kind": "RELEASED_DATA", **o})
    assert (n["units_declared"], n["units_with_result"], n["units_undefined"], n["units_not_completed"]) == (4, 2, 1, 1), n
    assert n["launches"] == 3 and n["result_lines"] == 6 and n["reconciles"], n


def test_every_declared_unit_is_accounted_for_however_many():
    """Sep-29 changepoint C7 declared 248 units and printed 220; the outcome named 22 incomplete and lost 6 without a
    trace (a cap on the units kept per seed). Every declared unit ends in exactly one of: decided, undefined, not completed."""
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        cdir = root / "checks" / "C1"
        cdir.mkdir(parents=True)
        units = [f"u{i:03d}" for i in range(248)]
        printed, undef = units[:220], units[220:234]
        check = {"id": "C1", "kind": "RECONSTRUCTION", "runs": 2, "stochastic": False, "script_sha256": "s",
                 "target": {"relation": "b - a > 0"}}
        for seed in range(2):
            out = "\n".join([f'REFEREE_PROGRESS {{"units": {json.dumps(units)}}}'] +
                            [f'REFEREE_RESULT {{"stage": "{u}", "a": 1, "b": 2}}' for u in printed] +
                            [f'REFEREE_RESULT {{"stage": "{u}", "undefined": "no events"}}' for u in undef])
            staged = execute.staged_values(out, "b - a > 0", "")
            declared, un = execute.units({"stdout": out}), execute.undefined_units(out)
            errs, _ = execute.split_units(declared, staged, False, "a declared unit printed no result line", un)
            state.append_jsonl(cdir / "seeds.jsonl", {"key": "s", "seed": seed, "values": [e[1] for e in staged],
                                                       "cert": [], "staged": staged, "seconds": 1, "error": "",
                                                       "stage_errors": errs, "units": sorted(declared), "undefined": un,
                                                       "schema": [], "cohort_mismatch": [],
                                                       "detail": execute.result_detail(out, seed)})
        st: dict = {}
        execute.reuse_checkpoints(root, cdir, check, st)
        res = execute._reconciled(check, st, True, "", failure="")
        sts = [p["status"] for p in res["stages"].values()]
        assert len(sts) == 248, len(sts)
        assert sts.count("UNDEFINED") == 14 and sts.count("NOT_COMPLETED") == 14 and sts.count("RELATION_HOLDS") == 220


def test_seeded_runs_that_vary_nothing_go_back_to_their_author():
    """Sep-29 changepoint C7: a deterministic pipeline (fixed WISDM data, deterministic detectors) declared `stochastic`;
    its three seeds printed identical lines, so no stage could be decided (no replicates, no noise band). That is a defect
    of the script's declaration, not a finding: the approved script goes back to its author once, with the reason."""
    rows = _rows([{"v": 1.0}, {"v": 1.0}, {"v": 1.0}]) + _rows([{"v": 2.0}] * 3, stage="b")
    chk = {"kind": "RECONSTRUCTION", "stochastic": True, "test": "performance"}
    why = execute.varied_nothing(chk, {"detail": rows, "done_seeds": [0, 1, 2]})
    assert why and "stochastic" in why, why
    assert not execute.varied_nothing({**chk, "stochastic": False}, {"detail": rows, "done_seeds": [0, 1, 2]})
    assert not execute.varied_nothing(chk, {"detail": rows + _rows([{"v": 3.0}, {"v": 4.0}], stage="c"),
                                            "done_seeds": [0, 1, 2]})        # one stage the seed did vary: replicates there
    assert not execute.varied_nothing(chk, {"detail": rows[:1], "done_seeds": [0]})
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        c = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
             "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        _seal(cfg, pid, "plan", {"checks": [c], "central_claims": [{"quote": c["claim_quote"], "checks": ["B"],
                                                                    "claim_type": "theory", "scope": ["the loss"]}]}, td)
        script = "n = 1\nassert n\nok = n > 0\nprint('REFEREE_RESULT', {'violated': 0, 'premises_hold': 1})\n"
        b = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
             (("hypotheses", "assert n"), ("claimed_bound", "ok = n > 0"), ("instance", "n = 1"))]
        _seal(cfg, pid, "gen:C1.1", {"script": script, "runs": 1, "outputs": ["violated"], "bindings": b}, td)
        state.write_json(td / pid / "checks" / "C1" / "smoke.1.json", {"returncode": 0, "failed": False, "reached": True,
                                                                        "stdout": "", "stderr": ""})
        _seal(cfg, pid, "verify:C1.1", {"verdict": "APPROVE", "quotes": ["reaches 61.4 accuracy"], "claim_changing": []}, td)
        tasks._plan(tasks._Ctx(cfg, pid))                                    # approved: started
        assert (td / pid / "checks" / "C1" / "exec.json").exists()
        state.write_json(td / pid / "checks" / "C1" / "outcome.json", {"status": "INCONCLUSIVE", "reason": "x",
                                                                         "revisable": "the seeds varied nothing"})
        _, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        gen = [o for o in owed if o["id"] == "gen:C1.2"]       # (a follow-up offered meanwhile stays owed beside it)
        assert gen and "the seeds varied nothing" in Path(gen[0]["prompt"]).read_text(encoding="utf-8"), owed
        assert (td / pid / "checks" / "C1" / "outcome.setup.1.json").exists()   # the old outcome is kept beside it


def test_a_printed_definition_is_computed_beside_any_change_and_paper_and_code_are_stated():
    """Sep-29 label ranking C9: the script replaced the paper's top-1 probability r/sum r by a softmax (declared
    claim-changing), relegated the printed ratio to a secondary output nobody decided, and pooled four positions where the
    released analysis scores the chosen response only — so the result matched neither the paper nor the code. Now a
    claim-changing departure from printed words computes the printed version too (or says why it cannot), and the
    script states data, model, metric, baselines, preprocessing, sample size and statistics against paper and code."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        code_line = "p = np.exp(r) / np.exp(r).sum()  # softmax of the chosen response"
        _checkout(td, pid, {"analyze.py": f"import numpy as np\n{code_line}\n"})
        state.write_json(td / pid / "released.json", [{"path": "results/a.csv"}])
        plan = {"checks": [{"id": "C1", "kind": "RELEASED_DATA", "metric": "", "criterion": "stated",
                            "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}]}
        x = tasks._Ctx(cfg, pid)
        x.sealed = lambda tid: plan if tid == "plan" else None
        x.plan = lambda: plan
        script = ('d = open("results/a.csv").read()\nece = 1\nprint("REFEREE_RESULT", ece, "reading", "cohort")\n')
        binds = [{"kind": k, "impl_quote": q, "paper_quote": "reaches 61.4 accuracy"} for k, q in
                 (("dataset", 'open("results/a.csv")'), ("metric", "ece = 1"), ("comparison_target", "print("))]
        fid = [f for f in FID if f["aspect"] != "metric"]
        dev = {"printed": "We use generation of samples", "used": "a softmax of the scores", "why": "positivity",
               "changes_claim": True}
        g = {"script": script, "runs": 1, "metric": "ece", "outputs": ["ece"], "bindings": binds, "deviations": [dev]}
        metric_disagrees = {"aspect": "metric", "paper": "We use generation of samples",
                            "code": {"file": "analyze.py", "quote": code_line}, "used": "the paper's ratio", "agrees": False}
        err = _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "fidelity": fid + [metric_disagrees]}, final=False))
        assert "compute the printed version" in err and "fidelity metric" in err, err
        readings = [{"name": "paper", "source": "paper", "quote": "We use generation of samples"},
                    {"name": "code", "source": "analyze.py", "quote": code_line}]
        ok = {**g, "readings": readings, "deviations": [{**dev, "reading": "code"}],
              "fidelity": fid + [{**metric_disagrees, "reading": "code"}]}
        rec = tasks._seal_gen(x, "gen:C1.1", ok, final=False)
        assert rec["deviations"][0]["reading"] == "code" and rec["fidelity"][-1]["agrees"] is False
        assert "printed_infeasible" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {
            **ok, "readings": [], "deviations": [{**dev, "printed_infeasible": "short"}]}, final=False))
        fine = tasks._seal_gen(x, "gen:C1.1", {**g, "fidelity": FID, "deviations": [
            {**dev, "printed_infeasible": "the paper never states the grid it tuned the baselines over"}]}, final=False)
        assert fine["deviations"][0]["printed_infeasible"].startswith("the paper never")
        bad_code = {**metric_disagrees, "code": {"file": "analyze.py", "quote": "a line the code never had in it"}}
        assert "not literal text" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**ok, "fidelity": fid + [bad_code]},
                                                                      final=False))
    # A change scoped to one reading, with the printed definition computed beside it, does not move the whole check.
    c = {"id": "C1", "kind": "RELEASED_DATA", "evidence": "x", "status": "RELATION_HOLDS", "role": "target",
         "deviations": [{"changes_claim": True, "used": "softmax", "reading": "code"}],
         "reading_defs": [{"name": "paper", "source": "paper"}, {"name": "code", "source": "analyze.py"}]}
    assert report._claim_status([c]) == "SUPPORT_FOUND"
    assert report._claim_status([{**c, "reading_defs": []}]) == "READING_CHANGED"   # no printed reading beside it


def test_acquired_data_is_checked_against_the_items_the_experiment_needs():
    """Oct-01 transformer and Oct-05 review: a download plan can leave out data the experiment needs, and a dataset the
    check `covers` read as run though no line showed it was loaded. A source names the scope items it `serves`; the run
    shows, per item, the data it loaded (`covers`) or that it is `missing`, else the item reads as changed data."""
    with tempfile.TemporaryDirectory() as t:
        cfg, pid = _project(Path(t))
        x = tasks._Ctx(cfg, pid)
        errs: list = []
        tasks._acquire(x, {"covers": ["political", "movies"], "acquire": [
            {"source": "https://example.org/d.zip", "cited_in": "paper", "serves": ["political", "glass"]}]}, errs, "C1")
        assert any("glass" in e and "covers" in e for e in errs), errs
    chk = {"kind": "RELEASED_DATA", "acquire": [{"source": "s", "serves": ["political", "movies"]}], "metric": "ece"}
    assert execute.unshown_items(chk, [{"dataset": "a", "covers": ["political"], "matches": True}]) == ["movies"]
    assert execute.unshown_items(chk, [{"covers": ["political"]}, {"missing": ["movies"]}]) == []
    rec = {"stdout": 'REFEREE_DATA {"dataset": "pol", "covers": ["political"], "matches": true}\nREFEREE_RESULT {"ece": 1}',
           "stderr": ""}
    assert any("movies" in d for d in execute.result_schema(rec, chk))      # the draft goes back to its author
    c = {"id": "C1", "kind": "RELEASED_DATA", "acquire": chk["acquire"], "values": [1.0],
         "data_identity": {"pol": {"covers": ["political"], "matches": True}}}
    assert report._data_changed(c, Path(t)) and "movies" in report._data_changed(c, Path(t))[-1]


CLAIM_PAGES = ["Abstract. Our layer can replace a dense layer in any network. Our method is more accurate than the "
               "baseline on CIFAR and SVHN.\nTheorem 1. If the step size is below one half, the error decays.\n"
               "We report the mean over 5 random seeds.",
               "Table 2: Results\nMethod CIFAR SVHN\nOurs 61.4 70.2\nBase 59.3 68.0\nIn conclusion, our method is more "
               "accurate than the baseline on both datasets."]


def _claims_project(td: Path) -> tuple[state.Config, str]:
    cfg, pid = _project(td)
    state.write_json(td / pid / "paper" / "doc.json", {"pid": pid, "title": "T", "sha256": "0", "pages": CLAIM_PAGES,
                                                       "rows": [[], CLAIM_PAGES[1].splitlines()[:4]], "arxiv_id": "",
                                                       "arxiv_version": "", "source": ""})
    (td / pid / "paper" / "paper.md").write_text("\n".join(CLAIM_PAGES), encoding="utf-8")
    state.write_json(td / ".gpu.json", False)
    return cfg, pid


_K = [{"quote": "Our layer can replace a dense layer in any network", "statement": "The layer can be swapped in for a dense layer.",
       "claim_type": "engineering", "scope": ["our layer", "dense layer"], "required_evidence": "swap the layer in and train it"},
      {"quote": "Our method is more accurate than the baseline on CIFAR and SVHN",
       "also_stated": ["our method is more accurate than the baseline on both datasets"],
       "statement": "The method has higher accuracy than the baseline.", "claim_type": "performance",
       "scope": ["Ours", "Base", "CIFAR, SVHN"], "required_evidence": "the Table 2 experiment over 5 seeds",
       "evidence_in_paper": [{"quote": "Table 2: Results", "what": "two datasets"}],
       "interpretations": [{"name": "top1", "reading": "top-1 accuracy on the test split",
                            "quote": "more accurate than the baseline on CIFAR"}]},
      {"quote": "If the step size is below one half, the error decays", "statement": "The error decays for small steps.",
       "claim_type": "theory", "scope": ["Theorem 1"], "required_evidence": "a proof; exact cases can only refute it",
       "assumptions": [{"quote": "the step size is below one half", "what": "step size < 1/2"}]}]


def test_main_claims_are_extracted_before_any_plan_and_kept_through_every_round():
    """Oct-05 review: the planner both listed the claims and chose the tests, so a planner's criterion could become a
    claim, fragments ('the truncation bias is smaller than that of') stood for claims, and a follow-up plan's list
    replaced the first one's. Claims are now extracted from the paper alone, with their words, scope, assumptions and
    required evidence; a plan tests them by id and can neither drop, requote nor retype one."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _claims_project(td)
        owed = {o["id"] for o in tasks._plan(tasks._Ctx(cfg, pid))[1]}
        assert "claims" in owed and "lens:overclaim" in owed                 # extracted beside the lenses, from the paper
        dup = {**_K[1], "quote": "more accurate than the baseline on CIFAR and SVHN"}
        x0 = tasks._Ctx(cfg, pid)
        assert "same claim" in _refused(lambda: tasks._seal_claims(x0, "claims", {"claims": _K + [dup]}, final=False))
        assert "claim_type" in _refused(lambda: tasks._seal_claims(x0, "claims", {"claims": [{**_K[0], "claim_type": "big"}]},
                                                                     final=False))
        assert "more accurate" in _refused(lambda: tasks._seal_claims(x0, "claims", {"claims": [{**_K[1], "interpretations": [
            {"name": "a", "reading": "an ambiguous reading", "quote": "more accurate"}]}]}, final=False))   # not unique
        _seal(cfg, pid, "claims", {"claims": _K}, td)
        ks = tasks._sealed(td / pid, "claims")["claims"]
        assert [k["id"] for k in ks] == ["K1", "K2", "K3"] and ks[1]["scope"] == ["Ours", "Base", "CIFAR", "SVHN"]
        assert ks[2]["assumptions"][0]["page"] == 1 and ks[1]["interpretations"][0]["name"] == "top1"
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and "K2" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        cert = {"id": "A", "kind": "CERTIFICATE", "claim_quote": "If the step size is below one half, the error decays",
                "statement_quote": "If the step size is below one half, the error decays", "role": "target",
                "covers": ["Theorem 1"]}
        x = tasks._Ctx(cfg, pid)
        part = {"checks": [cert], "central_claims": [{"id": "K3", "checks": ["A"]}]}
        err = _refused(lambda: tasks._seal_plan(x, "plan", part, final=False))
        assert "K1" in err and "K2" in err                                    # every extracted claim needs an entry
        forged = {"checks": [cert], "central_claims": [{"id": "K9", "checks": ["A"]}]}
        assert "not extracted" in _refused(lambda: tasks._seal_plan(x, "plan", forged, final=False))
        retyped = {"checks": [cert], "central_claims": [
            {"id": "K3", "checks": ["A"], "claim_type": "performance", "quote": "We report the mean over 5 random seeds"},
            {"id": "K1", "why_unchecked": "no network code", "blocker": "other"},
            {"id": "K2", "why_unchecked": "no data", "blocker": "other"}]}
        rec = tasks._seal_plan(x, "plan", retyped, final=False)               # quote and type come from the extraction
        k3 = next(c for c in rec["central_claims"] if c["id"] == "K3")
        assert k3["claim_type"] == "theory" and k3["quote"].startswith("If the step") and k3["checks"] == ["C1"]
        assert k3["statement"] == "The error decays for small steps." and k3["assumptions"]
        # A malformed plan (sealed empty on its last attempt) still leaves every extracted claim in the ledger.
        state.write_json(td / pid / "sealed" / "plan.json", tasks._EMPTY["plan"])
        seals = state.read_json(td / pid / "seals.json")
        seals["plan"] = state.sha256((td / pid / "sealed" / "plan.json").read_bytes())
        state.write_json(td / pid / "seals.json", seals)
        led = report.ledger(tasks._Ctx(cfg, pid))
        assert [c["id"] for c in led["central_claims"]] == ["K1", "K2", "K3"]
        assert all(c["claim_status"] == "NOT_CHECKED" and c["why_unchecked"] for c in led["central_claims"])
        assert len(led["completion"]["claims"]) == 3                         # every claim has a completion entry


def test_a_long_check_yields_its_turn_to_one_that_waited():
    """Sep-29 GRACE: the first-polled check took every freed slot (and the GPU) for all of its 100 seeds while the
    integration test waited behind it. A check that just ran waits behind every check that waited meanwhile."""
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects = Path(t)
        for c in ("A", "B", "C"):
            state.write_json(Path(t) / "p" / "checks" / c / "exec.json", {})
        assert execute.wait_turn(cfg, "p/A", True) == (0, 0)
        assert execute.wait_turn(cfg, "p/B", True) == (0, 1)                # A waited longer
        time.sleep(0.01)
        assert execute.wait_turn(cfg, "p/C", False) == (0, 2)
        execute.leave_turn(cfg, "p/A")                                       # A started its run
        assert execute.wait_turn(cfg, "p/B", True) == (0, 0)
        time.sleep(0.01)
        assert execute.wait_turn(cfg, "p/A", True) == (1, 1)                 # its next run waits behind B and C
        state.write_json(Path(t) / "p" / "checks" / "B" / "outcome.json", {})
        assert execute.wait_turn(cfg, "p/A", True) == (1, 0)                 # an ended check leaves no ghost in the line


def test_the_follow_up_round_does_not_wait_on_a_long_run():
    """Sep-29 GRACE: the follow-up plan (and so the layer-replacement test it would have planned) waited for a 100-seed
    training check of another claim. Once only long executions remain, the follow-up is planned beside them; a
    claim whose check still runs is not 'undecided' and is listed as still running."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        a = {"id": "A", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
             "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        b = {**a, "id": "B", "claim_quote": "We use generation of samples", "covers": ["samples"]}
        _seal(cfg, pid, "plan", {"checks": [a, b], "central_claims": [
            {"quote": a["claim_quote"], "checks": ["A"], "claim_type": "theory", "scope": ["the loss"]},
            {"quote": b["claim_quote"], "checks": ["B"], "claim_type": "theory", "scope": ["samples"]}]}, td)
        state.write_json(td / pid / "checks" / "C1" / "outcome.json", {"status": "NOT_CHECKABLE", "reason": "x"})
        state.write_json(td / pid / "checks" / "C2" / "check.json", {"id": "C2", "kind": "CERTIFICATE", "runs": 100})
        state.write_json(td / pid / "checks" / "C2" / "exec.json", {"token": "t"})          # approved and started
        real = execute.poll, execute.remaining_s
        try:
            execute.poll = lambda cfg, pid, cid: cid == "C2"                 # C2 executes
            execute.remaining_s = lambda cfg, root, cid: 600.0                # a short run: the follow-up waits for it
            phase, owed, running = tasks._plan(tasks._Ctx(cfg, pid))
            assert phase == "verify" and owed == [] and running == ["C2"]
            execute.remaining_s = lambda cfg, root, cid: 6 * 3600.0           # a long one: planned beside it
            phase, owed, running = tasks._plan(tasks._Ctx(cfg, pid))
            assert phase == "plan" and [o["id"] for o in owed] == ["plan:2"] and running == ["C2"]
            text = Path(owed[0]["prompt"]).read_text(encoding="utf-8")
            assert "STILL RUNNING" in text and "- C2 " in text
            undecided = text.split("Still undecided:")[1].split("===")[0]
            assert "generation of samples" not in undecided and "mean over 5" in undecided
        finally:
            execute.poll, execute.remaining_s = real


def _claim_row(st, ctype="performance", exp="RAN_AS_SPECIFIED", checks=(), **kw):
    return {"id": "K1", "quote": "q", "claim_type": ctype, "claim_status": st, "checks": list(checks),
            "completion": {"experiment": exp, "changes": [], "scope_not_run": [], "not_run": [], **kw.pop("comp", {})}, **kw}


def test_every_main_claim_gets_one_decision_with_a_specific_reason():
    """Oct-05 review: a reviewer needs one decision per main claim — Verified only when the requested test ran as
    specified and supports it within its scope; otherwise Not verified with the specific reason. A theorem is never
    verified by finite cases, a failed proof step is not a false theorem, missing evidence is not falsity."""
    from harness.reviewer import decision
    chk = lambda **kw: {"id": "C1", "kind": "RECONSTRUCTION", "role": "target", "status": "RELATION_HOLDS",
                        "values": [1.0], "counts": {}, **kw}
    d = lambda cc, cs=(): decision(cc, {c["id"]: c for c in cs})
    assert d(_claim_row("SUPPORT_FOUND", checks=["C1"]), [chk()])["decision"] == "VERIFIED"
    cases = [(_claim_row("PARTIAL_EVIDENCE", exp="RAN_PARTIAL", comp={"scope_not_run": ["SVHN"]}), "incomplete_coverage"),
             (_claim_row("NO_VIOLATION_FOUND", "theory", "RAN_AS_SPECIFIED"), "finite_cases_only"),
             (_claim_row("SUPPORT_FOUND", "theory"), "finite_cases_only"),
             (_claim_row("FAILURE_FOUND", "theory"), "false_as_printed"), (_claim_row("FAILURE_FOUND"), "contradicted"),
             (_claim_row("PROOF_GAP_FOUND", "theory"), "proof_step_invalid"), (_claim_row("PREMISE_NOT_MET", "theory"), "premise_impossible"),
             (_claim_row("READING_CHANGED", exp="RAN_WITH_CHANGES"), "changed_protocol"),
             (_claim_row("READINGS_DISAGREE"), "interpretation_uncertain"), (_claim_row("CHECKS_DISAGREE"), "checks_disagree"),
             (_claim_row("NOT_CHECKED", exp="NOT_RUN", why_unchecked="no code"), "not_checked"),
             (_claim_row("NOT_CHECKED", exp="NOT_RUN", comp={"not_run": [{"item": "IHDP", "blocker": "data", "basis": "harness",
                                                                  "why": "404"}]}), "blocked"),
             (_claim_row("PENDING"), "pending")]
    for cc, want in cases:
        got = d(cc)
        assert got["decision"] == "NOT_VERIFIED" and got["reason"] == want, (cc["claim_status"], got)
        assert got["reason_text"] and isinstance(got["because"], list)
    undecided = d(_claim_row("NOTHING_DECIDED", checks=["C1"]), [chk(status="INCONCLUSIVE")])
    assert undecided["reason"] == "undecided" and "not decided" in undecided["because"][0]
    cert = {"id": "C1", "kind": "CERTIFICATE", "role": "target", "status": "NO_VIOLATION_FOUND", "values": [0] * 60,
            "literal": {"undefined": 60}, "counts": {}}
    assert d(_claim_row("READING_CHANGED", "theory", checks=["C1"]), [cert])["reason"] == "notation_defect"
    depends = chk(status="RELATION_VIOLATED", audit={"verdict": "DEPENDS"})
    assert d(_claim_row("READING_CHANGED", checks=["C1"]), [depends])["reason"] == "interpretation_uncertain"
    assert d(_claim_row("NOT_CHECKED", exp="NOT_RUN", why_unchecked="x"))["because"][-1].startswith("why no test (planner")


def test_the_reviewer_page_is_built_from_the_record_and_publishes_only_checked_prose():
    """Oct-05 review: the reviewer report must give, per claim, the decision before its limits, what ran on what, the
    numbers with their uncertainty and denominators, what was not tested, and evidence references — and model prose
    that invents a number or a status word must not reach the page."""
    from harness import reviewer
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        st = {"interaction": {"status": "RELATION_HOLDS", "margin": 0.0973, "band": 0.0319, "n": 100, "n_independent": 100},
              "tree": {"status": "RELATION_HOLDS", "margin": 0.0598, "band": 0.0198, "n": 100, "n_independent": 100},
              "neural": {"status": "UNDEFINED", "reason": "no treated units"}}
        c1 = {"id": "C1", "kind": "RECONSTRUCTION", "role": "target", "status": "RELATION_HOLDS", "claim": "q", "covers": ["GRACE", "TARNet"],
              "target": {"relation": "err_base - err_grace > 0"}, "stages": st, "values": [0.1] * 200,
              "outputs": {"interaction": {"err_base": 1.0891, "err_grace": 0.9918}, "tree": {"err_base": 0.7826, "err_grace": 0.7228}},
              "execution": {"runs_planned": 100, "runs_ended": 100}, "units": ["interaction", "tree", "neural"],
              "fidelity": [{"aspect": "metric", "paper": "RMSE of the CATE", "code": {"file": "eval.py", "quote": "x"},
                            "used": "RMSE of the CATE", "agrees": False, "explained": "the code averages over seeds first"}],
              "deviations": [], "script_sha256": "8b7df4c174ee0000", "records": "execution.jsonl", "commit": "58d2f643aa",
              "data_identity": {}, "runs_source": "paper", "reason": "", "basis": "fresh_run"}
        c1["counts"] = report.counts(c1)
        claims = [{"id": "K1", "statement": "GRACE has lower error than the baselines.", "quote": "GRACE consistently surpasses",
                   "page": 6, "claim_type": "performance", "checks": ["C1"], "claim_status": "PARTIAL_EVIDENCE",
                   "completion": {"experiment": "RAN_PARTIAL", "changes": [], "scope_not_run": ["BART", "IHDP"],
                                  "not_run": [{"item": "BART", "blocker": "cap", "basis": "planner", "why": "budget"}]}},
                  {"id": "K2", "statement": "The layer has a sparse form.", "quote": "Theorem 5.1", "page": 5,
                   "claim_type": "theory", "checks": [], "claim_status": "NOT_CHECKED", "why_unchecked": "no exact form",
                   "completion": {"experiment": "NOT_RUN", "changes": [], "scope_not_run": [], "not_run": []}}]
        by = {"C1": c1}
        for cc in claims:
            cc["decision"] = reviewer.decision(cc, by)
        led = {"paper": {"title": "GRACE", "sha256": "ab" * 32, "arxiv_id": "", "source": "OpenReview tSZaHvpxCd"},
               "source": {"url": "https://github.com/x/GRACE", "commit": "58d2f643aa11"}, "checks": [c1],
               "central_claims": claims, "workflow": {"finished": 1, "checks_planned": 1}, "concerns": []}
        x = types.SimpleNamespace(root=root, paper=Paper(["GRACE consistently surpasses the baselines. Theorem 5.1 holds."]))
        rep = {"overview": "The paper proposes GRACE, a tree layer trained with gradients.",
               "claims": [{"id": "K1", "explanation": "GRACE had lower error in two settings. It improved error by 37.2% there."},
                          {"id": "K2", "explanation": "The theorem was not tested."}],
               "terms": [{"term": "RMSE", "definition": "root mean squared error, lower is better"}],
               "open_questions": ["Which seeds were used for the baselines? This is verified in Table 2."]}
        page = reviewer.render(x, led, rep)
        assert "| K1. GRACE has lower error than the baselines. | **Not verified** | incomplete coverage |" in page
        assert "**Unresolved claims:** K1, K2." in page
        assert page.index("**Decision: Not verified.**") < page.index("**Not tested:**")       # the finding before its limits
        assert "0.9918" in page and "0.0973 ± 0.0319" in page and "100 (100)" in page            # magnitudes and uncertainty
        assert "3 settings declared = 2 with a result + 1 undefined + 0 missing" in page          # denominators reconcile
        assert "Not tested:** BART, IHDP" in page and "budget" in page
        assert "37.2%" not in page and "withheld" in page                                        # an invented number
        assert "This is verified in Table 2" not in page                                         # an unearned status word
        assert "GRACE, a tree layer trained with gradients" in page and "root mean squared error" in page
        assert "the code averages over seeds first" in page                                      # paper vs code, explained
        assert "Evidence: C1 · script `8b7df4c174ee`" in page
        assert not re.search(r"^#+ .*\bC\d+\b", page, re.M)                                       # no check codes as headings
        assert len(page.split()) < 1100                                                          # about two pages


def test_each_reading_shows_its_numbers_and_an_audit_says_what_a_failure_depends_on():
    """Oct-06 draft review of label ranking K7: the readings check printed a table whose header had five columns and
    its separator four, with no number in it, and "reading paper_raw_sum: relation holds" hid that its correlations
    were negative (-0.314 vs -0.328) where the code's were positive (0.726 vs 0.595). Every reading shows its outputs and
    margin per setting. K1/K2: an independent audit found each counterexample DEPENDS on how a symbol is read; the page
    said only "interpretation uncertain" and never which words, nor that the claim holds under the other reading."""
    from harness import reviewer
    rd = lambda m, st: {"status": st, "stages": {"RB2": {"status": st, "margin": m, "band": 0.0, "n": 1}}}
    c6 = {"id": "C6", "kind": "RELEASED_DATA", "role": "target", "status": "READINGS_DIFFER", "values": [0.013152, 0.130513, -0.179659],
          "target": {"relation": "tau_fs_min > tau_mp_max", "names": ["tau_fs_min", "tau_mp_max"]},
          "stages": {"RB2": {"status": "READINGS_DIFFER", "readings": {"paper": "RELATION_HOLDS", "code": "RELATION_HOLDS",
                                                                       "pooled": "RELATION_VIOLATED"}}},
          "readings": {"paper": rd(0.013152, "RELATION_HOLDS"), "code": rd(0.130513, "RELATION_HOLDS"),
                       "pooled": rd(-0.179659, "RELATION_VIOLATED")},
          "outputs": {"RB2 [paper]": {"tau_fs_min": -0.314388, "tau_mp_max": -0.32754, "tau_lb": -0.326241},
                      "RB2 [code]": {"tau_fs_min": 0.725983, "tau_mp_max": 0.59547, "tau_lb": 0.771277},
                      "RB2 [pooled]": {"tau_fs_min": 0.286479, "tau_mp_max": 0.466137, "tau_lb": 0.35461}},
          "counts": {"units_declared": 1, "launches": 1}}
    rows = reviewer.result_rows(c6)
    table = [r for r in rows if r.startswith("|")]
    assert table and len({r.count("|") for r in table}) == 1, table                 # header, separator, rows: one width
    assert table[0].split("|")[2].strip() == "tau_fs_min", table[0]                  # the compared outputs come first
    text = "\n".join(rows)
    for v in ("-0.31439", "-0.32754", "0.72598", "0.59547", "0.013152", "-0.17966"):
        assert v in text, (v, text)
    assert "RB2 [paper]" in text and "relation violated" in text
    with tempfile.TemporaryDirectory() as t:                  # what "margin" compares, and whose criterion it is, once
        cc = {"id": "K7", "statement": "s", "quote": "correlates strongly", "page": 9, "claim_type": "performance",
              "checks": ["C6"], "claim_status": "READINGS_DISAGREE",
              "completion": {"experiment": "RAN", "changes": [], "scope_not_run": [], "not_run": []}}
        many = {**c6, "criterion": "supplied", "stages": {f"s{i}": {"status": "RELATION_HOLDS", "margin": 0.1 * i, "n": 3}
                                                          for i in range(9)}, "readings": {}, "outputs": {}}
        cc["decision"] = reviewer.decision(cc, {"C6": many})
        led = {"paper": {"title": "LR", "sha256": "ab" * 32, "arxiv_id": "", "source": "x"}, "source": {},
               "checks": [many], "central_claims": [cc], "workflow": {"finished": 1, "checks_planned": 1}, "concerns": []}
        page = reviewer.render(types.SimpleNamespace(root=Path(t), paper=Paper(["It correlates strongly."])), led, None)
        assert page.count("compared: `tau_fs_min > tau_mp_max`") == 1 and "chosen by REFEREE" in page, page
        assert page.count("settings: 9 decided") == 1, page                          # the summary is not printed twice
    grouped = {**c6, "readings": {r: {"status": "RELATION_HOLDS", "stages": {f"s{i}": {"status": "RELATION_HOLDS", "margin": 1,
                                                                                      "n": 1} for i in range(5)}}
                                  for r in ("paper", "code")}, "outputs": {}}
    text = "\n".join(reviewer.result_rows(grouped, limit=6))
    assert "settings under reading code: 5 decided" in text and "[code]" not in text, text   # readings named in words
    one = "\n".join(reviewer.result_rows({"kind": "CERTIFICATE", "values": [0, 0], "instances": 1, "readings_per_instance": 2,
                                          "admissible_instances": 1}))
    assert "- 1 case (" in one, one
    bare = {**c6, "readings": {}, "outputs": {}, "stages": {"a": {"status": "RELATION_HOLDS", "margin": 0.5, "band": 0.1, "n": 3}}}
    t2 = [r for r in reviewer.result_rows(bare) if r.startswith("|")]
    assert len({r.count("|") for r in t2}) == 1 and "0.5 ± 0.1" in t2[-1], t2         # no outputs: still one width
    with tempfile.TemporaryDirectory() as t:
        cert = {"id": "C1", "kind": "CERTIFICATE", "role": "target", "status": "COUNTEREXAMPLE_FOUND", "values": [1.0] * 8,
                "admissible": 8, "literal": {"fails": 8}, "counts": {}, "step": "Proof of Theorem 2", "deviations": [],
                "audit": {"verdict": "DEPENDS", "depends_on": [
                    {"printed": "where P denotes the projection onto B", "page": 5, "tested_as": "the projection event",
                     "alternative": "the symbol denotes the top-k prefix event; under it every tested case holds",
                     "why": "the definition is stated only once"}]}}
        cc = {"id": "K1", "statement": "Full calibration implies top-k calibration.", "quote": "full calibration implies",
              "page": 6, "claim_type": "theory", "checks": ["C1"], "claim_status": "READING_CHANGED",
              "completion": {"experiment": "RAN", "changes": [], "scope_not_run": [], "not_run": []}}
        cc["decision"] = reviewer.decision(cc, {"C1": cert})
        led = {"paper": {"title": "LR", "sha256": "ab" * 32, "arxiv_id": "", "source": "x"}, "source": {},
               "checks": [cert], "central_claims": [cc], "workflow": {"finished": 1, "checks_planned": 1}, "concerns": []}
        x = types.SimpleNamespace(root=Path(t), paper=Paper(["Thus full calibration implies top-k calibration, "
                                                             "where P denotes the projection onto B."]))
        page = reviewer.render(x, led, None)
        assert cc["decision"]["reason"] == "interpretation_uncertain"
        assert "independent audit" in page.lower() and '"where P denotes the projection onto B" (p5)' in page, page
        assert "top-k prefix event" in page                                              # what the other reading gives
        assert "of one proof step" in page and "8 cases" in page
        assert "proof step as printed fails" in page and "a counterexample was found" not in page   # a step, not the theorem


def test_the_reviewer_page_states_each_test_once_and_stays_near_two_pages():
    """Oct-06 draft review: the GRACE page ran to 4,900 words because a test's data, model, metric, baselines and changes
    were repeated under every claim citing it (C1 under K1 and K8, C5 under K1 and K9), each unrun scope item had its own
    "not run — no target check covering it ran to completion" line, and data identity was printed as raw JSON. A
    blocked check's six declared settings (0 + 0 + 6 missing) also read "DOES NOT RECONCILE"."""
    from harness import reviewer
    blocked = {"id": "C1", "kind": "RECONSTRUCTION", "role": "target", "status": "BLOCKED", "units": [f"u{i}" for i in range(6)],
               "reason": "RESOURCE BLOCKER: the 100 runs need about 25.2 h more", "values": [], "stages": {},
               "execution": {"runs_planned": 100, "runs_ended": 1}}
    n = report.counts(blocked)
    assert n["units_not_completed"] == 6 and n["reconciles"], n                   # 6 = 0 + 0 + 6: nothing unaccounted
    partly = {**blocked, "status": "PARTIAL", "values": [1.0], "stages": {"u0": {"status": "RELATION_HOLDS", "n": 3}}}
    assert not report.counts(partly)["reconciles"]                                # five units vanished beside a result
    with tempfile.TemporaryDirectory() as t:
        c1 = {**blocked, "counts": report.counts(blocked), "covers": ["GRACE", "TARNet"], "runs_source": "paper",
              "data_identity": {"toy k=5": {"observed": {"n_train": 5000, "n_test": 10000, "treated_fraction": 0.5020666666666667,
                                                         "x_min": 2.8245575196539363e-07}, "matches": True}},
              "fidelity": [{"aspect": "model", "used": "The authors' GRACE class with config.yaml values. Every other value "
                                                       "is taken from config.yaml as shipped."}],
              "deviations": [{"changes_claim": True, "used": "No tuning is run. GRACE uses the released configuration "
                                                             "(100 trees, depth 4) for every dataset and every size."}]}
        mk = lambda k, scope: {"id": k, "statement": f"claim {k}", "quote": "GRACE outperformed the baselines", "page": 6,
                               "claim_type": "performance", "checks": ["C1"], "claim_status": "NOT_CHECKED",
                               "completion": {"experiment": "NOT_RUN", "changes": [], "scope_not_run": scope, "not_run": [
                                   {"item": s, "blocker": "not run", "basis": "harness",
                                    "why": "no target check covering it ran to completion"} for s in scope] + [
                                   {"item": "BART", "blocker": "cap", "basis": "planner", "why": "no slot left"}]}}
        claims = [mk("K1", ["GRACE", "TARNet", "BART"]), mk("K8", ["GRACE", "TARNet"])]
        for cc in claims:
            cc["decision"] = reviewer.decision(cc, {"C1": c1})
        led = {"paper": {"title": "GRACE", "sha256": "ab" * 32, "arxiv_id": "", "source": "x"}, "source": {},
               "checks": [c1], "central_claims": claims, "workflow": {"finished": 1, "checks_planned": 1}, "concerns": []}
        page = reviewer.render(types.SimpleNamespace(root=Path(t), paper=Paper(["GRACE outperformed the baselines."])), led, None)
        k8 = page.split("### K8.")[1]
        assert page.count("config.yaml values") == 1 and "same test as under K1" in k8, page
        assert '{"n_train"' not in page and "n_train 5000" in page and "treated_fraction 0.50207" in page, page
        assert "no target check covering it ran to completion" not in page and "BART: cap" in page, page
        assert "1 of 100 planned runs" in page and "DOES NOT RECONCILE" not in page, page
        assert "No tuning is run." in page and "(100 trees, depth 4)" not in page        # a whole first sentence, no cut
        assert claims[0]["decision"]["reason"] == "blocked"                             # its one test was blocked
        main = page.split("## Test details")[0]
        assert "config.yaml values" not in main and "on toy k=5; 1 of 100 planned runs" in main, main   # the gist up top
        why = "The claim needs every baseline on all three DGPs at four sizes, which no slot is left for."
        k7 = {"id": "K7", "statement": "claim K7", "quote": "GRACE outperformed the baselines", "page": 6,
              "claim_type": "performance", "checks": [], "claim_status": "NOT_CHECKED", "why_unchecked": why,
              "completion": {"experiment": "NOT_RUN", "changes": [], "scope_not_run": ["GRACE"], "not_run": [
                  {"item": "(the whole claim)", "blocker": "cap", "basis": "planner", "why": why[:90]}]}}
        k7["decision"] = reviewer.decision(k7, {})
        page = reviewer.render(types.SimpleNamespace(root=Path(t), paper=Paper(["GRACE outperformed the baselines."])),
                               {**led, "central_claims": [k7]}, None)
        assert page.count("which no slot is left") == 1, page                            # the planner's reason once


def test_a_decision_never_says_a_changed_reading_held_when_it_failed_or_that_the_printed_text_was_not_tested():
    """Oct-06 draft review of changepoint: K6 read "Under a corrected reading it held on the tested cases" while its
    check found cases that violate the corrected reading; K2/K3 read "The test ran only under a changed protocol" while
    the same certificate evaluated the printed statement too and all 324 cases held as printed."""
    from harness.reviewer import decision
    cert = lambda st, lit: {"id": "C1", "kind": "CERTIFICATE", "role": "target", "status": st, "values": [0] * 24,
                            "literal": lit, "counts": {}}
    d = lambda c: decision(_claim_row("READING_CHANGED", "theory", "RAN", checks=["C1"]), {"C1": c})
    undefined_violated = d(cert("VIOLATION_UNDER_CHANGED_READING", {"undefined": 12}))
    assert undefined_violated["reason"] == "notation_defect"
    assert "held" not in undefined_violated["reason_text"] and "violate" in undefined_violated["reason_text"]
    undefined_held = d(cert("NO_VIOLATION_FOUND", {"undefined": 16}))
    assert undefined_held["reason"] == "notation_defect" and "held" in undefined_held["reason_text"]
    printed_held = d(cert("VIOLATION_UNDER_CHANGED_READING", {"holds": 324}))
    assert printed_held["reason"] == "finite_cases_only", printed_held                # the printed text was tested
    assert "changed reading" in printed_held["reason_text"] and "only under" not in printed_held["reason_text"]
    assert d(cert("VIOLATION_UNDER_CHANGED_READING", {}))["reason"] == "changed_protocol"   # nothing as printed
    assert d(cert("VIOLATION_UNDER_CHANGED_READING", {"holds": 3, "fails": 1}))["reason"] != "finite_cases_only"


def test_another_reproduction_record_is_compared_only_after_the_decisions_are_sealed():
    """Oct-05 review: HF verdicts are another reproduction record, not ground truth. One is registered only after
    REFEREE's own decisions are sealed, its findings are quoted verbatim from the registered file with whether the two
    tests are comparable, and no decision moves."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _claims_project(td)
        ref = td / "hf.md"
        ref.write_text("The logbook reports RMSE 0.0471 for GRACE versus 0.0802 for the best baseline on toy_1, judged toy.",
                       encoding="utf-8")
        assert "error" in tasks.register_reference(cfg, pid, str(ref), "HF logbook")
        _seal(cfg, pid, "claims", {"claims": _K}, td)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        _seal(cfg, pid, "plan", {"checks": [], "central_claims": [
            {"id": k, "why_unchecked": "no test fits this fixture", "blocker": "other"} for k in ("K1", "K2", "K3")]}, td)
        cfg.max_followup_checks = 0
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "report" and "Decisions on the main claims" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        _seal(cfg, pid, "report", {"overview": "A paper.", "claims": [{"id": k, "explanation": "Not tested."}
                                                                     for k in ("K1", "K2", "K3")]}, td)
        assert tasks._plan(tasks._Ctx(cfg, pid))[0] == "done"
        before = report.ledger(tasks._Ctx(cfg, pid))["central_claims"]
        assert tasks.register_reference(cfg, pid, str(ref), "HF logbook")["registered"]
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "compare" and [o["id"] for o in owed] == ["compare"]
        entry = {"id": "K2", "reference_finding": "The record reports lower error for GRACE on toy_1.",
                 "comparable": "partly", "why": "the record ran two datasets with five seeds; REFEREE ran none",
                 "agreement": "not_comparable", "source": "HF logbook"}
        x = tasks._Ctx(cfg, pid)
        assert "verbatim" in _refused(lambda: tasks._seal_compare(x, "compare", {"claims": [
            {**entry, "quotes": ["the record says GRACE is verified everywhere"]}]}, final=False))
        _seal(cfg, pid, "compare", {"claims": [{**entry, "quotes": ["RMSE 0.0471 for GRACE versus 0.0802"]}]}, td)
        assert tasks._plan(tasks._Ctx(cfg, pid))[0] == "done"
        page = (td / pid / "reviewer.md").read_text(encoding="utf-8")
        assert "Other reproduction record (HF logbook)" in page and "Comparable: partly" in page
        after = report.ledger(tasks._Ctx(cfg, pid))["central_claims"]
        assert [c["decision"] for c in after] == [c["decision"] for c in before]                 # nothing moved
        assert "In its words: \"RMSE 0.0471 for GRACE versus 0.0802\"" in page                    # its own words, shown
        # Oct-06 label ranking: "The record reports all five summaries verified ..." was withheld as REFEREE status
        # language. A finding attributed to the record may carry the record's own verdict words; REFEREE's comparison
        # note ("why") and an unattributed finding may not.
        from harness import reviewer
        led = report.ledger(tasks._Ctx(cfg, pid))
        cmp = {"claims": [{**entry, "quotes": [], "reference_finding": "The record reports five summaries verified GRACE on toy_1."},
                          {**entry, "id": "K1", "quotes": [], "reference_finding": "GRACE was verified on toy_1."},
                          {**entry, "id": "K3", "quotes": [], "why": "REFEREE reproduced the toy result."}]}
        page = reviewer.render(tasks._Ctx(cfg, pid), led, None, cmp)
        held = state.read_json(td / pid / "reviewer.withheld.json")
        assert "five summaries verified GRACE" in page and not any(h.startswith("K2 reference finding") for h in held), held
        assert any(h.startswith("K1 reference finding") for h in held) and any(h.startswith("K3 reference:") for h in held), held
        bad = td / "hf.pdf"
        bad.write_bytes(b"%PDF-1.4 \xff\xfe\x00binary")
        assert "not UTF-8" in tasks.register_reference(cfg, pid, str(bad), "HF")["error"]        # never stops the review


# --- 2026-10-05 code review of the round's changes ------------------------------------------------------------------
def test_a_failure_scoped_to_one_reading_is_still_audited_and_a_paper_tag_scopes_nothing():
    """Review #1/#4: needs_audit read the plan check (`readings`, no `reading_defs`), so a reading-scoped deviation looked
    claim-changing there and no audit was owed, while the ledger counted the failure: FAILURE_FOUND without its audit.
    And a deviation tagged with the printed reading itself, or flagged by the verifier, cancelled itself."""
    defs = [{"name": "paper", "source": "paper"}, {"name": "code", "source": "analyze.py"}]
    dev = {"changes_claim": True, "used": "softmax", "reading": "code"}
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "RELATION_VIOLATED"})
        state.write_json(root / "checks" / "C1" / "check.json", {"deviations": [dev], "readings": defs})
        plan_check = {"id": "C1", "kind": "RELEASED_DATA", "role": "target", "readings": defs}
        assert report.needs_audit(root, plan_check)                              # the failure is about the printed claim
    assert report._scoped(dev, {"reading_defs": defs}) and report._scoped(dev, {"readings": defs})
    assert not report._scoped({**dev, "reading": "paper"}, {"reading_defs": defs})   # the printed one: nothing scoped
    assert not report._scoped({**dev, "changes_claim_by": "verifier"}, {"reading_defs": defs})   # the second key stands
    assert not report._scoped({**dev, "reading": "other"}, {"reading_defs": defs})   # an undeclared reading


def test_an_extracted_claims_whole_scope_reaches_the_plan_and_the_ledger():
    """Review #2 (live run): the plan seal cut the extracted scope to 16 items (GRACE K1 lost 8 baselines and datasets,
    label ranking K6 lost 2 ECE variants), so a claim could read as run as specified on part of its scope."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _claims_project(td)
        wide = {**_K[1], "scope": ["Ours", "Base"] + [f"dataset {i}" for i in range(28)]}
        x0 = tasks._Ctx(cfg, pid)
        rec = tasks._seal_claims(x0, "claims", {"claims": [wide]}, final=False)
        assert len(rec["claims"][0]["scope"]) == 30 and "_raw" not in rec["claims"][0]
        assert "at most" in _refused(lambda: tasks._seal_claims(x0, "claims", {"claims": [
            {**wide, "scope": [f"d{i}" for i in range(45)]}]}, final=False))                    # refused, never cut
        cut = tasks._seal_claims(x0, "claims", {"claims": [{**wide, "scope": [f"d{i}" for i in range(45)]}]}, final=True)
        assert len(cut["claims"][0]["scope_cut"]) == 5                                           # recorded on the last attempt
        _seal(cfg, pid, "claims", {"claims": [wide]}, td)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        x = tasks._Ctx(cfg, pid)
        sealed = tasks._seal_plan(x, "plan", {"checks": [], "central_claims": [
            {"id": "K1", "why_unchecked": "no test fits", "blocker": "other"}]}, final=False)
        assert len(sealed["central_claims"][0]["scope"]) == 30
        old = {**sealed, "central_claims": [{**sealed["central_claims"][0], "scope": wide["scope"][:16]}]}   # a cut plan
        m = report.merged(old, None, (), [{**rec["claims"][0], "scope_cut": ["d40"]}])
        assert len(m["central_claims"][0]["scope"]) == 30                                        # the extraction restores it
        assert any(o["item"] == "d40" and o["blocker"] == "cap" for o in m["central_claims"][0]["omitted"])


def test_values_that_differ_are_never_one_measurement_and_round_off_never_fails_a_number():
    """Review #3/#5: `_repeats` trusted the independence record, which ignores time-like outputs, so a timing comparison
    whose margins differ (2.0, 0.7, 1.9) read as one deterministic measurement with a band of 0; and a printed-number check
    whose seeds differed only by round-off got a prediction interval of ~1e-13 (FAILED_REPRODUCTION)."""
    from harness.reconcile import _repeats
    rel = "time_base - time_ours > 0"
    outs = [{"time_base": 3.0, "time_ours": 1.0}, {"time_base": 2.0, "time_ours": 1.3}, {"time_base": 2.9, "time_ours": 1.0}]
    margins = [o["time_base"] - o["time_ours"] for o in outs]
    assert not _repeats(margins) and _repeats([1.0, 1.0 + 1e-15])
    det = _rel(rel, _rows(outs), margins, det=True)
    assert det["status"] == "INCONCLUSIVE" and "runs differ" in det["stages"]["a"]["reason"], det
    st = {"staged": [["a", m] for m in margins], "detail": _rows(outs), "done_seeds": [0, 1, 2]}
    assert not execute.varied_nothing({"kind": "RECONSTRUCTION", "stochastic": True}, st)   # a timing output varied
    vals = [61.0 + k * 1e-13 for k in range(3)]
    p = reconcile("RECONSTRUCTION", "61.4", vals, "", {}, True, True, "")
    assert p["status"] != "FAILED_REPRODUCTION" and p["std"] == 0.0, p


def test_a_place_in_the_line_lapses_when_its_review_stops_polling():
    """Review #6: a review whose workflow stopped kept its queue entries forever; two stale CPU waiters left every later
    check on the host with no free slot."""
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects = Path(t)
        for c in ("A", "B"):
            state.write_json(Path(t) / "p" / "checks" / c / "exec.json", {})
        execute.wait_turn(cfg, "p/A", False)
        q = state.read_json(Path(t) / ".queue.json")
        q["p/A"]["seen"] = q["p/A"]["t"] = time.time() - execute.QUEUE_STALE_S - 5      # nobody polled it for an hour
        state.write_json(Path(t) / ".queue.json", q)
        assert execute.wait_turn(cfg, "p/B", False) == (0, 0)


def test_a_gpu_out_of_memory_in_any_wording_is_retried_alone_and_a_lost_seed_is_resumed():
    """Oct-06 GRACE C4: seed 0 died with PyTorch's allocator message ("memory allocation failed with OOM on device 0 ...
    free: 0") while seeds 1 and 2 later ran the same setting; the harness knew only "CUDA out of memory", so the seed was
    a script failure: never retried alone, the check PARTIAL, and its zero-variance stages never extended to six. Any
    wording of a GPU memory failure takes the retry-once-alone path; a PARTIAL check whose every failed seed failed so is
    resumed by `reopen`: the same approved script, the completed seeds reused, the failed seeds run again."""
    err = ("[W1005 18:29:41] CUDACachingAllocator.cpp:3934] memory allocation failed with OOM on device 0 while trying "
           "to allocate 960495616 bytes (free: 0, total: 8585216000).\nFAILED (out of GPU/host memory)")
    st = {}
    assert execute.resource_action({"mode": "evidence", "returncode": 1, "stderr": err}, st, "0", 3600) == ("retry", "")
    act, why = execute.resource_action({"mode": "evidence", "returncode": 1, "stderr": err}, st, "0", 3600)
    assert act == "blocker" and why[0] == "vram", why
    assert execute.gpu_oom("RuntimeError: CUDA error: out of memory") and not execute.gpu_oom("ValueError: bad shape")
    with tempfile.TemporaryDirectory() as t:
        cfg, pid, root = _ready(Path(t))
        cdir = root / "checks" / "C1"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "script.py").write_text("print(1)\n", encoding="utf-8")
        state.write_json(cdir / "check.json", {"id": "C1", "kind": "RECONSTRUCTION", "script_sha256": "s", "runs": 3})
        rows = [{"key": "s", "seed": 0, "values": [], "error": "exit 1 after 1361s: " + err.replace("\n", " | ")},
                {"key": "s", "seed": 1, "values": [1.0]}, {"key": "s", "seed": 2, "values": [1.0]}]
        (cdir / "seeds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        state.write_json(cdir / "exec.json", {"seed": 3})
        part = {"status": "PARTIAL", "authorized": True, "values": [1.0, 1.0], "failed_seeds": {"0": rows[0]["error"]}}
        state.write_json(cdir / "outcome.json", part)
        seals = state.read_json(root / "seals.json", {}) or {}
        state.write_json(root / "seals.json", {**seals, "gen:C1.1": "x", "verify:C1.1": "y"})
        res = tasks.reopen(cfg, pid, "C1", "GPU OOM wording (test)")
        assert res.get("same_approved_script") and res.get("seeds_rerun") == ["0"], res
        assert [json.loads(x)["seed"] for x in (cdir / "seeds.jsonl").read_text(encoding="utf-8").splitlines()] == [1, 2]
        assert (cdir / "seeds.rerun.1.jsonl").exists() and (cdir / "script.py").exists() and not (cdir / "exec.json").exists()
        assert {"gen:C1.1", "verify:C1.1"} <= set(state.read_json(root / "seals.json"))   # the approval stands
        state.write_json(cdir / "outcome.json", {**part, "failed_seeds": {"0": "exit 1: ValueError: bad shape"}})
        assert "error" in tasks.reopen(cfg, pid, "C1", "a script failure is not resumed")


def test_a_waiter_nobody_is_polling_never_blocks_the_line():
    """Oct-06 run: changepoint C7/C8 headed the GPU line while their review's controller sat for 54 minutes on a script
    review (no `tasks` call, so nobody polled them); seven GPU checks of all three reviews waited behind them with the GPU
    idle. A waiter can take its turn only when polled: one not polled for QUEUE_IDLE_S keeps its place but blocks no one,
    and takes its turn back when it is polled again."""
    with tempfile.TemporaryDirectory() as t:
        cfg = state.Config()
        cfg.projects = Path(t)
        for c in ("A", "B", "C"):
            state.write_json(Path(t) / "p" / "checks" / c / "exec.json", {})
        execute.wait_turn(cfg, "p/A", True)
        execute.wait_turn(cfg, "p/B", True)
        q = state.read_json(Path(t) / ".queue.json")
        q["p/A"]["seen"] = time.time() - execute.QUEUE_IDLE_S - 60                     # its review is busy elsewhere
        state.write_json(Path(t) / ".queue.json", q)
        assert execute.wait_turn(cfg, "p/B", True) == (0, 0)                            # B runs instead of idling
        assert execute.wait_turn(cfg, "p/C", False) == (0, 1)                           # C still waits behind B
        assert execute.wait_turn(cfg, "p/A", True) == (0, 0)                            # A polled again: its place stands
        assert execute.wait_turn(cfg, "p/B", True) == (0, 1)


def test_an_offered_follow_up_stays_pending_until_it_is_sealed():
    """Review #7: the early follow-up was recomputed at every call; once the long run had less than 20 minutes left (or
    an audit appeared) the planner's answer was refused as 'not a pending task'."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        state.write_json(td / ".gpu.json", False)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        a = {"id": "A", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
             "statement_quote": "The final loss is -0.52", "role": "target", "covers": ["the loss"]}
        _seal(cfg, pid, "plan", {"checks": [a], "central_claims": [
            {"quote": a["claim_quote"], "checks": ["A"], "claim_type": "theory", "scope": ["the loss"]}]}, td)
        state.write_json(td / pid / "checks" / "C1" / "check.json", {"id": "C1", "kind": "CERTIFICATE", "runs": 100})
        state.write_json(td / pid / "checks" / "C1" / "exec.json", {"token": "t"})
        real = execute.poll, execute.remaining_s
        try:
            execute.poll = lambda cfg, pid, cid: True
            execute.remaining_s = lambda cfg, root, cid: 6 * 3600.0
            assert "plan:2" in {o["id"] for o in tasks._plan(tasks._Ctx(cfg, pid))[1]}
            execute.remaining_s = lambda cfg, root, cid: 60.0                     # the run is nearly done
            assert "plan:2" in {o["id"] for o in tasks._plan(tasks._Ctx(cfg, pid))[1]}   # still pending: never refused
            _seal(cfg, pid, "plan:2", {"checks": [], "central_claims": []}, td)
            assert "plan:2" not in {o["id"] for o in tasks._plan(tasks._Ctx(cfg, pid))[1]}
        finally:
            execute.poll, execute.remaining_s = real


def test_prose_cannot_slip_a_status_word_a_number_or_markup_past_the_checks():
    """Review #8/#9: a quoted status word ("reproduced") was stripped as someone else's words; signed numbers and numbers
    with a unit were not checked; the allowed numbers included every digit in the ledger's strings (sha256s, timestamps,
    model text); inline HTML and entities rendered unchecked."""
    from harness import reviewer
    p = Paper(["The method reaches 61.4 accuracy on CIFAR with 5 random seeds and a batch of 128."])
    led = {"checks": [{"id": "C1", "status": "INCONCLUSIVE"}], "concerns": [], "script_sha256": "9e300aa1"}
    allowed = reviewer.allowed_numbers({"stages": {"thr=316.2": {"margin": 0.0973, "n": 100}}, "sha": "9e3"}, p.text)
    ok = lambda s: reviewer.problems(s, led, p, allowed) == []
    assert ok('The paper says "reaches 61.4 accuracy on CIFAR" and the margin was 0.0973 over 100 runs.')
    assert not ok('The main result was "reproduced" here.')                     # a quoted status word is still REFEREE's
    assert not ok("The gap was -0.4417 on average.") and not ok("It was 2.73x faster.") and not ok("It took 150ms.")
    assert not ok("The count was 9000.")                                         # '9e3' in a string is no number held
    assert ok("At threshold 316.2 the gap held.")                                # a stage name's number is held
    assert not ok("The result was &#114;eproduced.") and not ok("<b>bold</b> claim")
    assert not ok("<script src=x>") and not ok("a &amp; b") and not ok("<!-- hidden -->")
    assert ok("Two of five settings ran.")
    # Oct-06 changepoint K6: "the printed premise t_F<t_H" is mathematics, not a tag
    assert ok("The printed premise t_F<t_H fails while H(t)>=F holds, and x<y>z is an inequality.")


def test_follow_up_claims_keep_their_own_entries_and_never_reuse_an_id():
    """Review #11/#12: new claims were paired with the unfiltered raw list (a dropped A shifted B onto A's checks), and
    were numbered after the extracted ones only, so a re-plan reused the id of a withdrawn follow-up's claim and the
    ledger dropped it."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _claims_project(td)
        _seal(cfg, pid, "claims", {"claims": _K[:2]}, td)
        for lens in tasks.LENSES:
            _seal(cfg, pid, f"lens:{lens}", {"concerns": []}, td)
        _seal(cfg, pid, "critic", {"reviews": []}, td)
        _seal(cfg, pid, "plan", {"checks": [], "central_claims": [{"id": k, "why_unchecked": "no test", "blocker": "other"}
                                                                  for k in ("K1", "K2")]}, td)
        x = tasks._Ctx(cfg, pid)
        withdrawn = {"checks": [], "central_claims": [{"id": "K3", "quote": "x", "checks": []}]}
        state.write_json(td / pid / "sealed" / "plan__2.withdrawn.1.json", withdrawn)
        seals = state.read_json(td / pid / "seals.json")
        seals["plan:2.withdrawn.1"] = state.sha256((td / pid / "sealed" / "plan__2.withdrawn.1.json").read_bytes())
        state.write_json(td / pid / "seals.json", seals)
        bad = {"quote": "a sentence the paper never printed anywhere", "statement": "x" * 20, "claim_type": "theory",
               "scope": ["x"], "required_evidence": "a proof of it", "why_unchecked": "BAD ONE"}
        good = {**_K[2], "why_unchecked": "the good one's own reason", "blocker": "other"}
        rec = tasks._seal_plan(x, "plan:2", {"checks": [], "central_claims": [], "new_claims": [bad, good]}, final=True)
        new = [c for c in rec["central_claims"] if c.get("origin") == "plan:2"]
        assert [c["id"] for c in new] == ["K4"], new                               # after the withdrawn K3
        assert new[0]["why_unchecked"] == "the good one's own reason"              # its own entry, not BAD ONE's


def test_counts_and_completion_read_unnamed_and_undefined_units_honestly():
    """Review #14/#15: one declared unit printed without its name counted as missing (DOES NOT RECONCILE); and a script
    could turn missing settings into 'undefined' ones at run time and still reach run as specified."""
    one = {"kind": "RELEASED_DATA", "values": [1.0], "units": ["mean_ece_top10"], "stages": {},
           "execution": {"runs_ended": 1}}
    n = report.counts(one)
    assert n["units_not_completed"] == 0 and n["reconciles"], n
    c = {"id": "C1", "kind": "RECONSTRUCTION", "role": "target", "state": "COMPLETED", "status": "RELATION_HOLDS",
         "values": [1.0], "covers": ["X"], "deviations": [], "data_identity": {}, "data_changed": [],
         "stages": {"a": {"status": "RELATION_HOLDS"}, "b": {"status": "UNDEFINED", "reason": "no events"}}}
    row = report._completion_row({"quote": "q", "page": 1, "claim_type": "performance", "checks": ["C1"], "scope": ["X"]},
                                 {"C1": c})
    assert row["experiment"] == "RAN_PARTIAL" and row["undefined_settings"] == ["C1: b"], row
    from harness.reviewer import decision
    mixed = _claim_row("SUPPORT_FOUND", exp="RAN_WITH_CHANGES")
    assert decision(mixed, {})["reason"] == "changed_protocol"                     # never "no test was run"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} kernel checks passed")
