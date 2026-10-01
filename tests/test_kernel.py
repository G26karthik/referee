"""The trust kernel, and nothing else: each check fails if a guarantee breaks.
Run: python tests/test_kernel.py   (or pytest)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
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
    f = td / "answer.json"
    f.write_text(json.dumps(obj), encoding="utf-8")
    return tasks.seal(cfg, pid, tid, str(f))


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
        g = {"script": script, "runs": 1, "outputs": ["n_no_cp", "violated"], "bindings": binds}
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
        g = {"script": script, "runs": 1, "metric": "ece", "outputs": ["ece"], "bindings": binds}
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
        g = {"script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": binds}
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
        g = {"runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": binds, "stochastic": True}
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
        g = {"runs": 1, "outputs": ["acc_a", "acc_b"], "bindings": binds, "stochastic": True}
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
            {**omit, "paper_quote": "Method A beats method B on CIFAR-10-C with ResNet-32"}]})
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
    planned = report._completion_row(cc, {"C1": chk("C1", "NOT_RUN", []), "C7": chk("C7", "NOT_STARTED", ["CIFAR10-C image monitoring"])})
    assert planned["experiment"] == "NOT_RUN" and planned["scope_not_run"] == ["CIFAR10-C image monitoring", "URM"]


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
        g = {"script": script, "runs": 1, "outputs": ["chi2_formula", "chi2_rival"], "bindings": binds, "stochastic": True}
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
        g = {"script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "stochastic": True}
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


def _x(td: Path):
    cfg, pid = _project_pages(td, DATA_PAGES)
    return cfg, pid, tasks._Ctx(cfg, pid)


def _run(cid="R", covers=("CIFAR-10-C",), **kw):
    return {"id": cid, "kind": "RECONSTRUCTION", "claim_quote": CIFAR, "target": {"quote": CIFAR, "relation": "acc_a > acc_b"},
            "covers": list(covers), **RUN, **kw}


def _claim(**kw):
    return {"quote": CIFAR, "claim_type": "performance", "checks": ["R"], "scope": ["CIFAR-10-C"], **kw}


def _plan(x, checks, claims, final=False, tid="plan"):
    return tasks._seal_plan(x, tid, {"checks": checks, "central_claims": claims}, final=final)


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
        cfg, pid, x = _x(Path(t))
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
        _plan(x, [_run()], [omit(blocker="compute", paper_quote="Method A beats method B on CIFAR-10-C")])
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
        g = {"script": SCRIPT, "runs": 1, "outputs": ["acc_a", "acc_b"], "bindings": BINDS, "stochastic": True}
        assert "merge choices of one kind" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs}, final=False))
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs}, final=True)["refused"]
        ok = tasks._seal_gen(x, "gen:C1.1", {**g, "deviations": devs[:tasks.MAX_DEVIATIONS]}, final=False)
        assert len(ok["deviations"]) == tasks.MAX_DEVIATIONS


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
        g = {"script": script, "runs": 1, "metric": "acc", "outputs": ["acc"], "bindings": b}
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
    assert h["status"] == READINGS_DIFFER and "s2" in h["reason"] and "s3" in h["reason"] and "s1" not in h["reason"], h
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

if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} kernel checks passed")
