"""How strongly a backend confines third-party code, and what that entitles it to run.

`python -m harness.isolation` runs the self-check.

**The defect this exists to close.** `authorize()` had nine conditions and none of them
asked WHERE the code would run. With `SH_ALLOW_REPO_EXEC=1` the local backend executed a
paper's repository as `subprocess.run(argv, cwd=checkout)` under the operator's own user:
their filesystem, their network, their credentials, their PATH. A venv and a timeout are
not a boundary — they scope imports and wall time, and confine nothing else. Every other
gate in this harness asks whether a conclusion is earned; none of them asked whether
running the program was safe, and the answer was that it was not.

**The rule.** Repository execution requires CONTAINER or REMOTE_SESSION. It is a
TIGHTENING of `authorize()`, never a widening: a backend that could execute before and
whose isolation is insufficient now refuses, and nothing that was refused before is now
permitted. There is no environment variable that lowers the requirement, for the same
reason `harness.provenance` has none — a boundary an operator can step over is not a
boundary.

**Why this is an abstraction and not a container runtime.** The development host has no
Docker, no Podman, no WSL and no virtualization, and adding one as a requirement would
make the harness unrunnable where it was developed. So the levels are declared by each
backend and matched here, and the first backend entitled to run a repository is the
remote sandbox that already exists (`harness/sandbox.py`, REMOTE_SESSION). A local
container backend can be added later by declaring CONTAINER; nothing else changes.

**What a declaration is worth.** Exactly what the backend can back up. `LocalBackend`
declares VENV because that is what it builds; declaring CONTAINER there would be a lie
this module could not detect, which is why `tests/test_isolation_boundary.py` asserts the
level of every registered backend rather than trusting the field.
"""
from __future__ import annotations

# Ordered weakest first. The order is meaningful: `at_least` compares by index, so a
# requirement expressed as a floor stays correct when a level is added in the middle.
ISOLATION_LEVELS: tuple[str, ...] = (
    "NONE",            # the harness's own process, or a bare subprocess with no scoping
    "VENV",            # a dedicated interpreter and dependency set; the host is still open
    "CONTAINER",       # a local container: own filesystem namespace, own network stance
    "REMOTE_SESSION",  # a leased machine that is not the operator's, torn down after use
)

# What running the AUTHORS' OWN CODE requires. `repo_exec` is the one provenance that
# executes a program this harness did not write and the operator did not read, so it is
# the one that needs a boundary rather than a scope.
#
# Deliberately a tuple of names rather than a floor: "at least CONTAINER" and "CONTAINER
# or REMOTE_SESSION" are the same set today and would diverge the moment a level is added
# between them, and the set is what the rule means.
ISOLATION_SUFFICIENT_FOR_REPO_EXEC: tuple[str, ...] = ("CONTAINER", "REMOTE_SESSION")

# The reader-facing sentence per level, used by the refusal in `backends.authorize` and by
# the report. A level with no sentence cannot be added, which is the point of the table.
DESCRIPTION = {
    "NONE": "runs in this process's own environment with no confinement at all",
    "VENV": "runs on this host under a dedicated interpreter; the operator's filesystem, "
            "network and credentials are reachable by anything it starts",
    "CONTAINER": "runs in a local container with its own filesystem namespace",
    "REMOTE_SESSION": "runs on a leased machine that is not the operator's and is torn "
                      "down afterwards",
}


def known(level: str = "") -> bool:
    """Is this a level this module recognises? Exact, and unrecognised means unknown.

    No stripping and no case folding, for the reason `harness.provenance.admits` gives:
    the value arrives from a backend declaration and, downstream, from a JSON artifact,
    and a predicate that repairs its input is a predicate that can be talked around.
    """
    return (level or "") in ISOLATION_LEVELS


def sufficient_for_repo_exec(level: str = "") -> bool:
    """May the authors' own code run at this isolation level?

    Fails closed on anything unrecognised: a backend that declares a level this module
    does not know is refused, not trusted. That is the direction a boundary has to fail.
    """
    return (level or "") in ISOLATION_SUFFICIENT_FOR_REPO_EXEC


def at_least(level: str, floor: str) -> bool:
    """Is `level` at or above `floor` in the ordering? Unrecognised inputs are below all."""
    if not (known(level) and known(floor)):
        return False
    return ISOLATION_LEVELS.index(level) >= ISOLATION_LEVELS.index(floor)


def describe(level: str = "") -> str:
    """One sentence naming what this level does and does not confine."""
    return DESCRIPTION.get(level or "", "declares an isolation level this harness does not "
                                        "recognise, so it is treated as unconfined")


def refusal_detail(backend_name: str, level: str) -> str:
    """The sentence `authorize()` returns when isolation is insufficient.

    Written here rather than in `backends` so the requirement and its explanation cannot
    drift apart, and so the sentence names the remedy: which backends DO qualify.
    """
    return (
        f"'{backend_name}' {describe(level)}. Running a paper's own repository requires "
        f"{' or '.join(ISOLATION_SUFFICIENT_FOR_REPO_EXEC)}; this is a property of the "
        f"backend, not a gate, and no configuration lowers it. Nothing about the paper "
        f"follows from this refusal."
    )


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect
    import sys

    assert ISOLATION_LEVELS == ("NONE", "VENV", "CONTAINER", "REMOTE_SESSION")
    assert set(DESCRIPTION) == set(ISOLATION_LEVELS), "every level needs a sentence"
    assert set(ISOLATION_SUFFICIENT_FOR_REPO_EXEC) <= set(ISOLATION_LEVELS)

    # --- the rule, over the whole level space -----------------------------------------
    for lvl in ISOLATION_LEVELS:
        assert sufficient_for_repo_exec(lvl) == (lvl in ("CONTAINER", "REMOTE_SESSION")), lvl
    assert not sufficient_for_repo_exec("VENV"), (
        "a venv scopes imports and confines nothing; it must never host the authors' code")
    assert not sufficient_for_repo_exec("NONE")

    # --- fails closed, exactly like the provenance ceiling ----------------------------
    for bad in ("", "  ", None, "container", "CONTAINER ", " REMOTE_SESSION", "docker",
                "Container", "sandbox", "venv"):
        assert not sufficient_for_repo_exec(bad or ""), bad
        assert not known(bad or ""), bad
        assert "does not recognise" in describe(bad or ""), bad

    # --- no configuration widens it ---------------------------------------------------
    # The MODULE BODY only, cut at this function: the needles below occur in this
    # assertion's own text, so scanning the whole file would always find them.
    src = inspect.getsource(sys.modules[__name__]).split("def _self_check")[0]
    for needle in ("os." + "environ", "get" + "env", "Config"):
        assert needle not in src, (
            f"isolation must not be readable from configuration ({needle}); a boundary an "
            f"operator can lower is not a boundary")
    assert set(inspect.signature(sufficient_for_repo_exec).parameters) == {"level"}

    # --- ordering ---------------------------------------------------------------------
    assert at_least("REMOTE_SESSION", "CONTAINER") and at_least("CONTAINER", "CONTAINER")
    assert not at_least("VENV", "CONTAINER") and not at_least("NONE", "VENV")
    assert not at_least("nonsense", "NONE") and not at_least("NONE", "nonsense")
    # every sufficient level is at or above CONTAINER, so the set and the floor agree today
    for lvl in ISOLATION_SUFFICIENT_FOR_REPO_EXEC:
        assert at_least(lvl, "CONTAINER"), lvl

    # --- the refusal names the remedy -------------------------------------------------
    why = refusal_detail("local", "VENV")
    assert "CONTAINER or REMOTE_SESSION" in why
    assert "Nothing about the paper follows" in why
    print("harness.isolation self-check ok")


if __name__ == "__main__":
    _self_check()
