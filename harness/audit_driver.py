"""Optional S2 autonomy: fill lens JSON by shelling out to a configured reviewer.

This exists because "run the whole pipeline without stopping" is a reasonable thing to
want, and the honest way to provide it is to make the delegation explicit rather than to
pretend S2 was never a judgement stage.

What this module does NOT do is invent an auditor. The harness has no model of its own —
no API key, no cloud SDK, by design — so autonomy here means running whatever command the
operator puts in `SH_AUDIT_CMD` and treating its output as a candidate lens report. Three
consequences follow, and all three are enforced below rather than documented and hoped for:

  1. The gate is off by default (`SH_ALLOW_AUTO_AUDIT`). Running an arbitrary configured
     command as part of `review` is a per-invocation decision, like the S3 exec gates.
  2. Every lens filled this way leaves `audit/<lens>.driver.json` recording the command,
     the exit code and the timestamp. A finding that reaches a report should always be
     traceable to who wrote it.
  3. Output is parsed and structurally validated here, and then re-verified downstream by
     `stages/audit.load_reports` exactly like a human-written file — quotes checked against
     the parsed PDF, cell citations checked against the cell. An auto-filled lens gets no
     benefit of the doubt that a hand-written one would not get.

If the command fails, writes nothing, or writes something that is not a lens report, that
lens stays pending. A failed auto-audit degrades to the normal manual pause; it never
writes a placeholder, because an empty findings list is indistinguishable from "this
paper is clean" once it reaches the report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import failures, state
from .artifacts import LensReport
from .config import Config
from .prompts import audit as P


class AuditDriverError(RuntimeError):
    """The configured command did not produce a usable lens report.

    Self-classifying. Every instance carries `kind` / `retry` / `reset_hint` from
    `harness.failures.classify` over its own message, so a raise site does not have to
    know or care which failure taxonomy bucket its text falls into — and so a failure
    mode nobody anticipated still arrives at the controller with a retry policy attached
    rather than as an undifferentiated string. See `harness/failures.py` for why the
    three policies (`now` / `later` / `never`) are not interchangeable.
    """

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = failures.classify(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


class RateLimited(AuditDriverError):
    """A refusal that a LATER attempt could satisfy and an immediate one cannot: the
    operator's own account over its usage limit, a 429, an overloaded or 5xx upstream.

    A subclass of `AuditDriverError` so every existing `except AuditDriverError` still
    catches it, and a distinct class so `fill()` can route it away from `failed` (a
    quality problem with the reviewer's output that a retry might well fix) for the
    controller to treat differently — a `later` failure must not consume a retry attempt.
    """


class NonRetryable(AuditDriverError):
    """A refusal that will fail identically forever: no reviewer installed, an
    unauthenticated account, a flag the CLI does not accept, an unreadable PDF.

    Kept apart from `RateLimited` because the operator's next action is the opposite one.
    Waiting fixes a rate limit and never fixes a missing `claude` on PATH — and spending
    three attempts to discover that hides the actual fix behind a retry log.
    """


def driver_error(message: str) -> AuditDriverError:
    """Build the RIGHT exception class for a failure message. One classification point.

    The defect this replaces: a hand-carved `_RATE_LIMIT_RE` at two call sites, covering
    exactly one of the eleven ways a delegated reviewer can fail. Everything else — a
    revoked credential, an unrecognised flag, an upstream outage — arrived as the generic
    class and burned the whole retry budget against a wall that would not move.
    """
    kind, retry, hint = failures.classify(message)
    cls = {"later": RateLimited, "never": NonRetryable}.get(retry, AuditDriverError)
    return cls(message, kind=kind, retry=retry, reset_hint=hint)


def _reset_hint(text: str) -> str:
    """Kept as a name because `grade_driver` imports it. See `failures.reset_hint`."""
    return failures.reset_hint(text)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the WHOLE process tree a `shell=True` command started, not just the shell.

    `run_lens` launches the reviewer inside its own process group (POSIX) or process
    group (Windows, via `CREATE_NEW_PROCESS_GROUP`) precisely so this can reach every
    descendant: `taskkill /T` walks the tree by PID on Windows, `killpg` signals every
    process sharing the session's process group ID on POSIX. `proc.kill()` alone reaches
    only the immediate shell — the actual reviewer process is its CHILD, not it.
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass                                  # already gone
    try:
        proc.kill()
    except OSError:
        pass


def default_cmd(lens: str | None = None, pdf_dir: str = "") -> str:
    """The built-in reviewer invocation, or '' when no reviewer can be found.

    The harness still has no model of its own and still holds no key. What it can do is
    notice that the Claude Code CLI is installed on this machine and hand it one lens
    prompt at a time. That is the same delegation `SH_AUDIT_CMD` always described, with
    the harness supplying the template instead of the operator — which is the difference
    between an orchestration loop that exists in code and one that exists in prose.

    One process per lens is not incidental. It is stronger isolation than the convention
    it replaces: four lenses read by one session share a context and can echo each other,
    whereas four subprocesses cannot. `stages/audit.py` names that weakness explicitly;
    this closes it. `--allowedTools`, restricted to exactly what `harness.prompts.audit`
    grants each lens (`Read`, plus `WebSearch` for `overclaim` alone), makes that
    isolation apply to the FILESYSTEM too — without it a lens can read its three
    siblings' `audit/<lens>.json` files and the harness's own source, which is a real
    hole today, not a hypothetical one. `--add-dir` grants read access to the paper's
    own directory ONLY, so `SOURCE_FIDELITY` in the prompt (open the PDF to settle an
    extraction ambiguity) is possible without opening the rest of the filesystem.

    Returns '' when the CLI is absent, so `available()` refuses with a reason and the
    pipeline falls back to the normal manual pause rather than inventing a reviewer.
    """
    exe = shutil.which("claude")
    if not exe:
        return ""
    # `-p` is one-shot and non-interactive. The prompt is PIPED rather than interpolated
    # into the command line: a lens prompt carries the paper's own text, which is
    # untrusted data that may contain quotes, backticks or anything else, and a shell
    # that expands it is a shell that can be made to run it.
    #
    # `run_lens` uses shell=True, so the reader differs by platform — cmd.exe has no
    # `cat` and does not understand POSIX substitution. Getting this wrong does not fail
    # loudly; it produces an empty output file, which `run_lens` reports as "the command
    # exited without writing", so the pipeline degrades to the manual pause and the cause
    # is invisible.
    reader = "type" if os.name == "nt" else "cat"
    # ponytail: no runtime `claude --help` preflight for the `--allowedTools` flag —
    # verified present on the installed CLI at design time (`claude --help` lists
    # `--allowedTools`, `--add-dir`). If a future CLI ever drops it, every lens fails
    # identically with "exited without writing" (see `run_lens`), which is loud, not
    # silent — an acceptable ceiling for one flag rather than a subprocess call on every
    # single lens invocation to re-verify something that does not change between them.
    tools = ",".join(P.LENSES.get(lens, {}).get("tools", ["Read"])) if lens else "Read"
    extra = f' --allowedTools "{tools}"' if tools else ""
    if pdf_dir:
        extra += f' --add-dir "{pdf_dir}"'
    return f'{reader} "{{prompt}}" | "{exe}" -p --output-format text{extra} > "{{out}}"'


def resolve_cmd(cfg: Config, lens: str | None = None, pdf_dir: str = "") -> str:
    """The command that would run: the operator's if set, otherwise the built-in one.

    An operator-supplied `SH_AUDIT_CMD` wins unconditionally and is NOT restricted by
    this module — `run_lens` records which happened (`tool_policy` on the `.driver.json`
    sidecar) rather than silently pretending a guarantee it cannot enforce over an
    arbitrary command line.
    """
    return cfg.audit_cmd.strip() or default_cmd(lens, pdf_dir)


def available(cfg: Config) -> tuple[bool, str]:
    """Whether auto-audit can run, and if not, the reason to show the operator."""
    if not cfg.allow_auto_audit:
        return False, ("auto-audit gate is closed; set SH_ALLOW_AUTO_AUDIT=1 (or pass "
                       "--auto-audit) to let the controller fill lenses by running a reviewer")
    cmd = resolve_cmd(cfg)
    if not cmd:
        return False, ("no reviewer is available: SH_AUDIT_CMD is empty and the `claude` CLI "
                       "is not on PATH, so there is nothing to delegate the audit to")
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, ("SH_AUDIT_CMD must contain both {prompt} and {out} placeholders; "
                       f"got: {cfg.audit_cmd!r}")
    return True, ""


def parse_lens_json(text: str, lens: str) -> LensReport:
    """A LensReport from the command's output file, or AuditDriverError explaining why not.

    Tolerant of one thing only: a JSON object wrapped in prose or a fenced code block,
    because a chat-shaped reviewer will often produce that. Not tolerant of a missing
    `findings` key — a report with no findings list is not a report that found nothing,
    it is a report that did not happen.
    """
    raw = (text or "").strip()
    if not raw:
        raise AuditDriverError("the command wrote an empty file")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AuditDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AuditDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AuditDriverError("output JSON is not an object")
    if "findings" not in data:
        raise AuditDriverError("output JSON has no 'findings' key")
    if not isinstance(data["findings"], list):
        raise AuditDriverError("'findings' is not a list")

    # `Finding.evidence_ref` defaults to "", so a finding that arrives without one is
    # accepted by the model and then written back out with an empty string — which reads
    # downstream as "a reference was recorded and it is blank" rather than "the reference
    # is missing". Catching it here keeps this module from laundering an unlocatable
    # finding into a well-formed lens file. Refusing the whole lens rather than dropping
    # the finding is deliberate: a reviewer that omits references is malfunctioning, and
    # silently keeping its other findings would hide that.
    for i, f in enumerate(data["findings"]):
        if not isinstance(f, dict):
            raise AuditDriverError(f"findings[{i}] is not an object")
        if (f.get("evidence_quote") or "").strip() and not (f.get("evidence_ref") or "").strip():
            raise AuditDriverError(
                f"findings[{i}] ({f.get('finding_id') or 'unnamed'}) carries an "
                f"evidence_quote but no evidence_ref; a quote with no location cannot be "
                f"verified and must not be persisted")

    data["lens"] = lens
    try:
        return LensReport(**data)
    except Exception as e:                       # pydantic validation, shape errors
        raise AuditDriverError(f"output does not match the lens schema: {e}") from e


def run_lens(cfg: Config, pid: str, lens: str, prompt: Path, out: Path, *,
            pdf_dir: str = "") -> dict:
    """Run the configured reviewer for one lens. Returns a record; raises on failure.

    The reviewer writes to a STAGING path, never to `audit/<lens>.json`, and its output
    is promoted to the lens path only after it validates. Redirecting the command
    straight at the lens path was a real defect with a silent and severe failure: when
    the reviewer emitted something that was not a lens report — a rate-limit notice, an
    error page, a partial response — the shell had already created the file before this
    module could reject it. `run_audit` then saw four files and reported the panel
    complete, `load_reports` could not parse them and returned four EMPTY reports, and a
    paper that had never been audited came out GREEN with "4 lenses run, 0 findings".

    The staged output of a rejected run is kept at `<lens>.rejected.txt` — diagnosable,
    and not a filename anything downstream mistakes for a result.

    Runs with `cwd` an EMPTY scratch directory, deleted after — a lens started in the
    repo root (the old behaviour) can `Read` its three sibling `audit/<lens>.json`
    files and this harness's own source, which contradicts the "sealed session" the
    module docstring in `harness/prompts/audit.py` already claims. Combined with
    `--allowedTools` (see `default_cmd`), a lens now genuinely sees only what this
    prompt gives it plus, through `--add-dir`, the one PDF `pdf_dir` names.
    """
    ok, why = available(cfg)
    if not ok:
        raise AuditDriverError(why)
    if not prompt.exists():
        raise AuditDriverError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()                             # never let a stale file look like success
    staged = out.with_suffix(".staged")
    rejected = out.with_name(f"{lens}.rejected.txt")
    for p_ in (staged, rejected):
        p_.unlink(missing_ok=True)

    cmd = resolve_cmd(cfg, lens, pdf_dir).replace("{prompt}", str(prompt)).replace("{out}", str(staged))
    tool_policy = "operator_supplied" if cfg.audit_cmd.strip() else "restricted"
    started = time.time()
    # Started in its own process group/session so a timeout can kill the WHOLE tree, not
    # just the immediate shell. `shell=True` on either platform launches a shell that is
    # itself the parent of the real work — `cmd.exe` for a pipeline, `sh -c` for one — and
    # `Popen.kill()` alone only terminates that shell. The reviewer it launched keeps
    # running as an orphan: it can still be writing to `staged` after this function has
    # already declared the attempt timed out and unlinked that same path, and it can
    # still be running when a retry starts a second reviewer over the same prompt.
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True})
    sandbox = Path(tempfile.mkdtemp(prefix=f"sh-lens-{lens}-"))
    try:
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", **group_kwargs)
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise AuditDriverError(f"could not start the command: {e}") from e
        try:
            stdout, stderr = proc.communicate(timeout=cfg.audit_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()                   # reap the process now that it is dead
            staged.unlink(missing_ok=True)
            raise AuditDriverError(f"timed out after {cfg.audit_timeout_s}s") from None
        p = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

        if not staged.exists():
            # Classify the reviewer's OWN words first: a rate limit, a revoked
            # credential and a flag the CLI does not accept all arrive here as the same
            # non-zero exit, and they have three different right responses.
            tail = (p.stderr or p.stdout or "").strip()[-300:]
            err = driver_error(tail or f"the command exited {p.returncode} with no output")
            if err.kind != "unknown":
                raise err
            raise AuditDriverError(
                f"the command exited {p.returncode} without writing {out.name}"
                + (f" — {tail}" if tail else ""))

        raw = staged.read_text(encoding="utf-8")
        try:
            report = parse_lens_json(raw, lens)
        except AuditDriverError:
            classified = driver_error(raw.strip()[:300])
            if classified.retry in ("later", "never"):
                # The reviewer's entire response WAS the refusal notice — `raw` is short
                # plain prose, not a lens report that merely failed to parse. Retrying a
                # parse failure can work; retrying this cannot.
                staged.replace(rejected)
                raise classified from None
            # Move the unusable output somewhere nothing reads as a lens result, and
            # keep it, because "the reviewer said something and it was not a report" is
            # worth seeing.
            staged.replace(rejected)
            raise
        staged.unlink(missing_ok=True)
        # Written normalised, and only now: downstream reads this file, so what is on
        # disk is exactly what was validated rather than whatever prose the command
        # wrapped it in.
        state.write_json(out, report.model_dump())

        record = {
            "lens": lens, "paper_id": pid, "command": cmd, "returncode": p.returncode,
            "seconds": round(time.time() - started, 1), "findings": len(report.findings),
            "written_by": "audit_driver", "tool_policy": tool_policy,
            # The seal `stages.audit.lens_is_accepted` checks against the lens file's
            # CURRENT bytes — without this, every lens this driver produces fails its
            # own provenance check the moment `lens_is_accepted` starts requiring it.
            "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state.write_json(out.with_suffix(".driver.json"), record)
        return record
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def fill(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str],
        pdf_dir: str = "") -> dict:
    """Attempt every pending lens. Partial success is success for the lenses that worked.

    THE BUCKETS ARE THE RETRY POLICIES, not the exception classes. `rate_limited` holds
    every `later` failure (an account limit, a 429, an overloaded upstream), `blocked`
    every `never` one (no CLI installed, unauthenticated, a flag the CLI rejects, an
    unreadable PDF), and `failed` only what a second attempt could plausibly fix. The
    controller spends its bounded retry budget on `failed` alone — see
    `harness/failures.py` for why treating all three alike stranded a paper permanently.

    `kinds` carries the classification per lens so the reason survives into the case
    state and the report, rather than only into a log line.
    """
    filled, failed, rate_limited, blocked, kinds = [], {}, {}, {}, {}
    audit_dir = state.project_dir(cfg, pid) / "audit"
    for lens in awaiting:
        prompt = Path(prompts[lens])
        try:
            rec = run_lens(cfg, pid, lens, prompt, audit_dir / f"{lens}.json", pdf_dir=pdf_dir)
            filled.append(lens)
            state.append_log(cfg, pid, artifact_type="audit_auto", phase="audit",
                             headers={"lens": lens, "findings": rec["findings"],
                                      "seconds": rec["seconds"]},
                             path=str(audit_dir / f"{lens}.json"))
        except AuditDriverError as e:
            kinds[lens] = {"kind": e.kind, "retry": e.retry, "reset_hint": e.reset_hint}
            {"later": rate_limited, "never": blocked}.get(e.retry, failed)[lens] = str(e)
    return {"filled": filled, "failed": failed, "rate_limited": rate_limited,
            "blocked": blocked, "kinds": kinds}


if __name__ == "__main__":       # self-check: python -m harness.audit_driver
    cfg = Config.load()

    # --- gate semantics ---------------------------------------------------------------
    closed = Config(allow_auto_audit=False, audit_cmd="x {prompt} {out}")
    assert available(closed)[0] is False, "the gate must be closed by default"
    open_gate = Config(allow_auto_audit=True, audit_cmd="")
    if default_cmd():
        # A reviewer was discovered on this machine, so an empty SH_AUDIT_CMD is no
        # longer a dead end — the harness supplies the invocation itself. This is what
        # makes the orchestration loop closeable in code.
        assert available(open_gate)[0] is True, available(open_gate)[1]
        assert "{prompt}" in default_cmd() and "{out}" in default_cmd()
        assert "claude" in default_cmd().lower()
    else:
        assert available(open_gate)[0] is False
        assert "not on PATH" in available(open_gate)[1]
    # The operator's command always wins over the built-in one.
    mine = Config(allow_auto_audit=True, audit_cmd="mytool {prompt} {out}")
    assert resolve_cmd(mine) == "mytool {prompt} {out}"
    bad = Config(allow_auto_audit=True, audit_cmd="review --in {prompt}")
    assert available(bad)[0] is False, "a template without {out} must be refused"
    good = Config(allow_auto_audit=True, audit_cmd="cp {prompt} {out}")
    assert available(good) == (True, "")

    # --- per-lens tool restriction ------------------------------------------------------
    if default_cmd():
        assert "WebSearch" in default_cmd("overclaim"), "overclaim alone gets WebSearch"
        assert "WebSearch" not in default_cmd("protocol")
        assert '--allowedTools "Read"' in default_cmd("protocol")
        assert "--add-dir" not in default_cmd("protocol"), "no pdf_dir given, no flag"
        assert "--add-dir" in default_cmd("protocol", pdf_dir="C:/papers")

    # --- parsing ----------------------------------------------------------------------
    body = ('Here you go:\n```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
            '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
            '"evidence_ref":"p1"}],"unasked_question":"u","notes":"n"}\n```')
    rep = parse_lens_json(body, "overclaim")
    assert rep.lens == "overclaim" and len(rep.findings) == 1
    assert rep.findings[0].severity == "MAJOR"

    for bad_text, expect in (("", "empty file"), ("no json here", "no JSON object"),
                             ("{nope}", "not valid JSON"), ('{"lens":"x"}', "no 'findings' key"),
                             ('{"findings":{}}', "not a list")):
        try:
            parse_lens_json(bad_text, "overclaim")
            raise AssertionError(f"should have rejected {bad_text!r}")
        except AuditDriverError as e:
            assert expect in str(e), (bad_text, str(e))

    # --- a command that writes nothing must fail, not produce an empty lens ------------
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("prompt", encoding="utf-8")
        silent = Config(projects_dir=Path(td), allow_auto_audit=True,
                        audit_cmd="python -c \"pass\" {prompt} {out}")
        try:
            run_lens(silent, "p", "overclaim", prompt, Path(td) / "overclaim.json")
            raise AssertionError("a command writing no output must raise")
        except AuditDriverError as e:
            assert "without writing" in str(e), str(e)

    # --- an account rate limit is distinguished from an ordinary malformed response ----
    # This is the confirmed `sanchez24a-icml` failure: the reviewer's ENTIRE response is
    # "You've hit your session limit · resets 3:20pm (Asia/Kolkata)", printed to stdout,
    # with no output file written. The old code raised a plain `AuditDriverError`
    # identical in kind to a truncated JSON object, so the controller retried it three
    # times in nine seconds against a limit that cannot possibly clear that fast.
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("prompt", encoding="utf-8")
        limiter = Path(td) / "limiter.py"
        limiter.write_text(
            "import sys\n"
            "sys.stdout.write(\"You've hit your session limit \\u00b7 resets 3:20pm "
            "(Asia/Kolkata)\")\n",
            encoding="utf-8",
        )
        limited = Config(projects_dir=Path(td), allow_auto_audit=True,
                         audit_cmd=f'"{sys.executable}" "{limiter}" {{prompt}} {{out}}')
        try:
            run_lens(limited, "p", "overclaim", prompt, Path(td) / "overclaim.json")
            raise AssertionError("a rate-limit response must raise")
        except RateLimited as e:
            assert "3:20pm" in str(e), str(e)
        except AuditDriverError as e:
            raise AssertionError(f"raised the generic class, not RateLimited: {e}") from e

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_auto_audit}, indent=2))
