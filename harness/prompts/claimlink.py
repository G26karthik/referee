"""Ask a reader for the one correspondence the paper does not print.

`python -m harness.prompts.claimlink` runs the self-check.

**What this reader is for, and what it must not become.** A paper's abstract says "reduces
training memory by 40%". Its Table 3 says `40.2`. Nothing in the document says those are
the same claim, and measured over eight papers nothing deterministic recovers the link:
zero cross-references in any Abstract, one in any Conclusion, and none of the twenty-one
numbers headline sentences print occurs in exactly one recovered cell. So this reader is
asked for exactly that pairing and for nothing else.

It is deliberately the narrowest reading in the system. It does not judge the paper, does
not grade anything, does not look for defects, and is shown no finding, no severity and no
other reader's output — because a reader asked "what does this claim rest on" and "is this
paper any good" in the same breath will answer the second and pair evidence to fit it.

**Its output is a pair of addresses and the harness checks both.**
`harness/claimlink.py` re-mints the claim quotation, requires it to land in the Abstract or
the Conclusion, re-resolves the evidence address against the quotation given for it, and —
where both sides carry a number — re-derives the relationship. A pairing whose halves do
not both exist is refused and counted. Nothing this reader says about its own pairing is
read back.
"""
from __future__ import annotations

from .audit import SECURITY

# `haiku`: this is a location task, not a judgement task. What it needs is care with
# quotations, which is the one thing the harness re-checks in full, so the expensive model
# buys nothing here that the verification does not already guarantee. `tools` names
# `("Read",)` because that is what `claimlink_driver.link_confinement` actually grants —
# extraction garbles tables, and a reader quoting a cell verbatim needs to be able to check
# the printed page — kept equal to the runtime grant rather than merely documented, so this
# spec cannot drift from the confinement the way `audit.LENSES`'s model field once did.
ROLE_SPEC = {"role": "claim-link reader", "model": "haiku", "tools": ("Read",)}

_RULES = """\
=== WHAT YOU ARE DOING ===
You are reading ONE paper and answering ONE question, for each claim the paper makes about
itself in its Abstract and its Conclusion:

    WHICH printed number, table cell, figure or equation in this paper is that claim
    resting on?

That is the whole task. You are not reviewing the paper, not looking for problems, not
judging whether any claim is true, and not deciding how important anything is.

=== WHY THIS IS BEING ASKED OF A READER AND NOT COMPUTED ===
Papers do not write this down. An abstract states a result in prose — "reduces training
memory by 40%" — and does not add "see Table 3"; the 40% it prints is a rounding, a
rename, or a difference that appears in no cell. Measured over eight papers, no abstract
in any of them contained a single cross-reference. So the correspondence has to be read,
and you are the reader.

=== WHAT IS DONE WITH YOUR ANSWER ===
Every pairing you give is CHECKED before it is used, and you should assume it will be:

  * the claim sentence is searched for in the parsed paper. If it is not there verbatim,
    or occurs more than once, the pairing is dropped.
  * it must land inside the Abstract or the Conclusion. A body sentence is dropped.
  * the evidence address must exist, and the text you quote for it must be what is
    actually at that address.
  * if your claim sentence prints a number and the evidence carries one, the two are
    compared arithmetically, allowing for the rounding the claim itself prints. If they
    disagree, the pairing is dropped.

Nothing you write about whether your own pairing holds is read back. Quote exactly.

=== DO NOT USE THIS TO REPORT PROBLEMS ===
If you notice that a claim and its evidence DISAGREE, do not submit the pairing to make
the point. A disagreement is a finding, it belongs to a different reader under different
rules, and submitting it here gets it dropped as a mismatch and reported as nothing. Pair
each claim with the evidence it is ABOUT.

=== WHEN NOT TO ANSWER ===
Omit a claim rather than guess. A claim resting on something the extraction below does not
contain has no pairing, and an invented one is worse than a missing one: a missing pairing
leaves the claim unsupported, which is true, and an invented one makes a number look
load-bearing when nothing established that it is.

Several claims may rest on the same evidence, and one claim may rest on several pieces —
give one entry per (claim, evidence) pair.

=== ADDRESSES ===
    T<t>:r<r>:c<c>   a table cell, as printed in the tables below
    F<n>             a figure CAPTION
    E<n>             an equation
    p<N>             a sentence elsewhere in the paper (give the sentence verbatim)"""

_RETURN = """\
Print ONLY this JSON to standard output. Do not write any file yourself.

{"paper_claims_read": <how many distinct claims you examined>,
 "links": [
   {"claim_quote": "the claim sentence, VERBATIM from the Abstract or Conclusion",
    "evidence_ref": "T<t>:r<r>:c<c> | F<n> | E<n> | p<N>",
    "evidence_quote": "VERBATIM text at that address — the cell's contents, the caption, "
                      "the equation line, or the sentence",
    "rationale": "one line: why that evidence is what this claim is about"}
 ],
 "notes": "what you could not pair, and why"}"""


def build(title: str, abstract_text: str, conclusion_text: str, tables_text: str,
          figures_text: str = "", equations_text: str = "", numbers_text: str = "",
          pdf_path: str = "") -> str:
    """The claim-link prompt for one paper.

    Shown the Abstract and the Conclusion in full and the paper's addressable evidence —
    and NOT the body prose. That is a deliberate bound rather than a budget saving: the
    claims this reader may pair are exactly the ones in those two sections, and a reader
    given the whole paper tends to pair a headline claim with the body sentence that
    restates it, which is a paraphrase and not evidence.
    """
    return f"""{SECURITY}

You are reading ONE paper to locate what its headline claims rest on.

Paper: {title or "(title not detected)"}
{f"Original PDF (open it only to read a table or figure the extraction below garbled): {pdf_path}" if pdf_path else ""}

{_RULES}

=== THE PAPER'S ABSTRACT ===
{abstract_text or "(no abstract was recovered — if so, pair nothing from it)"}

=== THE PAPER'S CONCLUSION ===
{conclusion_text or "(no conclusion was recovered — if so, pair nothing from it)"}

=== TABLES (each cell addressed T<table>:r<row>:c<col>) ===
{tables_text or "(no tables extracted)"}

=== FIGURE CAPTIONS (addressed F<n>) ===
{figures_text or "(none extracted)"}

=== EQUATIONS (addressed E<n>) ===
{equations_text or "(none extracted)"}

=== NUMBERS THE PAPER REPORTS ELSEWHERE ===
{numbers_text or "(none extracted)"}

{_RETURN}"""


def _self_check() -> None:
    body = build("A Paper", "We reduce memory by 40%.", "Memory is reduced.",
                 "T1:r0:c1 = 40.2", pdf_path="/papers/a.pdf")
    assert "T<t>:r<r>:c<c>" in body and '"links"' in body
    assert "We reduce memory by 40%." in body and "Memory is reduced." in body
    # The three things this reader must not be asked to do, each stated in the prompt.
    for rule in ("not reviewing the paper", "DO NOT USE THIS TO REPORT PROBLEMS",
                 "Omit a claim rather than guess"):
        assert rule in body, rule
    # It is never shown a finding, a severity or another reader's output, and the
    # signature is what says so: there is no parameter that could carry one.
    import inspect
    params = set(inspect.signature(build).parameters)
    assert params == {"title", "abstract_text", "conclusion_text", "tables_text",
                      "figures_text", "equations_text", "numbers_text", "pdf_path"}, params
    for banned in ("severity", "finding", "FATAL", "verdict", "grade"):
        assert banned not in " ".join(params)
    assert ROLE_SPEC["tools"] == ("Read",), "must match link_confinement's actual grant"
    print("harness.prompts.claimlink self-check ok")


if __name__ == "__main__":
    _self_check()
