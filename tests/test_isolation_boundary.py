"""The isolation boundary: WHERE a paper's own code may run, and where it may not.

`authorize()` had nine conditions and none of them asked where the process would land.
With `SH_ALLOW_REPO_EXEC=1` the local backend ran a third-party repository as
`subprocess.run(argv, cwd=checkout)` under the operator's own user, with their
filesystem, network, environment and credentials in reach. A venv scopes imports and
wall time; it confines nothing else.

These tests pin the tenth condition and the two properties that make it a boundary
rather than a preference:

  * it is a TIGHTENING — nothing previously refused becomes permitted, and the refusal
    is ordered before every scientific condition, because "is running a stranger's code
    here safe" is prior to "is it the right code";
  * NO CONFIGURATION LOWERS IT — `harness.isolation` reads no environment and no Config,
    so there is no `SH_*` that turns a laptop into a sandbox.

They also pin the declared level of every registered backend, rather than trusting the
field: a backend that declared CONTAINER while running bare subprocesses would satisfy
the gate and confine nothing, and no runtime probe can catch that.
"""
from __future__ import annotations

import inspect

import pytest

from harness import backends, isolation
from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec,
                               ResourceCapability)
from harness.config import Config


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def open_cfg() -> Config:
    cfg = Config.load()
    cfg.allow_repo_exec = True
    return cfg


def _backend_at(level: str) -> backends.ExecutionBackend:
    """The local backend with one field changed, so only isolation varies."""

    class _At(backends.LocalBackend):
        name = "local"
        isolation = level

    return _At(Config.load())


def _fully_qualified_spec() -> ProbeSpec:
    """A spec that satisfies every condition BELOW isolation, so isolation is what decides."""
    est = dict(state="established", established=True, reason="fixture")
    return ProbeSpec(
        paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
        experiment=ExperimentIdentity(**est), metric_identity=MetricIdentity(**est),
        configuration=ConfigurationIdentity(**est),
        capability=ExecCapability(established=True, reason_code="established"),
        resources=ResourceCapability(state="satisfied", reason="fixture"),
    )


VERIFIED = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)


# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #
def test_every_level_has_a_sentence_and_the_sufficient_set_is_a_subset():
    assert set(isolation.DESCRIPTION) == set(isolation.ISOLATION_LEVELS)
    assert set(isolation.ISOLATION_SUFFICIENT_FOR_REPO_EXEC) <= set(isolation.ISOLATION_LEVELS)


@pytest.mark.parametrize("level", isolation.ISOLATION_LEVELS)
def test_only_container_and_remote_session_may_host_the_authors_code(level):
    """Swept over the whole level space, not spot-checked."""
    assert isolation.sufficient_for_repo_exec(level) is (
        level in ("CONTAINER", "REMOTE_SESSION")), level


@pytest.mark.parametrize("bad", ["", "  ", "container", "CONTAINER ", " REMOTE_SESSION",
                                 "docker", "Container", "venv", "sandbox", "None"])
def test_an_unrecognised_level_fails_closed(bad):
    """A boundary that repairs its input is a boundary that can be talked around."""
    assert not isolation.sufficient_for_repo_exec(bad)
    assert not isolation.known(bad)


def test_isolation_reads_no_configuration():
    """No `SH_*` may lower the requirement. Asserted over the module body itself."""
    body = inspect.getsource(isolation).split("def _self_check")[0]
    # The DOCSTRING legitimately names `SH_ALLOW_REPO_EXEC` when explaining what the
    # boundary is for, so the scan is over executable lines only.
    lines = [ln for ln in body.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    code = chr(10).join(lines)
    code = code.split('"""')[0] + "".join(code.split('"""')[2:])
    for needle in ("os." + "environ", "get" + "env", "Config("):
        assert needle not in code, needle


# --------------------------------------------------------------------------- #
# What each registered backend declares
# --------------------------------------------------------------------------- #
def test_every_registered_backend_declares_a_known_level():
    for name in backends.registered_backends():
        level = backends._REGISTRY[name]().profile().isolation
        assert isolation.known(level), (name, level)


def test_the_local_backend_is_venv_and_therefore_cannot_host_repository_code():
    """The whole point of the change, stated as a test rather than as a comment."""
    local = backends.LocalBackend(Config.load())
    assert local.profile().isolation == "VENV"
    assert not isolation.sufficient_for_repo_exec(local.profile().isolation)


def test_only_a_confining_backend_may_run_a_repository():
    """Two conditions, and BOTH are needed: sufficient isolation and `can_execute`.

    This asserts the RULE, not the roster. It used to assert that `modal` was the only
    backend clearing both, which was true when the only alternatives were this machine and
    two declarations — and which stopped being true the moment a container backend was
    registered. A test that pins the membership of the set makes adding a backend that
    satisfies the boundary look like a regression, when satisfying the boundary is exactly
    what a new backend is supposed to do. What must never change is that clearing the
    boundary requires declaring CONTAINER or REMOTE_SESSION and being able to execute.

    Kaggle and Colab describe remote sessions, so they satisfy isolation and are still
    refused — as declarations, by `can_execute`. `local` can execute and is still refused,
    by isolation. Those two halves are the invariant.
    """
    profiles = {n: backends._REGISTRY[n]().profile() for n in backends.registered_backends()}
    confined = {n for n, p in profiles.items()
                if isolation.sufficient_for_repo_exec(p.isolation)}
    runnable = {n for n, p in profiles.items() if p.can_execute}

    # Every backend that clears the boundary declares one of exactly two levels, and no
    # backend clears it by any other route.
    for name in confined:
        assert profiles[name].isolation in isolation.ISOLATION_SUFFICIENT_FOR_REPO_EXEC, name
    for name, p in profiles.items():
        if p.isolation not in isolation.ISOLATION_SUFFICIENT_FOR_REPO_EXEC:
            assert name not in confined, name

    # The two halves are independent: being able to run does not confine, and confining
    # does not make a declaration runnable.
    assert "local" in runnable and "local" not in confined, (
        "this machine can execute and may never host a paper's own repository")
    assert {"kaggle", "colab"} <= confined and not ({"kaggle", "colab"} & runnable), (
        "a declaration satisfies the boundary and is still refused, by can_execute")
    assert confined & runnable, "some backend must be able to run a repository"


def test_a_backend_that_declares_nothing_is_refused_rather_than_trusted():
    """The default is NONE, so forgetting to declare is a refusal, not a grant."""
    assert backends.ExecutionBackend.isolation == "NONE"
    assert backends.BackendProfile(name="x", platform="linux").isolation == "NONE"
    assert not isolation.sufficient_for_repo_exec("NONE")


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_repository_execution_is_refused_on_an_insufficient_backend(open_cfg):
    a = backends.authorize(open_cfg, _fully_qualified_spec(), _backend_at("VENV"),
                           commit=VERIFIED)
    assert not a.allowed
    assert a.decision == "isolation_insufficient"
    assert a.failure_class == "execution_unauthorized"


def test_the_refusal_names_the_remedy_and_accuses_nobody(open_cfg):
    a = backends.authorize(open_cfg, _fully_qualified_spec(), _backend_at("VENV"),
                           commit=VERIFIED)
    assert "CONTAINER or REMOTE_SESSION" in a.detail
    assert "Nothing about the paper follows" in a.detail


@pytest.mark.parametrize("level", ["CONTAINER", "REMOTE_SESSION"])
def test_a_sufficient_backend_passes_the_isolation_condition(open_cfg, level):
    """Fully qualified plus sufficient isolation authorizes; that is the only way through."""
    a = backends.authorize(open_cfg, _fully_qualified_spec(), _backend_at(level),
                           commit=VERIFIED)
    assert a.allowed, a.detail
    assert a.decision == "authorized", a.decision


@pytest.mark.parametrize("level", ["NONE", "VENV"])
def test_no_scientific_condition_can_rescue_an_unconfined_backend(open_cfg, level):
    """Even with identity, capability, resources and the commit all established."""
    a = backends.authorize(open_cfg, _fully_qualified_spec(), _backend_at(level),
                           commit=VERIFIED)
    assert not a.allowed and a.decision == "isolation_insufficient"


def test_isolation_is_asked_before_the_scientific_conditions(open_cfg):
    """A bare repo_exec spec on an unconfined backend reports isolation, not identity.

    Ordering matters for what an operator is told first: "this machine may not run other
    people's code" is actionable, and "the experiment is unidentified" sends them to fix
    the wrong thing.
    """
    bare = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec")
    a = backends.authorize(open_cfg, bare, _backend_at("VENV"), commit=VERIFIED)
    assert a.decision == "isolation_insufficient"
    # ...and on a confined backend the very same spec reports the next real problem.
    b = backends.authorize(open_cfg, bare, _backend_at("REMOTE_SESSION"), commit=VERIFIED)
    assert b.decision == "identity_unproven"


def test_the_closed_gate_still_reports_the_gate(open_cfg):
    """A TIGHTENING must not reorder what a SHUT gate says: the gate is still first."""
    shut = Config.load()
    shut.allow_repo_exec = False
    a = backends.authorize(shut, _fully_qualified_spec(), _backend_at("VENV"),
                           commit=VERIFIED)
    assert a.decision == "gate_closed"


def test_our_own_probe_is_unaffected_by_the_boundary(open_cfg):
    """`not_repo_execution` runs code this harness or its operator wrote. The boundary is
    about running a THIRD PARTY's program, and nothing here restricts our own."""
    for level in isolation.ISOLATION_LEVELS:
        a = backends.authorize(open_cfg, ProbeSpec(paper_id="p"), _backend_at(level))
        assert a.allowed and a.decision == "not_repo_execution", level


def test_the_boundary_only_ever_removes_authorizations(open_cfg):
    """The tightening property, swept: for every level, authorized ⇒ sufficient.

    There is no (spec, backend) pair that the isolation condition turns from refused into
    allowed, because the condition can only return a refusal.
    """
    spec = _fully_qualified_spec()
    for level in isolation.ISOLATION_LEVELS:
        a = backends.authorize(open_cfg, spec, _backend_at(level), commit=VERIFIED)
        if a.allowed:
            assert isolation.sufficient_for_repo_exec(level), level
