"""Pytest bootstrap for the single-harness test suite.

`single-harness/` is deliberately NOT a uv workspace member (it has no
`pyproject.toml`) and is deliberately absent from the repo-root
`[tool.pytest.ini_options] testpaths`. Adding it there would pull an
un-packaged directory into CI and `make pre-pr`; instead these tests are run
explicitly:

    uv run pytest single-harness/tests -q

Because the package is never installed, `import harness.…` cannot resolve on
its own — this conftest puts `single-harness/` itself at the front of
`sys.path`. The repo-root `conftest.py` still loads as an ancestor conftest,
but it only manipulates env vars (no DB connection), so it is harmless here.

Every test here is a pure-function or local-subprocess test: no credentials, no
network. `harness.config.Config.load()` is safe to call — it only ensures the cases
directory exists — and `tests/test_local_execution.py` uses it to drive real probe
subprocesses against `tmp_path`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).resolve().parents[1]   # .../single-harness
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))


@pytest.fixture()
def confined_local(monkeypatch):
    """Run the LOCAL backend as though it were confined. Opt-in, never automatic.

    `authorize()` requires CONTAINER or REMOTE_SESSION isolation before it will run a
    paper's own repository (`harness.isolation`), and `LocalBackend` declares VENV — so on
    this host no `repo_exec` spec can be authorized at all. That is the intended production
    posture and `tests/test_isolation_boundary.py` is what pins it.

    Most execution tests are about something else entirely: whether identity binds,
    whether a crash convicts, whether a reconciliation is admissible, whether a resource
    shortfall blocks. Those questions are backend-independent, and rewriting each of them
    to lease a remote sandbox would test the provider instead of the property.

    So this fixture raises the LOCAL backend's declared isolation for the duration of one
    test, and it is deliberately NOT autouse: a test that wants the boundary out of the
    way has to say so on its own signature, and a reader of that signature can see that
    the run is simulated. Nothing here touches `ISOLATION_SUFFICIENT_FOR_REPO_EXEC`, which
    stays exactly as production reads it.
    """
    from harness import backends

    monkeypatch.setattr(backends.LocalBackend, "isolation", "REMOTE_SESSION")
    return "REMOTE_SESSION"

# Where the authored fixture papers live. Searched rather than hardcoded because they
# have already moved once: `papers/` accumulates real papers under review, someone filed
# the four synthetic ones into `papers/authored/`, and eight tests went red pointing at
# the old path — a whole-suite failure caused by tidying, with nothing wrong in the code.
# A fixture that finds its own file makes that class of breakage impossible.
_PAPER_DIRS = (HARNESS_ROOT / "papers" / "authored", HARNESS_ROOT / "papers")


def fixture_paper(name: str) -> Path:
    """The authored fixture PDF `name`, wherever it currently lives.

    Raises rather than returning a missing path: a test that silently proceeds with a
    nonexistent fixture reports an ingest failure, which looks like a harness defect and
    is not one.
    """
    for d in _PAPER_DIRS:
        if (p := d / name).exists():
            return p
    raise FileNotFoundError(
        f"fixture paper {name!r} not found in {[str(d) for d in _PAPER_DIRS]}")


# --------------------------------------------------------------------------- #
# MATERIALITY CONTEXT for tests that exercise a PAPER-LEVEL failure
# --------------------------------------------------------------------------- #
# Establishing a defect is Tier 1 (`TargetOutcome.establishes_failure`) and no longer
# convicts a paper on its own: only a target a central scientific claim is established to
# depend on may reach VERIFIED_FAILURE / RED / STOP_MATERIAL_FAILURE. See
# `harness.materiality` — the one basis implemented is ABSTRACT_CLAIM, a conservative
# sufficient condition and deliberately not a complete materiality model.
#
# A test that means "this route can convict" has to say which target the paper's claim
# rests on, because a paper-level stop may never depend on whether a caller happened to
# pass an optional argument. This helper is that statement, written once.
def material_objects(*target_ids: str) -> list:
    """DiscoveredObjects marking each target as material to a central claim."""
    from harness.artifacts import DiscoveredObject

    return [DiscoveredObject(target_id=t, centrality="CENTRAL",
                             materiality_basis="ABSTRACT_CLAIM")
            for t in (target_ids or ("T",))]


def incidental_objects(*target_ids: str) -> list:
    """The counterpart: the target exists, and no central claim was established to depend
    on it. A defect here is established, reported, and does not stop the paper."""
    from harness.artifacts import DiscoveredObject

    return [DiscoveredObject(target_id=t, centrality="CENTRAL", materiality_basis="NONE")
            for t in (target_ids or ("T",))]
