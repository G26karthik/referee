"""The CI literal sweep the v4 redesign's governing constraint requires.

Finding 1 of the redesign plan: `artifacts.py` declares closed vocabularies but nothing in
`harness/` ever checked that a production `decision=`/`failure_class=` string literal is
actually a member of its own vocabulary. Two of `authorize()`'s own safety refusals
(`isolation_insufficient`, `conformance_unproven`) and three more values
(`commit_changed_during_execution`, `reimplementation_nonconformant`,
`infrastructure_failure`) were emitted by production code before this tuple declared them —
a closed vocabulary that was open in practice. Generating a `Literal` from the OLD tuples
would have made `authorize()` raise `ValidationError` on its own refusals; the standard fix
for that (a `try/except` around the raise) would have converted a fail-closed boundary into
a fail-open one.

This test is the guard against that ever recurring silently: it walks `harness/**/*.py`
with `ast`, finds every keyword argument named `decision` or `failure_class` whose value is
a string literal, and asserts membership in `EXEC_DECISIONS` / `FAILURE_CLASSES`. It must
be RED against the pre-fix tuples and GREEN after — that is the proof it measures anything,
not just a shape.
"""
from __future__ import annotations

import ast
from pathlib import Path

from harness.artifacts import EXEC_DECISIONS, FAILURE_CLASSES

HARNESS_ROOT = Path(__file__).resolve().parents[1] / "harness"

# Keyword-argument name -> the vocabulary tuple it must be a member of.
VOCAB_FOR_KEYWORD = {
    "decision": EXEC_DECISIONS,
    "failure_class": FAILURE_CLASSES,
}


def _string_keyword_literals(py_file: Path) -> list[tuple[str, str, int]]:
    """Every (keyword, literal value, line) this file assigns to a vocabulary-bearing name.

    Two shapes are swept, because the ratchet's own gap was that only one was checked:
    a call keyword (`ExecAuthorization(decision="...", ...)`) and a plain attribute or
    variable assignment (`rec.failure_class = "..."`) — the exact shape that let
    `reimplementation_nonconformant` and `infrastructure_failure` go undeclared while a
    kwarg-only sweep of the same file stayed green. Only plain `ast.Constant` string
    values are collected — an f-string, a variable, or a conditional expression is not a
    literal this sweep can classify, and is deliberately left alone rather than guessed at.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    hits: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in VOCAB_FOR_KEYWORD and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    hits.append((kw.arg, kw.value.value, node.lineno))
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                name = target.attr if isinstance(target, ast.Attribute) \
                    else target.id if isinstance(target, ast.Name) else None
                if name in VOCAB_FOR_KEYWORD:
                    hits.append((name, node.value.value, node.lineno))
    return hits


def test_every_decision_and_failure_class_literal_is_declared():
    violations = []
    for py_file in sorted(HARNESS_ROOT.rglob("*.py")):
        for keyword, value, lineno in _string_keyword_literals(py_file):
            vocab = VOCAB_FOR_KEYWORD[keyword]
            if value not in vocab:
                violations.append(f"{py_file.relative_to(HARNESS_ROOT.parent)}:{lineno} "
                                  f"{keyword}={value!r} not in its declared vocabulary")
    assert not violations, (
        "closed vocabulary open in practice — a decision/failure_class literal is not a "
        "member of its own declared tuple:\n" + "\n".join(violations))


def test_sweep_actually_finds_something_when_the_vocabulary_regresses():
    """The sweep is not a no-op: prove it flags a value that is genuinely absent."""
    assert "this_value_will_never_be_declared" not in EXEC_DECISIONS
    assert "this_value_will_never_be_declared" not in FAILURE_CLASSES


if __name__ == "__main__":
    test_every_decision_and_failure_class_literal_is_declared()
    test_sweep_actually_finds_something_when_the_vocabulary_regresses()
    print("harness/**/*.py: every decision=/failure_class= literal is a declared "
          f"vocabulary member ({len(EXEC_DECISIONS)} decisions, {len(FAILURE_CLASSES)} "
          "failure classes).")
