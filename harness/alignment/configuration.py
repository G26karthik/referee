"""The fix for APT's `ambiguous`: telling fitting candidates apart by CONFIGURATION.

`python -m harness.alignment.configuration` runs the self-check.

**The shape of the defect this closes.** `experiment_id.resolve_experiment` refuses
`ambiguous` when more than one advertised command emits the cell's quantity — correctly,
because choosing between two commands that both fit would be a guess. Measured on a real
paper: 84 advertised scripts, every one of them an accuracy-emitting evaluation, none
chosen. What none of them checked is whether the SCRIPTS THEMSELVES say which
configuration they run — `scripts/llama7b_ratio50.sh` hardcoding `--pruning_ratio 0.5
--base_model llama-7b`, say — against what the CITED CELL's own row and caption say the
configuration is. When exactly one candidate's declared configuration agrees with the
cell's and none contradicts it, `ambiguous` was never the honest answer; it was the
answer to a question ("which one emits the quantity") narrower than the one the cell
actually poses ("which one produces THIS row").

**Contradiction eliminates; silence does not.** A candidate whose declared sparsity is
0.2 when the cell's row says 0.5 is POSITIVELY the wrong script, and is dropped. A
candidate that declares no sparsity at all is neither confirmed nor refuted — dropping it
too would be inventing evidence a repository never gave, so it survives alongside any
candidate that actually agrees. Narrowing to one requires that ONE candidate genuinely
agrees on at least one field and no surviving candidate contradicts any field; anything
short of that is reported honestly as still ambiguous, with the field-by-field record for
a reader to check by hand.

**A deliberate, narrow duplication.** `experiment_id.resolve_configuration` already
extracts model/dataset/sparsity from a cell's TABLE CAPTION for the separate
`ConfigurationIdentity` chain — the "does the seed policy match" question. That table
stays as it is; this module's own field patterns are not imported from it and vice
versa, for two reasons: this module also reads the CITED ROW's own cells (a caption is
usually shared across every row of a table, so "50% sparsity" in a caption cannot tell a
50%-row apart from an 80%-row in the same table — the row can), and unifying the two
tables now would touch `resolve_configuration`'s already-tested behaviour for a benefit
this module does not need. A future consolidation is legitimate; it is not this one.
"""
from __future__ import annotations

import re

from ..artifacts import CandidateCommand, Table

# field -> substrings recognised in a CLI FLAG NAME. Used by `alignment.candidates` to
# decide which `--flag value` pair in a script's own text is worth keeping as a
# configuration field, as opposed to an unrelated flag like `--epochs` or `--lr`.
FIELD_FLAG_HINTS: dict[str, tuple[str, ...]] = {
    "model": ("model", "base_model", "backbone", "arch"),
    "dataset": ("dataset", "data", "task", "benchmark"),
    "sparsity": ("sparsity", "ratio", "prune", "pruning"),
}

# field -> a pattern recognising its VALUE in free text — a caption, a row's cells, or a
# script's own hardcoded literal. Deliberately small: the point is to tell apart what a
# real paper's table varies its rows by, not to name every possible experimental axis.
FIELD_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "model": re.compile(
        r"(LLaMA[\s\-]?2?[\s\-]?\d+B|RoBERTa\w*|T5\w*|BERT\w*|GPT-?J|Vicuna[\w\-]*)", re.I),
    "dataset": re.compile(
        r"(Alpaca[\w\-]*|GLUE|SST-?2|CIFAR-?\d+|ImageNet|SQuAD\w*|CNN/?DM|WikiText\w*)", re.I),
    # A bare percentage, unlike `experiment_id`'s caption pattern which requires the
    # literal word "sparsity" adjacent — a row's own "Ratio" column often carries just
    # "50%" with the field named by the COLUMN HEADER instead of by the cell text.
    #
    # The lookarounds are load-bearing. `\b(\d{1,3})\s*%` matched the `5%` INSIDE the
    # metric value `4523.5%` on APT's Table 1, because `\b` sits between the `.` and the
    # `5`. That fabricated a sparsity of 5 for a row whose caption states 60% sparsity,
    # and `narrow` then eliminated 75 of 84 candidate commands by contradiction against a
    # number that appears nowhere in the paper. It happened not to reach a single
    # survivor, so it refused — but the mechanism that promotes `ambiguous` to
    # `established` was running on an invented value, which is the one thing identity
    # binding may never do.
    "sparsity": re.compile(r"(?<![\d.])(\d{1,3})\s*%(?!\d)"),
}


def _normalize(field: str, value: str) -> str:
    """A field's value, in the ONE shape it needs to be compared in.

    Sparsity is the only field with more than one honest spelling for the same number —
    `50%`, `0.5`, `50` — so it is the only one normalised numerically; everything else is
    compared as a lowercased, punctuation-stripped token, which is enough to equate
    `LLaMA-7B` with `llama7b` without claiming any deeper semantic understanding.
    """
    v = (value or "").strip()
    if field == "sparsity":
        try:
            f = float(v.rstrip("%"))
        except ValueError:
            return re.sub(r"[^a-z0-9]", "", v.lower())
        if f > 1:
            f = f / 100.0
        return f"{f:.4g}"
    return re.sub(r"[^a-z0-9]", "", v.lower())


def _extract(text: str) -> dict[str, str]:
    """Every field `FIELD_VALUE_PATTERNS` recognises in one span of text, first match
    per field."""
    out: dict[str, str] = {}
    for field, pattern in FIELD_VALUE_PATTERNS.items():
        if m := pattern.search(text or ""):
            out[field] = m.group(1) if m.groups() else m.group(0)
    return out


def expected_fields(table: Table, row: int, col: int) -> tuple[dict[str, str], dict[str, str]]:
    """(fields, evidence) the CITED CELL's row asks a candidate to match.

    The caption is checked first and the ROW's own cells checked second, WINNING on a
    conflict — a caption naming the table's overall model and a row whose own column
    states a DIFFERENT one (a multi-model comparison table) means the row is the more
    specific, and therefore more trustworthy, source for THIS cell.
    """
    fields: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for k, v in _extract(table.caption or "").items():
        fields[k] = v
        evidence[k] = f"T{table.table_idx}:caption"
    if 0 <= row < len(table.rows):
        # The whole row, not just the cited column: the CITED cell is a metric, and the
        # field that would disambiguate it (a "Ratio" or "Sparsity" column) usually sits
        # elsewhere in the same row. The column header at `col` is folded in too, so a
        # bare header word reinforces a value found beside it.
        header = table.header[col] if 0 <= col < len(table.header) else ""
        row_text = header + " | " + " | ".join(c for c in table.rows[row] if c)
        # The NAMING evidence is looked for across the whole table's header rows, not just
        # the cited column's header: the column that names the field ("Ratio", "Sparsity")
        # is almost never the column holding the cited metric, and a table whose header
        # list is empty still carries its column names in row 0.
        named_in = (" | ".join(h for h in table.header if h) + " | "
                    + " | ".join(c for c in (table.rows[0] if table.rows else []) if c)
                    + " | " + row_text).lower()
        for k, v in _extract(row_text).items():
            # A ROW VALUE IS ACCEPTED ONLY WHERE SOMETHING NAMES THE FIELD. Not merely for
            # overrides — always. Two real failures forced this, both on APT's tables and
            # both of which produced a CONFIDENT WRONG ANSWER rather than a refusal:
            #
            #   * `4523.5%` yielded a sparsity of 5, displacing the caption's correct 60%;
            #   * a `10%` in a column headed DENSITY yielded a sparsity of 10, and
            #     narrowed 84 candidate commands to exactly one. Density 10% IS SPARSITY
            #     90%, so that binding matched a command to a row meaning the opposite,
            #     and identity read `established` on seven targets.
            #
            # An established identity authorises running that command against that cell,
            # so a wrong one is not a missed check — it is a measurement of the wrong
            # experiment presented as the paper's own. `DENSITY` is deliberately NOT a
            # naming token for sparsity: it is the inverse quantity, and reading one as
            # the other is the error above, not a synonym.
            # One condition, over the table's own naming context: the header rows plus
            # this row. A table with a "Ratio" or "Sparsity" column names the field for
            # every row in it, so the row's value is the specific one and wins. A table
            # with no such column names it nowhere, so a bare percentage in a row is just
            # a number and the caption — which did name the field — stands.
            if not any(h in named_in for h in FIELD_FLAG_HINTS.get(k, ())):
                continue
            fields[k] = v
            evidence[k] = f"T{table.table_idx}:r{row}"
    return fields, evidence


def narrow(fitting: list[CandidateCommand],
          expected: dict[str, str]) -> tuple[list[CandidateCommand], dict[str, str]]:
    """(survivors, reasons) — candidates the cited cell's configuration does not exclude.

    `reasons` maps a DROPPED candidate's `source_ref` to why: only a genuine field
    CONTRADICTION drops a candidate. A candidate that declares nothing comparable is
    never dropped by this function — it may still be the right one, and dropping it on
    silence would be inventing evidence the repository never gave. Called only when
    `expected` is non-empty; an empty `expected` narrows nothing; the caller keeps the
    original refusal.

    Returns the FULL input list, unchanged, when `expected` is empty or when narrowing
    would not change the answer — the caller compares length to decide whether anything
    was actually learned.
    """
    if not expected or not fitting:
        return fitting, {}

    survivors: list[CandidateCommand] = []
    supported: list[CandidateCommand] = []
    reasons: dict[str, str] = {}
    for cmd in fitting:
        contradicted = ""
        agreed: list[str] = []
        for field, want in expected.items():
            got = cmd.declared_args.get(field)
            if got is None:
                continue
            if _normalize(field, got) == _normalize(field, want):
                agreed.append(field)
            else:
                contradicted = (f"declares {field}={got!r}, the cited cell's row/caption "
                                f"says {field}={want!r}")
                break
        if contradicted:
            reasons[cmd.source_ref] = contradicted
            continue
        survivors.append(cmd)
        if agreed:
            supported.append(cmd)

    # Support outranks silence, exactly the way an actual agreement outranks a candidate
    # that simply was not disproven. Only escalates when it would genuinely narrow.
    if supported and len(supported) < len(survivors):
        return supported, reasons
    return survivors, reasons


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    from ..artifacts import Table

    # --- field extraction: row wins over caption ---------------------------------------
    t = Table(table_idx=2, caption="Table 2: accuracy under 50% sparsity on LLaMA-7B",
             header=["Method", "Ratio", "Acc"],
             rows=[["Ours", "20%", "61.4"], ["Ours", "80%", "55.2"]])
    fields0, ev0 = expected_fields(t, row=0, col=2)
    assert fields0["sparsity"] == "20", "the ROW's own value wins over the caption's 50%"
    assert fields0["model"] == "LLaMA-7B", "the caption still supplies what the row lacks"
    assert ev0["sparsity"] == "T2:r0"
    assert ev0["model"] == "T2:caption"

    fields1, _ = expected_fields(t, row=1, col=2)
    assert fields1["sparsity"] == "80"

    # --- normalisation: three spellings of one number agree -----------------------------
    assert _normalize("sparsity", "50%") == _normalize("sparsity", "0.5")
    assert _normalize("sparsity", "50") == _normalize("sparsity", "0.5")
    assert _normalize("model", "LLaMA-7B") == _normalize("model", "llama_7b")

    # --- THE FIX: exactly one candidate agrees, the rest neither agree nor contradict ---
    a = CandidateCommand(argv=["bash", "a.sh"], source_ref="a.sh:1",
                        declared_args={"sparsity": "0.2"})
    b = CandidateCommand(argv=["bash", "b.sh"], source_ref="b.sh:1",
                        declared_args={"sparsity": "0.5"})
    c = CandidateCommand(argv=["bash", "c.sh"], source_ref="c.sh:1", declared_args={})
    survivors, reasons = narrow([a, b, c], {"sparsity": "0.5"})
    assert survivors == [b], "b agrees, a contradicts, c is silent and survives contradiction-\
only filtering but loses to b's genuine support"
    assert "0.2" in reasons["a.sh:1"] and "0.5" in reasons["a.sh:1"]
    assert "c.sh:1" not in reasons, "silence is never a contradiction"

    # --- no genuine support anywhere: still reported as ambiguous, contradictions only --
    d = CandidateCommand(argv=["bash", "d.sh"], source_ref="d.sh:1", declared_args={})
    e = CandidateCommand(argv=["bash", "e.sh"], source_ref="e.sh:1", declared_args={})
    survivors2, reasons2 = narrow([d, e], {"sparsity": "0.5"})
    assert survivors2 == [d, e], "nothing to prefer between two silent candidates"
    assert reasons2 == {}

    # --- an empty expectation narrows nothing --------------------------------------------
    assert narrow([a, b], {}) == ([a, b], {})
    assert narrow([], {"sparsity": "0.5"}) == ([], {})

    # --- every dropped candidate's reason names BOTH values, checkable by a reader ------
    _, r3 = narrow([a], {"sparsity": "0.8"})
    assert "0.2" in r3["a.sh:1"] and "0.8" in r3["a.sh:1"]
    print("harness.alignment.configuration self-check ok")


if __name__ == "__main__":
    _self_check()
