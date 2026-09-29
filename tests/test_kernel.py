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
    assert reconcile("CERTIFICATE", "", [0, 1, 0], "", {}, False, True, "")["status"] == "COUNTEREXAMPLE_FOUND"
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
        chk = {"kind": "RELEASED_DATA", "claim_quote": "reaches 61.4 accuracy",
               "target": {"quote": "reaches 61.4 accuracy", "value": "61.4"}}
        for metric, refused in (("", True), ("acc", False)):   # an unnamed output can never be compared
            try:
                tasks._seal_plan(x, "plan", {"checks": [{**chk, "metric": metric}], "central_claims": []}, final=False)
                err = ""
            except tasks.SealError as e:
                err = str(e)
            assert ("`metric`" in err) == refused, err
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
        x = type("X", (), {"checkout": co, "paper": Paper(PAGES, ROWS), "cfg": cfg, "sealed": lambda self, t: plan,
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
