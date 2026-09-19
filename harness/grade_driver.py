"""S2.5 autonomy: grade one candidate finding by shelling out to a blinded reviewer.

Mirrors `audit_driver.py` on everything that matters for provenance and safety —
staging before promotion, a kept `.rejected.txt` on failure, a `.driver.json` sidecar
recording who wrote it, tree-kill on timeout — and differs on exactly what blinding
requires: no tools at all, not even `Read` (the grader must not be able to open
`audit/<lens>.json` and read the severity it was not given) and NO `--add-dir` (no
filesystem access outside an empty sandbox cwd, so there is nothing to add a directory TO).

THREE THINGS THIS MODULE CLAIMED AND DID NOT DO, each fixed below:

  1. "Zero tools, no filesystem access at all" was `--allowedTools ""` and nothing else.
     An empty ALLOW list denies nothing on its own — what a session may do without one is
     a property of the CLI's permission mode. The grader now runs with the same deny-side
     flags a lens gets plus `--bare`, which is the flag that stops the operator's own
     user-level `CLAUDE.md`, hooks and plugins from loading. For a lens those are noise;
     for the reader whose whole value is not having seen the first reader's opinion, an
     operator's own notes about this paper are a BLINDING LEAK.
  2. `default_cmd()` took no arguments and emitted no `--model`, so the "independent
     second reader" ran on the CLI's ambient default — on the development host, the same
     model family as the `overclaim` lens it was meant to check. A grader whose identity
     is a property of the operator's shell is not a recorded fact about the review.
  3. The grade sidecar recorded no `tool_policy` at all, while `audit_driver` and
     `stages/grade.accept_grade` both did — so the one delegated role with the strongest
     isolation claim was the only one that wrote nothing down about it.

`_kill_tree`, the confinement machinery, the result-envelope reader and the failure
classification are all reused from `audit_driver` rather than re-implemented — one
instance of each, not two that could drift apart.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import delegation, reviewer_cli, sealing, state
from .artifacts import Grade
from .config import Config
from .prompts import grade as GP
from .reviewer_cli import (KNOWN_TOOLS, Confinement, NonRetryable, RateLimited,
                           _kill_tree, classify_delegated_failure, denied_tools,
                           envelope_provenance, keep_prompt_copy, operator_confinement,
                           prompt_fingerprint, unwrap_envelope, write_pinned_settings)


class GradeDriverError(RuntimeError):
    """The configured grader did not produce a usable grade.

    Self-classifying, exactly as `audit_driver.AuditDriverError` is and for the same
    reason. NOT a subclass of it: the grading path is optional and must never be able to
    block a report, so an `except AuditDriverError` in the audit path must not
    accidentally swallow a grading failure or vice versa.
    """

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = classify_delegated_failure(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


def grade_error(message: str) -> GradeDriverError | RateLimited | NonRetryable:
    """The right class for one grader failure. Mirrors `audit_driver.driver_error`.

    The two `later`/`never` classes are shared with the audit path deliberately: the
    controller's retry accounting keys on them, and a rate limit is the same fact about
    the world whichever subprocess met it.
    """
    kind, retry, hint = classify_delegated_failure(message)
    if retry == "later":
        return RateLimited(message, kind=kind, retry=retry, reset_hint=hint)
    if retry == "never":
        return NonRetryable(message, kind=kind, retry=retry, reset_hint=hint)
    return GradeDriverError(message, kind=kind, retry=retry, reset_hint=hint)


def role_model(cfg: Config) -> str:
    """WHICH model grades: the operator's `SH_GRADE_MODEL`, else the declared role default.

    Never the CLI's ambient default. What the ceiling still is, stated rather than hidden:
    every role in this system runs through one vendor's CLI, so "independent" here means a
    separate process with a separate context and a separately named model — not a second
    vendor. `prompts.grade.ROLE_SPEC` says the same thing where a reader of the panel table
    will see it.
    """
    return delegation.resolve_model(cfg.grade_model, GP.ROLE_SPEC.get("model", ""))


def grade_confinement(cfg: Config, *, settings_sha256: str = "") -> Confinement:
    """The confinement the blinded grader runs under: no tools, no directories, `--bare`.

    `--bare` is the difference from a lens, and it is the flag that makes the blinding
    claim true rather than merely intended: it skips hooks, plugins and `CLAUDE.md`
    auto-discovery, and a user-level `CLAUDE.md` reaches every session regardless of cwd.
    A lens keeps them out through `--restricted` + `--settings` alone because it
    legitimately needs `Read`; the grader needs nothing, so it can take the stricter flag.
    """
    if cfg.grade_cmd.strip():
        return operator_confinement("grader")
    return Confinement(
        role="grader", allowed_tools=(), disallowed_tools=denied_tools(()),
        add_dir=(), model=role_model(cfg), restricted=True, strict_mcp=True, bare=True,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "") -> str:
    """The built-in grader invocation, or '' when no grader can be found.

    Takes a `Config` now, where it used to take nothing at all — which is exactly why it
    emitted no `--model` and the grader ran on whatever the operator's settings file said.
    `exe` overrides PATH discovery so every assertion about this argv runs on a host with
    no CLI; `settings` is supplied by the runner, for the reason `audit_driver.default_cmd`
    gives.
    """
    cfg = cfg or Config()
    exe = delegation.resolve_reviewer_exe(exe, cfg.reviewer_exe)
    if not exe:
        return ""
    extra = grade_confinement(Config(grade_cmd="", grade_model=cfg.grade_model)).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, *, settings: str = "") -> str:
    return cfg.grade_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe, settings=settings)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_grading:
        # See `audit_driver.available` for why the flag is no longer named here.
        return False, ("grading gate is closed: SH_ALLOW_GRADING is not set. Set it (in "
                       "the environment or .env.sandbox) to let the controller "
                       "independently grade findings")
    return delegation.check_command(resolve_cmd(cfg), "SH_GRADE_CMD")


def parse_grade_json(text: str) -> Grade:
    """A `Grade` from the command's output. The thin public API; see `parse_grade_report`."""
    return parse_grade_report(text)[0]


def parse_grade_report(text: str) -> tuple[Grade, dict]:
    """(grade, meta) — the parsed grade plus what the parse observed.

    `meta` carries the CLI's own result envelope and the closed-vocabulary desiderata the
    grader says this candidate violates. Tolerant of prose/fences wrapping the JSON, like
    `audit_driver.parse_lens_report` — same reviewer, same habit — and of the
    `--output-format json` envelope. Not tolerant of a missing `verdict`: a grade with no
    verdict at all is not a grade that happened.

    UNKNOWN KEYS ARE DROPPED rather than kept. `artifacts._Base` is `extra="allow"`, so a
    grader could previously write `weight: 8` or `score: 0.72` into the sealed grade file
    and any future consumer would find a number sitting on a model's word. The two
    external rubrics this design was compared against both do exactly that, and both were
    rejected for it: a model-assigned numeric weight is a model setting severity, which
    invariant 11 forbids outright.
    """
    outer = (text or "").strip()
    if not outer:
        raise GradeDriverError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                  "desiderata_violations": [], "desiderata_passed": None,
                  "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise grade_error(raw[:300] or "the grader reported an error and returned no result")
    if not raw:
        raise GradeDriverError("the grader returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise GradeDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise GradeDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise GradeDriverError("output JSON is not an object")
    if not str(data.get("verdict") or "").strip():
        raise GradeDriverError("output JSON has no 'verdict'")

    violations, unknown_desiderata = GP.parse_violations(data.get("desiderata_violations"))
    meta["desiderata_violations"] = list(violations)
    meta["unknown_desiderata"] = list(unknown_desiderata)
    # THE DETERMINISTIC PASS RULE. An empty violation set over a CLOSED vocabulary, not a
    # score — so "did this candidate pass" is machine-checkable from the record instead of
    # being a threshold over a number a model chose. `None` when the grader said nothing
    # at all about the desiderata: a grader that did not answer has not passed a candidate,
    # and defaulting to `True` (or to 0.5, or to `[False] * n`) would be a fabricated
    # measurement in the shape invariants 6, 7 and 21 all forbid.
    if data.get("desiderata_violations") is not None or violations:
        meta["desiderata_passed"] = len(violations) == 0
    if unknown_desiderata:
        # A grader that invents a desideratum is writing its own rubric. The invented
        # names are recorded and excluded, and the pass rule then cannot be satisfied by
        # naming zero of the ones that exist while naming several that do not.
        meta["desiderata_passed"] = False

    clean = {k: v for k, v in data.items() if k in Grade.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return Grade(**clean), meta
    except Exception as e:
        raise GradeDriverError(f"output does not match the grade schema: {e}") from e


def run_candidate(cfg: Config, pid: str, slug: str, prompt: Path, out: Path, *,
             withheld: list[str]) -> dict:
    """Run the configured grader for one candidate. Returns the sidecar record; raises
    on failure. Same staging/promotion discipline as `audit_driver.run_lens` — see that
    function's docstring for why the output is never redirected straight at `out`."""
    ok, why = available(cfg)
    if not ok:
        raise GradeDriverError(why)
    if not prompt.exists():
        raise GradeDriverError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    staged = out.with_suffix(".staged")
    rejected = out.with_name(f"{slug}.rejected.txt")
    raw_kept = out.with_name(f"{slug}.raw.txt")
    for p_ in (staged, rejected, raw_kept):
        p_.unlink(missing_ok=True)

    prompt_sha = prompt_fingerprint(prompt)
    prompt_copy = keep_prompt_copy(prompt, prompt_sha)
    policy_dir = Path(tempfile.mkdtemp(prefix=f"sh-grade-policy-{slug}-"))
    conf = grade_confinement(cfg)
    settings_path = ""
    try:
        if conf.enforced:
            sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
            settings_path = str(sp)
            conf = dataclasses.replace(conf, settings_sha256=sha)
    except OSError as e:
        # See `audit_driver.run_lens`: refuse rather than grade unconfined.
        shutil.rmtree(policy_dir, ignore_errors=True)
        raise GradeDriverError(f"could not write the pinned tool policy: {e}") from e
    cmd = (resolve_cmd(cfg, settings=settings_path)
           .replace("{prompt}", str(prompt)).replace("{out}", str(staged)))
    started = time.time()
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True})
    sandbox = Path(tempfile.mkdtemp(prefix=f"sh-grade-{slug}-"))
    try:
        try:
            # `stdin=DEVNULL` — see `audit_driver.run_lens`. A grader that stops to ask a
            # question is a human checkpoint in the middle of an autonomous stage.
            proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", **group_kwargs)
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise GradeDriverError(f"could not start the command: {e}") from e
        try:
            stdout, stderr = proc.communicate(timeout=cfg.grade_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            staged.unlink(missing_ok=True)
            raise GradeDriverError(f"timed out after {cfg.grade_timeout_s}s") from None
        p = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

        if not staged.exists():
            tail = (p.stderr or p.stdout or "").strip()[-300:]
            err = grade_error(tail or f"the command exited {p.returncode} with no output")
            if getattr(err, "kind", "unknown") != "unknown":
                raise err
            raise GradeDriverError(
                f"the command exited {p.returncode} without writing {out.name}"
                + (f" — {tail}" if tail else ""))

        raw = staged.read_text(encoding="utf-8")
        try:
            grade, meta = parse_grade_report(raw)
        except GradeDriverError:
            classified = grade_error(raw.strip()[:300])
            if getattr(classified, "retry", "now") in ("later", "never"):
                staged.replace(rejected)
                raise classified from None
            staged.replace(rejected)
            raise
        staged.replace(raw_kept)                 # kept on success too — see `run_lens`

        # This IS a `CLI_SUBPROCESS` call, exactly as `audit_driver.run_lens`'s own inline
        # seal is — but this driver's OWN written_by token is "grade_driver", not the
        # generic delegation vocabulary's "audit_driver", so it is forced via `extra`
        # exactly as `stages.probe.accept_spec` forces "driver_accept".
        return sealing.seal(out, grade.model_dump(), mode="CLI_SUBPROCESS",
                            tool_policy=conf.summary(), extra={
            "slug": slug, "paper_id": pid, "command": cmd, "returncode": p.returncode,
            "seconds": round(time.time() - started, 1), "verdict": grade.verdict,
            "written_by": "grade_driver", "withheld": withheld,
            # Recorded at last. This was the one delegated role with the strongest
            # isolation claim and the only sidecar that wrote nothing about it.
            "tool_policy_detail": conf.policy(),
            "prompt_sha256": prompt_sha,
            "prompt_copy": str(prompt_copy) if prompt_copy else "",
            "raw_sha256": hashlib.sha256(raw_kept.read_bytes()).hexdigest(),
            "raw_response": str(raw_kept),
            "envelope": meta.get("envelope") or {},
            # REPORTED, and consumed by no threshold in this module. `desiderata_passed`
            # is `len(violations) == 0` over a closed vocabulary, which is what makes it
            # machine-checkable from the record — and it reaches `grading.derive` only if
            # and when that function grows a vocabulary-string parameter for it. Nothing
            # here may promote a severity, so nothing here is wired into one.
            "desiderata_violations": meta.get("desiderata_violations") or [],
            "desiderata_passed": meta.get("desiderata_passed"),
            "unknown_desiderata": meta.get("unknown_desiderata") or [],
            "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
        })
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(policy_dir, ignore_errors=True)


def fill(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str]) -> dict:
    """Attempt every pending candidate. Mirrors `audit_driver.fill` exactly, including
    the retry-policy buckets — a `later` or `never` failure must not consume a grading
    retry any more than it should consume an audit one."""
    filled, failed, rate_limited, blocked, kinds = [], {}, {}, {}, {}
    grade_dir = state.project_dir(cfg, pid) / "audit" / "grade"
    withheld = ["severity", "severity_rationale", "lens", "other_findings",
               "prior_grades", "verdict_thresholds", "derivation_table"]
    for slug in awaiting:
        prompt = Path(prompts[slug])
        try:
            rec = run_candidate(cfg, pid, slug, prompt, grade_dir / f"{slug}.json",
                                withheld=withheld)
            filled.append(slug)
            detail = rec.get("tool_policy_detail") or {}
            state.append_log(cfg, pid, artifact_type="grade_auto", phase="grade",
                             headers={"slug": slug, "verdict": rec["verdict"],
                                      "seconds": rec["seconds"],
                                      "model_requested": detail.get("model_requested", ""),
                                      "model_reported": (rec.get("envelope") or {}).get(
                                          "model_reported", ""),
                                      "confinement_enforced": bool(detail.get("enforced")),
                                      "desiderata_passed": rec.get("desiderata_passed")},
                             path=str(grade_dir / f"{slug}.json"))
        except (GradeDriverError, RateLimited, NonRetryable) as e:
            kind = getattr(e, "kind", "unknown")
            retry = getattr(e, "retry", "now")
            kinds[slug] = {"kind": kind, "retry": retry,
                           "reset_hint": getattr(e, "reset_hint", "")}
            {"later": rate_limited, "never": blocked}.get(retry, failed)[slug] = str(e)
    return {"filled": filled, "failed": failed, "rate_limited": rate_limited,
            "blocked": blocked, "kinds": kinds}


if __name__ == "__main__":       # self-check: python -m harness.grade_driver
    import ast as _ast
    import inspect as _inspect
    import tempfile as _tf

    cfg = Config.load()
    closed = Config(allow_grading=False, grade_cmd="x {prompt} {out}")
    assert available(closed)[0] is False, "the gate must be closed by default"
    bad = Config(allow_grading=True, grade_cmd="review --in {prompt}")
    assert available(bad)[0] is False, "a template without {out} must be refused"

    # --- the grader's confinement, driven from an injected exe so it always runs -------
    _EXE = "/nonexistent/claude"
    _built = default_cmd(Config(), exe=_EXE, settings="/policy/s.json")
    assert "{prompt}" in _built and "{out}" in _built
    for _flag in ("--restricted", "--strict-mcp-config", "--settings", "--bare",
                  "--disallowedTools", "--model", "--output-format json"):
        assert _flag in _built, (_flag, _built)
    assert '--allowedTools ""' in _built, "the grader must be granted no tools at all"
    assert "--add-dir" not in _built, "the grader must have no filesystem access"
    _c = grade_confinement(Config())
    assert _c.allowed_tools == () and set(_c.disallowed_tools) == set(KNOWN_TOOLS)
    assert "Read" in _c.disallowed_tools, "an empty allow list denies nothing on its own"
    assert _c.bare is True, "the operator's own CLAUDE.md is a blinding leak, not noise"
    assert _c.model, "the grader must not inherit the operator's ambient model"
    assert _c.enforced is True
    # The operator's own command wins and is recorded as unenforced, never described.
    assert grade_confinement(Config(grade_cmd="x {prompt} {out}")).enforced is False
    # The role model is selectable, and never blank.
    assert role_model(Config(grade_model="haiku")) == "haiku"
    assert role_model(Config(grade_model="")) == GP.ROLE_SPEC["model"]
    assert "--model haiku" in default_cmd(Config(grade_model="haiku"), exe=_EXE)

    body = '```json\n{"verdict":"CONFIRMED","severity":"MAJOR","confidence":"HIGH"}\n```'
    g = parse_grade_json(body)
    assert g.verdict == "CONFIRMED" and g.severity == "MAJOR"
    for bad_text, expect in (("", "empty file"), ("no json", "no JSON object"),
                             ('{"nope":1}', "no 'verdict'")):
        try:
            parse_grade_json(bad_text)
            raise AssertionError(f"should have rejected {bad_text!r}")
        except GradeDriverError as e:
            assert expect in str(e), (bad_text, str(e))

    # --- the CLI envelope, and a grader that reports its own failure ------------------
    _inner = '{"verdict":"REFUTED","severity":"NONE","confidence":"HIGH"}'
    _env = json.dumps({"type": "result", "is_error": False, "result": _inner,
                       "session_id": "s-9", "model": "claude-x"})
    _g, _m = parse_grade_report(_env)
    assert _g.verdict == "REFUTED" and _m["envelope"]["model_reported"] == "claude-x"
    _err = json.dumps({"type": "result", "is_error": True, "session_id": "s-9",
                       "result": "You've hit your session limit · resets 3:20pm"})
    try:
        parse_grade_report(_err)
        raise AssertionError("an is_error envelope must raise")
    except RateLimited as e:
        assert "3:20pm" in str(e), str(e)

    # --- desiderata: a CLOSED vocabulary and a deterministic pass rule ----------------
    _clean, _meta = parse_grade_report(
        json.dumps({"verdict": "CONFIRMED", "desiderata_violations": []}))
    assert _meta["desiderata_passed"] is True and _meta["desiderata_violations"] == []
    _one = json.dumps({"verdict": "CONFIRMED",
                       "desiderata_violations": [GP.DESIDERATA[0]]})
    assert parse_grade_report(_one)[1]["desiderata_passed"] is False
    # A grader that invents a desideratum is writing its own rubric: the name is recorded,
    # excluded, and cannot be used to claim a pass by naming zero of the real ones.
    _made_up = json.dumps({"verdict": "CONFIRMED",
                           "desiderata_violations": ["EVERYTHING_IS_FINE"]})
    _mm = parse_grade_report(_made_up)[1]
    assert _mm["unknown_desiderata"] == ["EVERYTHING_IS_FINE"]
    assert _mm["desiderata_violations"] == [] and _mm["desiderata_passed"] is False
    # NO NEUTRAL DEFAULT. A grader that said nothing about the desiderata has not passed
    # the candidate, and there is no 0.5, no `True`, and no `[False] * n` to stand in.
    _silent = parse_grade_report(json.dumps({"verdict": "CONFIRMED"}))[1]
    assert _silent["desiderata_passed"] is None, _silent
    # A model-assigned number never survives into the sealed grade.
    _scored = parse_grade_report(json.dumps(
        {"verdict": "CONFIRMED", "weight": 8, "score": 0.72, "rubric_total": 9.5}))
    assert _scored[1]["unknown_keys_dropped"] == 3
    _sealed = json.dumps(_scored[0].model_dump())
    for _n in ("weight", "score", "rubric_total"):
        assert _n not in _sealed, _n

    with _tf.TemporaryDirectory() as td:
        prompt = Path(td) / "c-01.md"
        prompt.write_text("prompt", encoding="utf-8")
        silent = Config(allow_grading=True, grade_cmd="python -c \"pass\" {prompt} {out}")
        try:
            run_candidate(silent, "p", "c-01", prompt, Path(td) / "c-01.json", withheld=[])
            raise AssertionError("a command writing no output must raise")
        except GradeDriverError as e:
            assert "without writing" in str(e), str(e)

    # --- INVARIANT 11: nothing this module produces can raise a severity --------------
    # The desiderata are the one idea taken from an external rubric design, and they are
    # taken WITHOUT its scoring: they are recorded on the sidecar and consumed by nothing.
    # That `grading.derive` cannot receive them — its signature admits vocabulary strings
    # and booleans only — is asserted in `tests/test_delegation_path.py`, deliberately and
    # not here: importing the deciding module to check its signature would put it in this
    # module's scope, and the property this layer needs is that it is NOT reachable from
    # here at all. A cross-module signature assertion belongs outside both modules.
    _grade_src = _inspect.getsource(sys.modules[__name__])
    _tree = _ast.parse(_grade_src)
    _assigned = {t.id for n in _ast.walk(_tree)
                 if isinstance(n, _ast.Assign)
                 for t in n.targets if isinstance(t, _ast.Name)}
    for _decision in ("counted_severity", "finding_class", "binding_cap", "triage"):
        assert _decision not in _assigned, _decision

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_grading}, indent=2))
