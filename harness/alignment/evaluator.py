"""Which quantity a candidate's code actually COMPUTES, from a real call site.

`python -m harness.alignment.evaluator` runs the self-check.

**Why a bare quoted key is not enough.** `experiment_id.describe_command` classifies a
candidate by searching its source for a QUOTED KEY like `"accuracy"` or `"f1"` — cheap,
and it can be wrong in a specific way: an argparse help string (`help="report accuracy"`),
a config dict key that is never populated, or a docstring can all contain the same quoted
word without the file computing anything. This module looks for the CODE STRUCTURE that
actually produces a quantity — a call to a named metric function, or a
`compute_metrics`-shaped definition — which a comment or a help string cannot imitate.

**Corroboration, not a replacement.** `describe_command`'s key search stays exactly as it
is; this module is consulted only when that search comes up with nothing (`cmd.emits`
empty), and it only ever ADDS quantities to `cmd.emits` — it never removes one the key
search found, and it never changes `identities_established`'s contract. A repository
whose evaluator this module still cannot classify stays `unknown`, honestly.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import experiment_id as experiment_id_mod
from ..artifacts import CandidateCommand, IdentityEvidence

# quantity -> patterns matching a CALL or a DEFINITION that computes it, not merely a
# word that names it. Each is anchored on a real function/attribute a metric library or a
# training loop actually exposes, so a help string or a docstring cannot match one.
METRIC_CALL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "accuracy": (
        re.compile(r"\baccuracy_score\s*\("),
        re.compile(r"\bdef\s+compute_metrics\s*\("),
        re.compile(r"\.compute\(\s*predictions"),
    ),
    "loss": (
        re.compile(r"\bcross_entropy\s*\("),
        re.compile(r"\bnll_loss\s*\("),
        re.compile(r"\bF\.mse_loss\s*\("),
    ),
    "memory": (
        re.compile(r"\bmax_memory_allocated\s*\("),
        re.compile(r"\bmemory_allocated\s*\("),
        re.compile(r"\btorch\.cuda\.memory_stats\s*\("),
    ),
    "flops": (
        re.compile(r"\bFlopCountAnalysis\s*\("),
        re.compile(r"\bprofile\([^)]*with_flops"),
    ),
    "latency": (
        re.compile(r"\btime\.perf_counter\s*\("),
        re.compile(r"\btorch\.cuda\.Event\s*\("),
    ),
}


def find_in_text(text: str, source_ref: str = "") -> tuple[set[str], list[IdentityEvidence]]:
    """(quantities, evidence) every `METRIC_CALL_PATTERNS` match in `text`.

    Pure over the text; `source_ref` is a filename echoed with a 1-indexed line number so
    each finding is re-checkable, the same discipline `experiment_id`'s own evidence
    pointers already follow.
    """
    quantities: set[str] = set()
    evidence: list[IdentityEvidence] = []
    for quantity, patterns in METRIC_CALL_PATTERNS.items():
        for pattern in patterns:
            m = pattern.search(text or "")
            if not m:
                continue
            line = text[:m.start()].count("\n") + 1
            quantities.add(quantity)
            evidence.append(IdentityEvidence(
                quote=m.group(0), source_ref=f"{source_ref}:{line}" if source_ref else str(line),
                note=f"a real call site computing '{quantity}', not a quoted key"))
            break                                   # one witness per quantity is enough
    return quantities, evidence


def evaluator_quantities(repo: Path, cmd: CandidateCommand,
                         depth: int = 2) -> tuple[set[str], list[IdentityEvidence]]:
    """The quantities a candidate's code computes, hopping one level from shell into
    python exactly as `experiment_id.describe_command` does for output keys.

    Reads the same file `describe_command` would follow into, so the two functions are
    meant to be called on the SAME candidate and their evidence to sit side by side.
    """
    path = experiment_id_mod.target_file(repo, cmd)
    if path is None:
        return set(), []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), []

    quantities, evidence = find_in_text(text, source_ref=str(path))
    if depth > 0 and path.suffix == ".sh":
        for m in re.finditer(r"\b(?:python3?|bash)\s+(\S+\.(?:py|sh))", text):
            nested = repo / m.group(1)
            if nested.is_file():
                inner_cmd = CandidateCommand(argv=["python", m.group(1)])
                inner_q, inner_e = evaluator_quantities(repo, inner_cmd, depth - 1)
                quantities |= inner_q
                evidence += inner_e
    return quantities, evidence


def corroborate(repo: Path, cmd: CandidateCommand) -> CandidateCommand:
    """Fill `cmd.emits`/`cmd.label` from evaluator code, ONLY where the key search found
    nothing. Mutates and returns `cmd`, exactly as `describe_command` does; meant to be
    called immediately after it.

    Writes the QUANTITY NAME ITSELF into `cmd.emits` (`"accuracy"`, not
    `"eval_accuracy"`) rather than a new field, because `experiment_id._OUTPUT_QUANTITY`
    already carries a `(quantity, quantity)` pair for every quantity this module
    recognises — `("accuracy", "accuracy")`, `("loss", "loss")`, and so on — so a caller
    filtering candidates by `any(q == wanted for k, q in _OUTPUT_QUANTITY if k in
    cmd.emits)` picks up an evaluator-corroborated candidate with no change of its own.
    A second field here would need every such caller updated to read it; reusing the
    vocabulary that already exists needs none.
    """
    if cmd.emits:
        return cmd                                  # the key search already found something
    quantities, _ = evaluator_quantities(repo, cmd)
    if quantities:
        cmd.emits = sorted(quantities)
        cmd.label = ", ".join(sorted(quantities)) + " (from evaluator code)"
    return cmd


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    text = (
        "from sklearn.metrics import accuracy_score\n"
        "def evaluate(preds, labels):\n"
        "    acc = accuracy_score(labels, preds)\n"
        "    return {'result': acc}\n")
    quantities, evidence = find_in_text(text, source_ref="eval.py")
    assert quantities == {"accuracy"}
    assert evidence[0].source_ref == "eval.py:3"
    assert "accuracy_score" in evidence[0].quote

    # a help string or docstring containing the bare WORD, with no call parentheses,
    # must not match — this is the whole point of matching a CALL SHAPE rather than a key
    no_call = 'parser.add_argument("--metric", help="report accuracy for this run")'
    assert find_in_text(no_call)[0] == set(), "a bare word with no call site matches nothing"
    # but the identical word INSIDE a real call shape still matches, even embedded in a
    # string argument — the shape is what is recognised, not the surrounding context
    in_a_string = 'cmd = "python -c \'print(accuracy_score(a, b))\'"'
    assert find_in_text(in_a_string)[0] == {"accuracy"}

    assert find_in_text("")[0] == set()
    assert find_in_text("x = 1 + 1")[0] == set()

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "eval.py").write_text(text, encoding="utf-8")
        cmd = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
        q, ev = evaluator_quantities(repo, cmd)
        assert q == {"accuracy"} and ev

        # --- corroboration only fills a GAP, never overrides an existing classification -
        already_classified = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1",
                                              emits=["model_flops"], label="flops")
        corroborate(repo, already_classified)
        assert already_classified.label == "flops", "an existing finding is never replaced"

        empty = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
        corroborate(repo, empty)
        assert "accuracy" in empty.label and "evaluator code" in empty.label
        assert empty.emits == ["accuracy"], (
            "written as the QUANTITY NAME itself, so experiment_id's existing "
            "_OUTPUT_QUANTITY-based filtering recognises it with no change of its own")

        # a candidate with no file to read yields nothing, never a crash
        ghost = CandidateCommand(argv=["bash", "missing.sh"], source_ref="x:1")
        assert evaluator_quantities(repo, ghost) == (set(), [])
        corroborate(repo, ghost)
        assert ghost.label == ""

        # one hop from shell into python, matching describe_command's own depth
        (repo / "run.sh").write_text("python eval.py\n", encoding="utf-8")
        shell_cmd = CandidateCommand(argv=["bash", "run.sh"], source_ref="run.sh:1")
        q2, _ = evaluator_quantities(repo, shell_cmd)
        assert q2 == {"accuracy"}, "the hop into the python it calls must still find it"
    print("harness.alignment.evaluator self-check ok")


if __name__ == "__main__":
    _self_check()
