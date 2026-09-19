"""Two bounded readings for the prior-art route: what to search for, and what came back.

`python -m harness.prompts.literature` runs the self-check.

**Neither reader decides anything, and they are two readers rather than one for a reason.**
A single reader asked "search the literature and tell me whether this paper is novel" is
being asked for a verdict it cannot support: it would have to know what it did not find.
So the task is split at the only place a model is genuinely useful — proposing SEARCH
TERMS, which is a language problem, and reading whether one retrieved abstract overlaps
one addressed claim, which is a comparison between two texts that are both in front of it.

**What neither of them may see.** No finding, no severity, no grade, no target outcome, no
execution result, no other reader's conclusion, and no paper decision. The signatures are
what make that so: there is no parameter either `queries` or `compare` could receive one
through. A reader that knew the paper was already in trouble would find prior art for it.

**What the harness does with the answers.** Queries are issued verbatim and recorded. A
proposed relation is put through `literature.bind_prior_art`, which re-mints the target
claim, requires the quoted candidate passage to be present in the text THIS HARNESS
retrieved, and requires the candidate to predate the target's established cutoff. A
relation whose halves do not both hold is refused and counted, and nothing either reader
says about its own conclusion is read back.

**And the asymmetry is stated to the reader too**, because a reader that believes a clean
search means "novel" will pad its answers rather than return nothing. Returning nothing is
a correct and common answer here.
"""
from __future__ import annotations

from ..artifacts import LITERATURE_RELATIONS
from .audit import SECURITY

# Two tiers, not one. Proposing search terms from a closed 6-family list is a language
# task — rewriting a claim sentence into a handful of keyword strings — and the harness
# re-checks the only things that could be got wrong silently (the addresses, the passages,
# the dates), so `haiku` buys nothing less here than `sonnet` did. Comparing a retrieved
# abstract against an addressed claim is real reading comprehension — does this overlap
# undermine novelty — so it keeps the stronger tier.
QUERY_ROLE_SPEC = {"role": "literature query proposer", "model": "haiku", "tools": ()}
REVIEW_ROLE_SPEC = {"role": "literature reviewer", "model": "sonnet", "tools": ()}

# The query families the proposer may use. Closed, because an open one turns a bounded
# protocol into whatever the model felt like trying, and "the declared protocol completed"
# would then mean nothing.
QUERY_FAMILIES = ("EXACT_METHOD_NAME", "ACRONYM_EXPANSION", "CONTRIBUTION_DECOMPOSITION",
                  "TASK_AND_MECHANISM", "BENCHMARK_AND_MECHANISM", "TERMINOLOGY_VARIANT")

_ASYMMETRY = """\
=== THE ONE RULE THAT SHAPES THIS TASK ===
Finding earlier work may raise a prior-art concern. FAILING to find earlier work
establishes NOTHING. It does not mean the contribution is new; it means a bounded search
did not surface a match, and the literature this search did not reach is not enumerable.

Returning nothing is a correct, complete and common answer. Do not pad. A weak match
offered to avoid an empty answer costs a human referee real time and teaches this system
nothing, and it will be refused by the checks below anyway."""


def queries(title: str, claim_quote: str, concepts_text: str,
            method_names_text: str = "") -> str:
    """Ask for bounded search queries for ONE addressed claim. Terms only, no judgement."""
    return f"""{SECURITY}

You are proposing SEARCH QUERIES for one claim in one paper. You are not searching, not
judging the paper, and not deciding anything about it.

Paper: {title or "(title not detected)"}

=== THE CLAIM BEING SEARCHED FOR ===
{claim_quote}

=== TERMS ALREADY EXTRACTED FROM THAT SENTENCE ===
{concepts_text or "(none)"}

=== NAMES THIS PAPER GIVES ITS OWN CONTRIBUTION ===
{method_names_text or "(none detected)"}

{_ASYMMETRY}

=== WHAT A GOOD QUERY IS HERE ===
A query that would surface EARLIER work making a similar contribution. Not a query that
would surface this paper. Keep each query short enough for a bibliographic index —
these go to OpenAlex, Crossref and arXiv, not to a chat model. No boolean operators, no
quotation marks, no field prefixes.

Use these families and no others, at most one query per family:
{chr(10).join("    " + f for f in QUERY_FAMILIES)}

Avoid near-duplicate rewordings. Six slightly different spellings of one idea is one
query's worth of coverage and six queries' worth of budget.

Print ONLY this JSON to standard output. Do not write any file yourself.

{{"queries": [{{"family": "one of the families above", "query": "the query text"}}],
 "notes": "anything you could not turn into a query"}}"""


def compare(title: str, claim_quote: str, claim_ref: str, cutoff_text: str,
            candidates_text: str) -> str:
    """Ask whether any retrieved candidate materially overlaps ONE addressed claim.

    Shown the claim, the target's cutoff date and the candidates' own metadata and
    abstracts — and nothing about the review. There is no parameter here through which a
    finding, a severity, a verdict or another reader's answer could arrive.
    """
    return f"""{SECURITY}

You are comparing ONE claim from ONE paper against a list of EARLIER published works that
a bibliographic search returned. You are not reviewing the paper, not assigning any
severity, and not deciding anything about it. Your answer is a proposal that will be
checked.

Paper under review: {title or "(title not detected)"}
This paper became public: {cutoff_text or "(date not established)"}

=== THE CLAIM ({claim_ref}) ===
{claim_quote}

{_ASYMMETRY}

=== WHAT YOU ARE DECIDING, PER CANDIDATE ===
Choose exactly one of these for any candidate you report:

    LIKELY_DIRECT_PREDECESSOR       the earlier work appears to make the same specific
                                    contribution this claim asserts
    POSSIBLE_PRIORITY_CONFLICT      the claim asserts being FIRST at something, and the
                                    earlier work appears to state that same narrow thing
    POSSIBLE_OMITTED_BASELINE       the earlier work is a comparison this paper's claim
                                    would need and does not appear to make
    RELATED_BUT_MATERIALLY_DIFFERENT  same area, genuinely different contribution
    TERMINOLOGY_COLLISION_ONLY      the same words, a different idea
    INSUFFICIENT_EVIDENCE           the abstract does not say enough to tell

The last three are real answers and are frequently the right one. Report a candidate under
any of them only when it is worth a human's glance; otherwise omit it.

=== WHAT IS DONE WITH YOUR ANSWER ===
Every pairing is checked before it counts, and you should assume it will be:

  * the claim sentence is re-searched in the parsed paper, and must resolve to the same
    address it was searched under;
  * the sentence must actually claim novelty, priority or a contribution;
  * the candidate must carry a DOI, an arXiv id or an index id — a title is not a work;
  * THE PASSAGE YOU QUOTE MUST BE PRESENT, VERBATIM, IN THE CANDIDATE TEXT SHOWN BELOW.
    Do not quote from memory, do not paraphrase, and do not quote a work you know of that
    is not in this list;
  * the candidate must be public BEFORE the date printed above. A later work is not prior
    art however relevant it is;
  * even when all of that holds, the OVERLAP you assert remains recorded as YOUR READING.
    It is reported to a human as a question, never as a demonstrated fact about the paper.

Nothing you write about whether your own pairing holds is read back. There is no field in
which to assert that novelty fails, and a pairing offered with one is dropped.

=== EARLIER CANDIDATE WORKS ===
{candidates_text or "(no candidates were retrieved for this claim)"}

Print ONLY this JSON to standard output. Do not write any file yourself.

{{"candidates_read": <how many you examined>,
 "matches": [
   {{"candidate_id": "the id printed beside the candidate above",
     "relation": "one of the six values above",
     "candidate_quote": "VERBATIM from that candidate's abstract as printed above",
     "overlap": "one or two sentences: what specifically overlaps",
     "binding_basis": "named_method_lineage | explicit_first_claim_met | "
                      "identical_identifier | reviewer_reading"}}
 ],
 "notes": "what you could not judge, and why"}}"""


def _self_check() -> None:
    import inspect

    q = queries("A Paper", "We are the first to do X.", "optical flow, end-to-end", "FlowNet")
    assert "EXACT_METHOD_NAME" in q and "We are the first to do X." in q
    assert "establishes NOTHING" in q and "Returning nothing is" in q

    c = compare("A Paper", "We are the first to do X.", "P0:0-24", "2024-06-01",
                "[c1] Earlier Work (2013) doi:10.1/x\n  abstract: We do X.")
    for rule in ("MUST BE PRESENT, VERBATIM", "A later work is not prior",
                 "remains recorded as YOUR READING", "There is no field in"):
        assert rule in c, rule
    # Every relation the harness knows is offered, and nothing else is.
    for relation in LITERATURE_RELATIONS:
        assert relation in c, relation
    assert set(QUERY_FAMILIES) == set(
        __import__("harness.literature", fromlist=["x"]).QUERY_FAMILIES)

    # NEITHER READER CAN BE SHOWN THE REVIEW. The signatures are the guarantee.
    for fn, allowed in ((queries, {"title", "claim_quote", "concepts_text",
                                   "method_names_text"}),
                        (compare, {"title", "claim_quote", "claim_ref", "cutoff_text",
                                   "candidates_text"})):
        params = set(inspect.signature(fn).parameters)
        assert params == allowed, (fn.__name__, params)
        joined = " ".join(params)
        for banned in ("finding", "severity", "verdict", "grade", "outcome", "decision",
                       "novel"):
            assert banned not in joined, (fn.__name__, banned)
    assert QUERY_ROLE_SPEC["tools"] == () and REVIEW_ROLE_SPEC["tools"] == ()
    print("harness.prompts.literature self-check ok")


if __name__ == "__main__":
    _self_check()
