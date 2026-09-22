"""Ask a reader to check the PARSER against the page images — never the paper's science.

`python -m harness.prompts.extraction_audit` runs the self-check.

This is a vision check of EXTRACTION, not a second scientific reader. The question is
narrow: does a parsed table roughly match what is printed on its own page, and does the
parsed title match what page 1 shows? Nothing here proposes a finding, a severity, or a
verdict about the paper — see `harness/extraction_audit.py`'s module docstring and
invariant 13 (document integrity is not a paper finding, and never moves disposition).
"""
from __future__ import annotations

from .audit import SECURITY

# `sonnet`, for vision: this is located comparison — does the image match the parsed
# text — not open-ended scientific judgement, so the expensive model buys nothing extra.
ROLE_SPEC = {"role": "extraction auditor", "model": "sonnet", "tools": ("Read",)}

VERDICTS = ("MATCH", "MISMATCH", "UNSURE")

_RULES = """\
=== WHAT YOU ARE DOING ===
You are comparing a PARSER's output against the PAGE IMAGE it was parsed from, table by
table, plus the parsed TITLE against the page-1 image. Nothing else.

=== WHAT YOU MAY NOT DO ===
Do not judge whether the paper's results are correct, novel, or well supported. Do not
propose a scientific finding of any kind. You are checking EXTRACTION FIDELITY only —
whether the parser read the page right, never whether the paper is right.

=== VERDICTS ===
For each table: MATCH (the parsed table reflects the image, minor formatting differences
aside), MISMATCH (rows, columns, or numbers differ from the image), or UNSURE (the image
is unreadable, cropped wrong, or you genuinely cannot tell). Give short `issues` strings
for anything other than MATCH — quote what the image shows vs. what was parsed.

=== OUTPUT ===
Return EXACTLY one JSON object, no markdown fence, no prose around it:
{"tables": [{"table_idx": <int>, "verdict": "MATCH"|"MISMATCH"|"UNSURE",
             "issues": ["short phrase", ...]}, ...],
 "title_ok": <bool — does the parsed title match what page 1 shows>,
 "notes": "<=200 chars, only if something else about extraction looks wrong>"}
One entry per table listed below, in the same order, using its given `table_idx`. Never
invent a `table_idx` that is not listed below."""


def build(title: str, page1_image: str, tables: list[dict]) -> str:
    """One small prompt: the parsed title + page-1 image, then each qualifying table's
    parsed markdown next to its own page image.

    `tables` items are exactly `extraction_audit.build_prompt`'s rows:
    `{table_idx, page, image_path, markdown}`.
    """
    table_blocks = "\n\n".join(
        f"--- Table {t['table_idx']} (page {t['page']}) ---\n"
        f"Page image: {t['image_path'] or '(not rendered)'}\n"
        f"Parsed:\n{t['markdown'] or '(no rows)'}"
        for t in tables
    )
    return f"""{SECURITY}

Parsed title: {title or "(none recovered)"}
Page 1 image: {page1_image or "(not rendered)"}

{_RULES}

=== TABLES ===
{table_blocks or "(no qualifying tables)"}
"""


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.prompts.extraction_audit
    body = build("A Great Paper", "paper/pages/p1.png", [
        {"table_idx": 2, "page": 4, "image_path": "paper/pages/p4.png",
         "markdown": "| a | b |\n| --- | --- |\n| 1 | 2 |"},
    ])
    assert "A Great Paper" in body and "paper/pages/p1.png" in body
    assert "Table 2 (page 4)" in body and "paper/pages/p4.png" in body
    assert "| a | b |" in body
    assert '"title_ok"' in body and '"tables"' in body
    assert SECURITY in body

    empty = build("", "", [])
    assert "(none recovered)" in empty and "(not rendered)" in empty
    assert "(no qualifying tables)" in empty

    print("harness.prompts.extraction_audit self-check ok")
