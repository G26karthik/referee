"""An OPTIONAL, GATED `--help` invocation, confirming a static argparse surface for real.

`python -m harness.alignment.trial` runs the self-check.

**Why this is not just another read of the source.** Every other module in this package
is static: it reads text and never runs it. A script can still be WRONG about itself — a
flag `argparse_surface` reads from the source may be dead code behind a version check, or
the file may simply fail to import. The only way to know whether a declared flag is real
is to ask the program, and asking it means running it — so this module exists apart from
the rest of the package, carries its own gate, and is the only place in `alignment` that
executes third-party code.

**The same isolation bar as the real experiment, for the same reason.** A `--help`
invocation still executes the top of a file — module-level imports, decorators, anything
above `if __name__ == "__main__":` — that this harness did not write. `may_trial` requires
`harness.isolation.sufficient_for_repo_exec`, exactly as `backends.authorize` requires for
`AUTHOR_CODE_EXECUTION`, and requires it independently: this module never calls
`authorize()` and `authorize()` never calls this module. Two callers asking the same
question about the same backend is not a duplicated check, it is two different actions —
running the experiment, and confirming a surface — each needing its own permission.

**Optional in the literal sense.** Every other identity decision in this harness works
without this module ever running: `argparse_surface` and `alignment.configuration` narrow
`ambiguous` down using only static evidence, and this module is consulted, if at all,
after that narrowing has already happened — to CONFIRM the survivor, never to produce one.
A caller that never sets `SH_ALLOW_ALIGNMENT_TRIAL` gets identical identity resolution to
one that does; this module can only ADD a confirmation record, never change a state
`experiment_id.identities_established` reads.
"""
from __future__ import annotations

from .. import isolation as isolation_mod
from ..artifacts import ArgSpec, CandidateCommand, TrialResult
from ..backends import ExecOutcome, ExecRequest, ExecutionBackend
from ..config import Config

_TIMEOUT_S = 20


def may_trial(cfg: Config, backend: ExecutionBackend) -> tuple[bool, str]:
    """May a trial invocation start at all?

    Two independent conditions, both required: the operator granted the gate, and this
    backend confines code it did not write strongly enough. Neither is `authorize()`'s to
    decide and neither is decided there — this is a separate, narrower permission for a
    cheaper action.
    """
    if not cfg.allow_alignment_trial:
        return False, ("the alignment-trial gate is shut (SH_ALLOW_ALIGNMENT_TRIAL=0); a "
                       "declared argparse surface is reported as read from source, "
                       "unconfirmed against the real program")
    level = backend.profile().isolation
    if not isolation_mod.sufficient_for_repo_exec(level):
        return False, (
            f"'{backend.name}' {isolation_mod.describe(level)}. Confirming an argparse "
            f"surface still executes the top of a file this harness did not write, so it "
            f"requires the same isolation repository execution does; nothing about the "
            f"paper follows from this refusal.")
    return True, ""


def _help_argv(cmd: CandidateCommand, interpreter: str) -> list[str]:
    """The command's own argv, with its target replaced by the interpreter this backend
    would actually use, and `--help` appended. Never invents a script the command did not
    already name."""
    argv = list(cmd.argv)
    if not argv:
        return []
    if interpreter and argv[0] in ("python", "python3"):
        argv[0] = interpreter
    return argv + ["--help"]


def run_trial(cfg: Config, backend: ExecutionBackend, cwd: str, interpreter: str,
             cmd: CandidateCommand, declared: list[ArgSpec]) -> TrialResult:
    """Run `cmd`'s own invocation with `--help` appended, and check which declared flags
    actually appear in its output.

    `attempted=False` and nothing else populated when `may_trial` refuses — the refusal
    reason is carried in `.reason` and NOTHING was executed. A process that starts but
    exits non-zero, or times out, still counts as `ran=True`: `--help` is expected to
    print and exit regardless of whether every flag is confirmed, and a non-zero exit
    from `--help` is itself worth reporting rather than treating as a null result.
    """
    ok, why = may_trial(cfg, backend)
    if not ok:
        return TrialResult(attempted=False, reason=why, backend=backend.name)

    argv = _help_argv(cmd, interpreter)
    if not argv:
        return TrialResult(attempted=False,
                           reason="the candidate has no argv to append --help to",
                           backend=backend.name)

    outcome = backend.execute(ExecRequest(argv=argv, cwd=cwd, timeout_s=_TIMEOUT_S,
                                          label="alignment-trial"))
    if not outcome.launched:
        return TrialResult(attempted=True, ran=False, argv=argv,
                           reason=outcome.error or "the trial process never started",
                           backend=outcome.backend or backend.name)

    text = outcome.stdout + "\n" + outcome.stderr
    confirmed = [s.flag for s in declared if s.flag in text]
    unconfirmed = [s.flag for s in declared if s.flag not in confirmed]
    return TrialResult(
        attempted=True, ran=outcome.completed, argv=outcome.argv or argv,
        returncode=outcome.returncode, stdout_tail=text[-800:],
        confirmed_flags=confirmed, unconfirmed_flags=unconfirmed,
        backend=outcome.backend or backend.name,
        reason="" if outcome.completed else "the trial process did not complete before the "
                                            "timeout; nothing about its flags was confirmed")


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    from ..backends import BackendResources, ExecCapability

    class _StubBackend(ExecutionBackend):
        """A backend that never launches a real process — `execute` returns a fixture,
        the same shape `LinuxStubBackend` in `tests/test_execution_backend.py` uses to
        exercise a seam without this module gaining a way to run anything for real."""

        name = "trial-stub"
        isolation = "NONE"

        def resources(self) -> BackendResources:
            return BackendResources(name=self.name, platform="linux", python="/usr/bin/python3")

        def capability(self, acq, interpreter, harness_python, flag="seed") -> ExecCapability:
            return ExecCapability(established=False, reason_code="not_attempted")

        def provision(self, cfg, root, pid, acq):
            return acq

        def execute(self, req: ExecRequest) -> ExecOutcome:
            return ExecOutcome(launched=True, completed=True, returncode=0,
                              stdout="usage: eval.py [-h] [--sparsity SPARSITY]\n",
                              argv=req.argv, backend=self.name)

        def cleanup(self, root, pid) -> list[str]:
            return []

    class _RefusingStubBackend(_StubBackend):
        name = "refusing-stub"

        def execute(self, req: ExecRequest) -> ExecOutcome:
            return ExecOutcome(launched=False, completed=False, error="not found",
                              backend=self.name)

    unconfined = _StubBackend()
    confined = _StubBackend()
    confined.isolation = "REMOTE_SESSION"

    def _cfg(allow: bool) -> Config:
        c = Config.load()
        c.allow_alignment_trial = allow
        return c

    # --- the gate ------------------------------------------------------------------------
    ok, why = may_trial(_cfg(False), confined)
    assert not ok and "SH_ALLOW_ALIGNMENT_TRIAL" in why

    # --- the isolation floor, independent of the gate -------------------------------------
    ok, why = may_trial(_cfg(True), unconfined)
    assert not ok and "requires the same isolation" in why

    ok, why = may_trial(_cfg(True), confined)
    assert ok and why == ""

    # --- an unconfined backend NEVER runs, whatever the gate says ------------------------
    cmd = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
    declared = [ArgSpec(flag="--sparsity"), ArgSpec(flag="--seed")]
    r = run_trial(_cfg(True), unconfined, cwd=".", interpreter="python", cmd=cmd,
                 declared=declared)
    assert not r.attempted, "isolation insufficient must refuse even with the gate open"

    r = run_trial(_cfg(False), confined, cwd=".", interpreter="python", cmd=cmd,
                 declared=declared)
    assert not r.attempted, "the gate shut must refuse even on a confined backend"

    # --- a confined backend with the gate open actually runs, and confirms what it can ---
    r = run_trial(_cfg(True), confined, cwd=".", interpreter="/env/bin/python", cmd=cmd,
                 declared=declared)
    assert r.attempted and r.ran and r.returncode == 0
    assert r.confirmed_flags == ["--sparsity"], "only the flag --help actually printed"
    assert r.unconfirmed_flags == ["--seed"]
    assert r.argv[0] == "/env/bin/python", "the backend's own interpreter is used"
    assert r.argv[-1] == "--help"

    # --- a candidate with no argv at all refuses honestly, never invents one ------------
    empty_cmd = CandidateCommand(argv=[], source_ref="x:1")
    r = run_trial(_cfg(True), confined, cwd=".", interpreter="python", cmd=empty_cmd,
                 declared=[])
    assert not r.attempted and "no argv" in r.reason

    # --- a process that never launches is distinguished from one that ran and confirmed -
    refusing = _RefusingStubBackend()
    refusing.isolation = "REMOTE_SESSION"
    r = run_trial(_cfg(True), refusing, cwd=".", interpreter="python", cmd=cmd,
                 declared=declared)
    assert r.attempted and not r.ran and r.reason == "not found"
    print("harness.alignment.trial self-check ok")


if __name__ == "__main__":
    _self_check()
