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
directory exists — and `tests/test_local_exec.py` uses it to drive real probe
subprocesses against `tmp_path`.
"""
from __future__ import annotations

import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]   # .../single-harness
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

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
