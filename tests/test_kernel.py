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
    assert execute.resource_action({"mode": "evidence", "returncode": 255}, {}, "0", 60) == ("", "")
    with tempfile.TemporaryDirectory() as t:                  # completed seeds of the same script are reused
        cdir = Path(t)
        for k, sha in ((0, "a"), (1, "a"), (0, "b")):
            state.append_jsonl(cdir / "seeds.jsonl", {"key": sha, "seed": k, "values": [k], "seconds": 5})
        assert [r["seed"] for r in execute._checkpoints(cdir, {"script_sha256": "a", "runs": 3})] == [0, 1]
    pr = execute.protocol({"kind": "RECONSTRUCTION", "runs": 3, "runs_source": "referee_floor",
                           "target": {"relation": "a < b"}, "deviations": [
                               {"printed": "", "used": "thresholds 50..700", "changes_claim": False},
                               {"printed": "x", "used": "0-based j", "changes_claim": True}]}, {"pilot_s": 8}, "t-test")
    assert "REFEREE" in pr["runs_from"] and pr["supplied_by_referee"] == ["thresholds 50..700"]
    assert pr["claim_changes"] == ["0-based j"] and "planner" in pr["relation"] and pr["pilot_seconds"] == 8


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
    single = reconcile("RECONSTRUCTION", "", [0.7, 0.72], "", {}, True, True, "", rel,
                       staged=[["", 0.7], ["", 0.72]], failed={"2": "exit 1"})
    assert single["status"] == PARTIAL and single["status_on_completed"] == "RELATION_HOLDS"
    c = {"id": "C1", "kind": "RECONSTRUCTION", "evidence": "PAPER_DERIVED_IMPLEMENTATION", "deviations": [],
         "status": PARTIAL}
    assert report._claim_status([c]) == "PARTIAL_EVIDENCE"
    assert report._headline([c], [{"claim_status": "PARTIAL_EVIDENCE"}]) == "CENTRAL_PARTIAL_EVIDENCE"
    assert report._state(Path("."), "C1", {"status": PARTIAL}) == "PARTIALLY_COMPLETED"
    assert report._state(Path("."), "C1", {"status": "BLOCKED", "reason": "RESOURCE BLOCKER: x"}) == "RESOURCE_LIMITED"


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


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} kernel checks passed")
