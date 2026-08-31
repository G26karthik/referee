"""Prompt for S1 — claim + reported-number extraction from an ingested paper.

The model never sees the PDF. It sees `harness/pdf.py`'s deterministic render:
sections as text, tables as addressed cells. Its only job is to say what the paper
CLAIMS about itself and which numbers it REPORTS — not to judge either. Judging is
the audit stage's job, and keeping the two apart is what lets the auditors run
blinded.
"""
from __future__ import annotations

SYS = (
    "You are a precise extraction subagent working on ONE research paper. You do not "
    "evaluate, praise, or criticize the paper — you record what it asserts and what it "
    "reports, with exact provenance, so a separate blinded auditor can judge it.\n\n"
    "PROVENANCE IS ABSOLUTE. Every claim and every number you emit carries a "
    "source_quote copied VERBATIM from the text or cell you were given — same digits, "
    "same units, same wording. You may not paraphrase a quote, round a number, convert "
    "units, or compute a delta the paper did not print. If you cannot find the exact "
    "text for something, DO NOT EMIT IT. A dropped number costs nothing; an invented "
    "one poisons the whole review. Reply with ONLY JSON."
)


def build(title: str, sections_text: str, tables_text: str) -> str:
    return f"""Paper under review: {title or "(title not detected)"}

=== SECTIONS ===
{sections_text or "(no section text extracted)"}

=== TABLES (each cell is addressed T<table>:r<row>:c<col>) ===
{tables_text or "(no tables extracted)"}

Extract two lists.

(1) claims — the assertions the paper makes ABOUT ITSELF. Aim for 8-20. Cover:
    - kind "premise": the motivating rationale — WHY this method should work at all.
      Capture it even when it is stated informally; the auditors need the paper's own
      reasoning to test whether it is sound or merely intuitive-sounding.
    - kind "contribution": what the paper says it contributes (usually the abstract's
      or introduction's numbered list).
    - kind "result": an empirical outcome the paper asserts ("X improves Y by Z").
    - kind "comparison": an assertion that the method beats / matches some baseline.
    Prefer claims that a reviewer could actually contest. Skip background statements
    about other people's work.

(2) reported_numbers — every headline quantitative result the paper states, from the
    prose AND from the tables. For a number read out of a table, set table_ref to that
    cell's exact address (e.g. "T0:r2:c1") and source_quote to the cell's literal
    contents. For a number stated in prose, set page to the "[pN]" marker of the
    section it appeared in and source_quote to the sentence containing it.
    Record ONLY numbers actually printed. If the paper reports no numbers, return [].

Return ONLY JSON:
{{"title": "the paper's title as printed (or '' if you cannot find it)",
  "claims": [
    {{"claim_id": "c01",
      "kind": "premise|contribution|result|comparison",
      "text": "the claim in one sentence, as a reviewer would restate it",
      "source_quote": "verbatim sentence(s) it came from",
      "section": "the '## <title>' heading it sits under",
      "page": <int from the [pN] marker, 0 if unknown>}}
  ],
  "reported_numbers": [
    {{"benchmark": "dataset/task, e.g. 'CIFAR-100'",
      "metric": "e.g. 'top-1 accuracy'",
      "method": "which arm this number is for (baseline | <method name>)",
      "value": "the number with units, verbatim",
      "baseline_value": "the baseline it is compared against, verbatim, or ''",
      "delta": "the improvement ONLY IF the paper prints one, else ''",
      "seeds_or_variance": "#seeds / std / CI as printed, or '' if not reported",
      "dataset_scale": "train size / #params / compute if stated, else ''",
      "source_quote": "verbatim sentence or cell contents",
      "page": <int>,
      "table_ref": "T<t>:r<r>:c<c> if from a table, else ''"}}
  ]}}"""
