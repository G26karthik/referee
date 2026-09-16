"""One bounded reading for the focused-validation route: what the smallest experiment is.

`python -m harness.prompts.validation` runs the self-check.

**What the designer is for, and what it is not allowed to be.** The arms, the metric, the
benchmark and the addressed question all come off the document before this reader is
called. What a document does not print — and what no parser recovers, because it is not
there — is which VARIABLE the two arms differ in, which ones are held fixed, and what
observation would settle the question. Those are readings of the paper's method section,
and they are exactly the things a referee writes down when proposing an experiment.

**So the reader proposes and the harness relocates.** Every configuration value it offers
must come with the sentence it read the value from, and `validation_driver` re-mints that
sentence against the parsed paper: a value whose sentence does not resolve is dropped, and
an arm left with a `NEVER_ASSUMED` key this harness would have to fill makes the whole
design SPECIFICATION_BLOCKED. That is not a formality. An experiment whose optimizer,
split or schedule this review chose measures this review.

**The one thing it may never do is decide.** There is no field in which to say the paper
is wrong, no severity, no verdict, and no way to declare its own design conformant or its
own comparison settled — `state`, `conformance`, `authority`, `answers_question` and
`established` are stripped at the boundary. And the settlement condition it declares is
declared BEFORE anything runs, which is the only reason a controlled result can honestly
be said to have settled anything: a rule written after the numbers arrive settles whatever
its author wanted.

**Blocking is a correct and expected answer.** Most papers do not state enough to build a
one-variable contrast from, and a designer that invents the missing half to avoid an empty
answer produces an experiment nobody can hold against anything.
"""
from __future__ import annotations

from ..artifacts import NEVER_ASSUMED, PREDICTED_DIRECTIONS, SETTLEMENT_RULES
from .audit import SECURITY

# `sonnet`, and no tools. Naming the variable two arms differ in is a reading of two
# passages that are both printed below; it is not the kind of judgement the expensive
# model buys, and every part of the answer that could be got wrong silently — the
# quotations, the addresses, the conformance — is re-checked by the harness.
DESIGN_ROLE_SPEC = {"role": "focused validation designer", "model": "sonnet", "tools": ()}

_DISCIPLINE = """\
=== THE ONE RULE THAT SHAPES THIS TASK ===
Every scientific choice in the experiment must be one the PAPER states. You may not
supply an optimizer, a learning rate, a schedule, a split, an augmentation strength, a
preprocessing step, a threshold, a batch size, an epoch count or a seed policy because it
is the conventional one. If the paper does not state a choice the experiment needs, say
so and leave the design blocked.

A blocked design is a correct, complete and common answer. An experiment built on a value
this review chose measures this review's choice, not the authors' method, and it will be
refused by the checks below anyway.

The experiment must change EXACTLY ONE thing. An experiment in which two things move
discriminates between neither — which is the very complaint that raised this question."""


def design(title: str, question: str, question_quote: str, explanations_text: str,
           control_text: str, treatment_text: str, metric: str, benchmark: str,
           method_text: str, artifact_text: str = "") -> str:
    """Ask for the smallest one-variable contrast that would discriminate. No judgement."""
    return f"""{SECURITY}
You are proposing a FOCUSED VALIDATION EXPERIMENT for one open question about one paper.

{_DISCIPLINE}

=== THE PAPER ===
{title}

=== THE OPEN QUESTION ===
{question}

The question is anchored to this sentence of the paper, verbatim:
  "{question_quote}"

=== THE COMPETING EXPLANATIONS A RUN WOULD DISCRIMINATE BETWEEN ===
{explanations_text}

=== THE TWO ARMS THE PAPER ALREADY REPORTS ===
control:   {control_text}
treatment: {treatment_text}
metric:    {metric}
benchmark: {benchmark}

=== WHAT THE PAPER SAYS ABOUT HOW THESE ARMS WERE RUN ===
{method_text or "(no method text was recovered for these arms)"}

{("=== WHAT THE PINNED CHECKOUT SAYS ===" + chr(10) + artifact_text) if artifact_text else ""}

=== WHAT YOU ARE ASKED FOR ===
1. `changed_variable`: the ONE thing the experiment varies between the arms. One name.
2. `controlled_variables`: everything it holds fixed, by name.
3. `configuration`: for each arm, every scientific choice it fixes — and for EVERY key,
   the VERBATIM sentence of the paper (or line of the checkout) you read the value from.
   A value with no such sentence will be dropped, and a design missing a value it needs
   will be blocked. Do not supply a value you cannot quote.
4. `settlement`: what observation would settle the question, DECLARED NOW, before
   anything runs.
     rule: one of {" | ".join(SETTLEMENT_RULES)}
     predicted_direction: one of {" | ".join(PREDICTED_DIRECTIONS)} — which way the claim
       says the metric moves from control to treatment
     tolerance: required for EFFECT_EXCEEDS_TOLERANCE and
       EFFECT_WITHIN_EQUIVALENCE_MARGIN, in the metric's own unit
     tolerance_basis: where that number comes from — the paper's own stated effect size,
       its own reported variance, or its own reported seed spread. A tolerance you chose
       has no basis and the design will be blocked.

These are the choices you may NOT invent: {", ".join(NEVER_ASSUMED)}.

Nothing you write about whether your own design holds is read back. There is no field in
which to assert that the experiment is conformant, that the paper is wrong, or that any
question is settled, and a design offered with one is dropped.

Print ONLY this JSON to standard output. Do not write any file yourself.

{{"buildable": true,
 "changed_variable": "one name",
 "controlled_variables": ["name", "name"],
 "arms": {{"control":   {{"key": {{"value": "...", "quote": "VERBATIM from the paper"}}}},
          "treatment": {{"key": {{"value": "...", "quote": "VERBATIM from the paper"}}}}}},
 "settlement": {{"rule": "...", "predicted_direction": "...", "tolerance": null,
                "tolerance_basis": "", "statement": "one sentence a referee would read"}},
 "unstated": ["every choice the experiment needs that the paper does not state"],
 "notes": "what you could not determine, and why"}}

If the paper does not state enough, set "buildable" to false, list what is missing in
"unstated", and leave the rest empty."""


def _self_check() -> None:
    import inspect

    d = design("A Paper", "is the gain the regulariser or the schedule?",
               "LDReg improves accuracy by 2.1 points.",
               "1. the regulariser\n2. the longer schedule",
               "AugOnly (T1:r2:c3)", "LDReg (T1:r3:c3)", "top-1 accuracy", "CIFAR-100",
               "All models are trained for 200 epochs with SGD.")
    for rule in ("EXACTLY ONE thing", "A blocked design is a correct",
                 "VERBATIM sentence", "DECLARED NOW", "no such sentence will be dropped",
                 "Nothing you write about whether your own design holds is read back"):
        assert rule in d, rule
    for rule in SETTLEMENT_RULES:
        assert rule in d, rule
    for never in NEVER_ASSUMED:
        assert never in d, never
    assert "buildable" in d and '"unstated"' in d

    blocked = design("P", "q", "quote", "a\nb", "c", "t", "m", "b", "")
    assert "(no method text was recovered for these arms)" in blocked

    # THE DESIGNER CANNOT BE SHOWN THE REVIEW. The signature is the guarantee, exactly as
    # it is for the literature and claim-link readers.
    params = set(inspect.signature(design).parameters)
    assert params == {"title", "question", "question_quote", "explanations_text",
                      "control_text", "treatment_text", "metric", "benchmark",
                      "method_text", "artifact_text"}, params
    joined = " ".join(params)
    for banned in ("finding", "severity", "verdict", "grade", "outcome", "decision",
                   "conformance", "authority"):
        assert banned not in joined, banned
    assert DESIGN_ROLE_SPEC["tools"] == ()
    print("harness.prompts.validation self-check ok")


if __name__ == "__main__":
    _self_check()
