"""The trust kernel, and nothing else: each check fails if a guarantee breaks.
Run: python tests/test_kernel.py   (or pytest)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import execute, report, state, tasks  # noqa: E402
from harness.evidence import Paper, evaluate, interval, value_in  # noqa: E402
from harness.reconcile import arithmetic, reconcile  # noqa: E402

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
    assert reconcile("CERTIFICATE", "", [0, 0], "", {}, False, True, "")["status"] == "NO_VIOLATION_FOUND"
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
        arith = {"id": "A", "kind": "ARITHMETIC", "claim_quote": "The baseline reaches 59.3",
                 "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"},
                 "operands": [{"name": "a", "quote": "reaches 61.4 accuracy", "value": "61.4"}], "expression": "a"}
        cert = {"id": "B", "kind": "CERTIFICATE", "claim_quote": "We report the mean over 5 random seeds",
                "statement_quote": "The final loss is -0.52"}
        central = [{"quote": "We report the mean over 5 random seeds", "checks": ["B"]}]
        cfg.max_checks = 1        # the incidental check came first; the central one still gets the slot
        rec = tasks._seal_plan(x, "plan", {"checks": [arith, cert], "central_claims": central}, final=False)
        assert [c["kind"] for c in rec["checks"]] == ["CERTIFICATE"] and rec["checks"][0]["central"]
        assert rec["central_claims"][0]["checks"] == ["C1"] and rec["dropped"][0]["check"] == "A"
        cfg.max_checks = 3
        assert "incidental_why" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [arith]}, final=False))
        rec = tasks._seal_plan(x, "plan", {"checks": [{**arith, "incidental_why": "no central claim computes"}]}, final=False)
        assert rec["checks"][0]["central"] is False
        code = {"id": "B", "kind": "AUTHOR_CODE", "claim_quote": "We report the mean over 5 random seeds",
                "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}
        assert "`metric`" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [code], "central_claims": central},
                                                              final=False))   # AUTHOR_CODE: the planner's key
        rel = {"id": "B", "kind": "RECONSTRUCTION", "claim_quote": "We report the mean over 5 random seeds",
               "target": {"quote": "Our method reaches 61.4", "relation": "acc_ours > acc_base"}}
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
                "central_claims": [{"quote": "a", "page": 1, "checks": ["C1"], "why_unchecked": ""},
                                   {"quote": "b", "page": 1, "checks": [], "why_unchecked": "figure"}]}
        state.write_json(root / "sealed" / "plan.json", plan)
        assert report.scientific_status(root) == "CHECKS_PENDING"
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "NO_VIOLATION_FOUND"})
        for s in ("ARITHMETIC_CONSISTENT", "ARITHMETIC_CONTRADICTION"):   # incidental: never lifts or sinks it
            state.write_json(root / "checks" / "C2" / "outcome.json", {"status": s})
            assert report.scientific_status(root) == "CENTRAL_NO_VIOLATION_FOUND"
        assert report._central(plan, report._checks(root, plan), [])[0]["claim_status"] == "NO_VIOLATION_FOUND"
        dev = {"printed": "for j = 1..J", "used": "j = 0..J-1", "why": "1-based leaves the range", "page": 6,
               "changes_claim": True}
        state.write_json(root / "checks" / "C1" / "check.json", {"deviations": [dev]})
        state.write_json(root / "checks" / "C1" / "outcome.json",
                         {"status": "NO_VIOLATION_FOUND", "literal": {"undefined": 36}})
        assert report._central(plan, report._checks(root, plan), [])[0]["claim_status"] == "READING_CHANGED"
        assert report.scientific_status(root) == "CENTRAL_READING_CHANGED"      # a changed reading: not support
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "PREMISE_NOT_MET"})
        assert report.scientific_status(root) == "CENTRAL_PREMISE_NOT_MET"      # an unmet premise: not a failure
        plan["checks"].append({"id": "C3", "kind": "CERTIFICATE", "concerns": [], "claim": "x", "statement": "Theorem 1"})
        plan["central_claims"][1]["checks"] = ["C3"]
        state.write_json(root / "checks" / "C1" / "check.json", {})
        state.write_json(root / "checks" / "C3" / "outcome.json", {"status": "COUNTEREXAMPLE_FOUND"})
        conf = report.conflicts(report._checks(root, plan))       # same reading, opposite results
        assert conf == []                                         # C1's premise was never met: nothing to disagree
        state.write_json(root / "checks" / "C1" / "outcome.json", {"status": "NO_VIOLATION_FOUND"})
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
    assert report._claim_status([step]) == "PROOF_GAP_FOUND"            # the printed step fails as printed
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
               "statement_quote": "The final loss is -0.52", "covers": ["PL", "MM"]}
        claim = {"quote": "We report the mean over 5 random seeds", "checks": ["B"], "scope": ["PL", "MM", "RPC"]}
        assert "RPC" in _refused(lambda: tasks._seal_plan(x, "plan", {"checks": [chk], "central_claims": [claim]},
                                                          final=False))
        claim["omitted"] = [{"item": "RPC", "why": "its pairwise ECE needs the vendored Cython build, which failed"}]
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [claim]}, td)
        state.write_json(td / pid / "checks" / "C1" / "outcome.json", {"status": "NOT_CHECKABLE", "reason": "refused"})
        phase, owed, _ = tasks._plan(tasks._Ctx(cfg, pid))
        assert phase == "plan" and [o["id"] for o in owed] == ["plan:2"]   # an undecided central claim: a follow-up
        assert "FOLLOW-UP" in Path(owed[0]["prompt"]).read_text(encoding="utf-8")
        _seal(cfg, pid, "plan:2", {"checks": [{**chk, "id": "F1", "covers": []}],
                                   "central_claims": [{"quote": claim["quote"], "checks": ["F1"]}]}, td)
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
               "statement_quote": "The final loss is -0.52"}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [{"quote": chk["claim_quote"], "checks": ["B"]}]}, td)
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
               "statement_quote": "The final loss is -0.52"}
        _seal(cfg, pid, "plan", {"checks": [chk], "central_claims": [{"quote": chk["claim_quote"], "checks": ["B"]}]}, td)
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
              "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"},
              "readings": [{"name": "paper", "source": "paper", "quote": "We use generation of samples"},
                           {"name": "code", "source": "analyze.py", "quote": code_line}]}
        central = [{"quote": "We report the mean over 5 random seeds", "checks": ["B"]}]
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
        compat = {"id": "B", "kind": "RECONSTRUCTION", "test": "compatibility",
                  "claim_quote": "We report the mean over 5 random seeds",
                  "target": {"quote": "Our method reaches 61.4", "relation": "loss_first - loss_last > 0"},
                  "define": {"loss_first": "training loss after epoch 1", "loss_last": "training loss after the last epoch"}}
        eng = [{"quote": "We report the mean over 5 random seeds", "claim_type": "engineering", "checks": ["B"]}]
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
    # ...unless the seed is shown to reach a generator: draws that coincided, not one run repeated
    assert run(same, staged(0.0, 3), rng=True)["status"] == "NO_VIOLATION_FOUND"
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
    assert z["status"] == "RELATION_HOLDS" and z["events"] == 0 and z["trials"] == 1200 and z["ci"][1] < 0.004
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
        ignores = "acc = 61.0\nfit()\nload()\nrng = np.random.default_rng(7)\nprint('REFEREE_RESULT', acc)\n"
        assert "never reaches a random generator" in _refused(lambda: tasks._seal_gen(x, "gen:C1.1", {**g, "script": ignores}, final=False))
        seeded = ignores.replace("default_rng(7)", "default_rng(args.seed)")
        assert tasks._seal_gen(x, "gen:C1.1", {**g, "script": seeded}, final=False)["seed_flow"] is True
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
               "covers": ["CIFAR-10-C"], "acquire": [{"source": "https://data.example.org/set.zip", "cited_in": "paper"}]}
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
        sim = "acc_a = 1\nfit()\nload()\nrng = np.random.default_rng(args.seed)\nprint('REFEREE_RESULT', acc_a)\n"
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
        cert = {"id": "T", "kind": "CERTIFICATE", "claim_quote": thm, "statement_quote": thm}
        run = {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": claim, "target": {"quote": claim, "relation": "acc_a > acc_b"},
               "covers": ["CIFAR-10-C"]}
        cc = {"quote": claim, "claim_type": "performance", "scope": ["CIFAR-10-C"]}

        def seal(checks, claim_):
            return tasks._seal_plan(x, "plan", {"checks": checks, "central_claims": [claim_]}, final=False)
        # a proof of a related lemma cannot stand for the experiment; beside it, as supporting evidence, it can
        assert "cannot stand for the performance claim" in _refused(lambda: seal([cert], {**cc, "checks": ["T"], "omitted": []}))
        rec = seal([{**cert, "role": "supporting"}], {**cc, "checks": ["T"], "blocker": "compute", "omitted": [
            {"item": "CIFAR-10-C", "why": "needs 5 GB per run", "blocker": "compute"}]})
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
        seal([], {"quote": "Method C uses GPT-4.1 as its labeler", "claim_type": "performance", "scope": ["GPT-4.1"], "checks": [],
                  "why_unchecked": "a paid closed API", "blocker": "credentials"})


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
        checks = [{"id": "B", "kind": "CERTIFICATE", "claim_quote": thm, "statement_quote": thm},
                  {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": cifar, "target": rel(cifar, "acc_a > acc_b"),
                   "covers": ["CIFAR-10-C"], "acquire": [{"source": "https://data.example.org/set.zip", "cited_in": "paper"}]},
                  {"id": "S", "kind": "CERTIFICATE", "claim_quote": cifar, "statement_quote": "The error bars use 95% CIs",
                   "role": "supporting"},
                  {"id": "M", "kind": "RECONSTRUCTION", "claim_quote": mmlu, "target": rel(mmlu, "acc_llm > acc_base"), "covers": ["MMLU"]}]
        central = [{"quote": thm, "claim_type": "theory", "checks": ["B"], "scope": []},
                   {"quote": cifar, "claim_type": "performance", "checks": ["R", "S"], "scope": ["CIFAR-10-C"]},
                   {"quote": mmlu, "claim_type": "performance", "checks": ["M"], "scope": ["MMLU"]},
                   {"quote": gpt, "claim_type": "performance", "checks": [], "scope": ["GPT-4.1"],
                    "why_unchecked": "a paid closed API", "blocker": "credentials"}]
        _seal(cfg, pid, "plan", {"checks": checks, "central_claims": central}, td)
        done = {"runs_planned": 3, "runs_ended": 3, "runs_exited_ok": 3, "runs_failed": []}
        write = lambda cid, outcome, check=None: (state.write_json(td / pid / "checks" / cid / "outcome.json", outcome),
                                                  state.write_json(td / pid / "checks" / cid / "check.json", check or {}))
        write("C1", {"status": "COUNTEREXAMPLE_FOUND", "reason": "a violation", "values": [1], "execution": done})
        write("C2", {"status": "BLOCKED", "reason": "DATA BLOCKER (missing): x", "data_blocker": [{"source": "s", "class": "missing"}]})
        write("C3", {"status": "NO_VIOLATION_FOUND", "reason": "held", "values": [0], "execution": done})
        write("C4", {"status": "RELATION_HOLDS", "reason": "held", "values": [0.1, 0.2, 0.15], "execution": done},
              {"deviations": [{"printed": "", "used": "a simulated stand-in for the MMLU items", "changes_claim": True}]})
        led = report.ledger(tasks._Ctx(cfg, pid))
        pick = lambda prefix: next(r for r in led["completion"]["claims"] if r["claim"].startswith(prefix))
        assert led["scientific_status"] == "CENTRAL_FAILURE_FOUND"          # what was found about the theorem...
        th, cf, mm, gp = (pick(p) for p in ("Theorem 1", "Method A beats", "Our LLM monitor", "Method C uses"))
        assert th["experiment"] == "RAN_AS_SPECIFIED" and th["evidence"] == "FAILURE_FOUND"
        assert cf["experiment"] == "NOT_RUN" and cf["protocol_matched"] is None and cf["evidence"] == "NOTHING_DECIDED"   # ...says nothing about CIFAR
        assert cf["not_run"][0]["blocker"] == "data" and cf["not_run"][0]["basis"] == "harness" and cf["not_run"][0]["class"] == "missing"
        assert cf["supporting"] == [{"check": "C3", "kind": "CERTIFICATE", "status": "NO_VIOLATION_FOUND", "state": "COMPLETED"}]
        assert mm["experiment"] == "RAN_WITH_CHANGES" and mm["protocol_matched"] is False and "simulated stand-in" in mm["changes"][0]
        assert mm["evidence"] == "READING_CHANGED"                          # about the changed claim, never support of the printed one
        assert gp["experiment"] == "NOT_RUN" and gp["not_run"][0]["blocker"] == "credentials" and gp["not_run"][0]["basis"] == "planner"
        assert led["workflow"]["reached_terminal_state"] == 4               # every check is terminal, and it says nothing more
        line = report._completion_line(led["completion"])
        assert "3 — 1 ran with claim-changing changes" in line and "2 not run" in line and "ran as specified" not in line
        assert "data 1" in line and "credentials 1" in line and "not a measure of what was reproduced" in line
        table = report.table(led)
        assert "Completion, kept apart from what was found" in table and "experiment **NOT_RUN**" in table
        assert "supporting only: C3" in table and "reached a terminal state" in table
        # a supporting check never lets an empirical claim read as supported
        assert report._claim_status([{"id": "C3", "kind": "CERTIFICATE", "evidence": "x", "status": "NO_VIOLATION_FOUND",
                                      "deviations": [], "role": "supporting"}], claim_type="performance") == "NOT_CHECKED"
        assert report._claim_status([{"id": "C3", "kind": "CERTIFICATE", "evidence": "x", "status": "NO_VIOLATION_FOUND",
                                      "deviations": [], "role": "supporting"}], claim_type="theory") == "NO_VIOLATION_FOUND"


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
        chk = {"id": "M", "kind": "RECONSTRUCTION", "claim_quote": mmlu, "covers": ["MMLU"],
               "target": {"quote": mmlu, "relation": "acc_llm > acc_base"}}
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
    assert [o["item"] for o in merged["central_claims"][0]["omitted"]] == ["URM"]                 # the experiment that ran is not also 'not run'
    assert merged["central_claims"][0]["checks"] == ["C1", "C7"]


def test_a_criterion_the_planner_supplied_makes_the_result_about_that_criterion():
    """Sep-30 rerun, transformer: 'provides a surprisingly good description' states no comparison; the planner's rival
    family decided it, one check labelled that a protocol choice, and the claim read FAILURE_FOUND."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        cfg, pid = _project(td)
        x = tasks._Ctx(cfg, pid)
        claim = "We report the mean over 5 random seeds"
        rel = {"id": "R", "kind": "RECONSTRUCTION", "claim_quote": claim, "target": {"quote": claim, "relation": "chi2_formula < chi2_rival"}}
        central = [{"quote": claim, "claim_type": "performance", "checks": ["R"]}]
        rec = tasks._seal_plan(x, "plan", {"checks": [{**rel, "criterion": "supplied"}], "central_claims": central}, final=False)
        assert rec["checks"][0]["criterion"] == "supplied"
        assert tasks._seal_plan(x, "plan", {"checks": [rel], "central_claims": central}, final=False)["checks"][0]["criterion"] == "stated"
        plan = {"checks": [rec["checks"][0]]}
        x2 = type("X", (), {"root": td, "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, tid: plan,
                            "plan": lambda self: plan})()
        binds = [{"kind": k, "impl_quote": q, "paper_quote": claim} for k, q in
                 (("method", "fit()"), ("training", "fit()"), ("dataset", "load()"), ("metric", "a = 1"), ("comparison_target", "print("))]
        script = "a = 1\nfit()\nload()\nrng = np.random.default_rng(args.seed)\nprint('REFEREE_RESULT', a)\n"
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
        script = "acc = 61.0\nrun()\nload()\nrng = np.random.default_rng(args.seed)\nprint('REFEREE_RESULT', acc)\n"
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


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} kernel checks passed")
