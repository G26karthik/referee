"""Can this paper be reconstructed well enough to test, when the authors published no code?

THE DEFECT THIS EXISTS TO FIX. A real run on a real published paper with no advertised
repository ended: no repository → a standalone scaffold that raises NotImplementedError →
a generic placebo → nothing. Three of those four steps are honest; the sequence is not,
because it never ASKED the question a research auditor is supposed to ask. "The authors
published no code" is not the end of an investigation. "The paper does not say enough to
rebuild the experiment" is — but it is a finding about the paper, and it has to be
established rather than assumed.

WHAT THIS IS. A pure, deterministic assessment over the parsed document: which
ingredients of a reimplementation the paper actually supplies, each with a locator a
reader can check, and — when the answer is no — exactly which ones are missing. No model
call, no network, no filesystem. It decides ELIGIBILITY only. It does not write an
implementation, and nothing here can conclude anything about the paper's claims.

WHAT IT DELIBERATELY DOES NOT DO. It never infers a missing ingredient from a present
one, and it never fills a gap with a plausible default. A paper that omits its optimizer
does not get Adam; a paper that omits its dataset does not get the obvious one. That is
the difference between reconstructing a paper and writing a new one that resembles it,
and the whole value of PATH B collapses the moment those blur. Insufficient evidence
yields NOT_VERIFIED with the gaps named — an outcome this system is allowed to reach.

ponytail: keyword and structure detection over the parsed text, not a parser and not a model.
Ceiling: a paper that describes its method only in prose the vocabulary below does not
match reads as under-specified, which is the conservative direction (it refuses a
reimplementation it might have been able to attempt). Upgrade path when that starts
costing real papers: hand `INGREDIENTS` to the reviewer as a checklist and let a lens
answer it with quotes, through the same accept channel every other phase already uses.

`python -m harness.reimplement` runs the self-check.
"""
from __future__ import annotations

import re

from .artifacts import PaperDoc, ReimplementationReadiness, ReimplementationIngredient

# What a reimplementation needs, and the evidence that counts as supplying it.
#
# REQUIRED is not a style preference — each entry is something you cannot write the
# program without. Drop any one and the thing you build is no longer this paper's
# experiment: with no method you have not implemented it, with no training procedure you
# cannot run it, with no dataset you cannot feed it, with no metric you cannot score it,
# and with no printed target there is nothing to compare against and therefore no
# experiment at all — only a demo.
#
# `hyperparameters` is OPTIONAL on purpose. Papers routinely omit a batch size while
# fully specifying the method, and refusing those would refuse most of the literature.
# It is reported either way, because its absence is exactly what a reader needs in order
# to judge how much interpretation any resulting number carries.
INGREDIENTS: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    ("method", True, ("algorithm", "pseudocode", "we define", "is defined as", "objective",
                      "loss function", "our method", "we propose", "formulation")),
    ("architecture", False, ("architecture", "layer", "encoder", "decoder", "hidden",
                             "embedding", "network", "backbone", "kernel", "estimator")),
    ("preprocessing", False, ("preprocess", "normali", "augment", "tokeni", "resize",
                              "standardi", "cleaning", "filtering")),
    # CONCRETE PROCEDURE ONLY. "We train the model" is not a training procedure — it is
    # the assertion that one exists. An earlier version accepted "we train"/"trained
    # for"/"fine-tun"/"converge" and duly pronounced a real paper reimplementable whose
    # own protocol lens had just found, correctly, that it discloses no optimizer, no
    # learning rate and no epoch count. Eligibility that can be satisfied by the verb
    # alone would send an implementer off to invent the very thing the paper omitted,
    # which is the one failure PATH B exists to prevent.
    ("training", True, ("optimizer", "adam", "sgd", "rmsprop", "adagrad", "learning rate",
                        "epoch", "batch size", "weight decay", "momentum", "lr=",
                        "iterations", "training steps")),
    ("dataset", True, ("dataset", "corpus", "benchmark", "we evaluate on", "test set",
                       "training set", "samples", "records")),
    ("metric", True, ("accuracy", "precision", "recall", "f1", "f-score", "auc", "bleu",
                      "perplexity", "error rate", "mse", "rmse", "map", "iou", "dice")),
    ("hyperparameters", False, ("learning rate", "batch size", "weight decay", "dropout",
                                "momentum", "temperature", "hidden size", "num_layers",
                                "seed")),
)

# A display equation or an "Algorithm 1" block is stronger evidence of a specified method
# than any prose keyword, so either satisfies `method` outright.
_ALGORITHM_BLOCK = re.compile(r"\balgorithm\s+\d+\b", re.I)

_MAX_QUOTE = 200


def _find(needles: tuple[str, ...], doc: PaperDoc) -> tuple[str, str]:
    """First (locator, quote) in the paper matching any needle, or ("", "").

    Returns the surrounding sentence rather than the needle so the quote is checkable by
    a human against the section it names, which is the only reason to record it at all.
    """
    for s in doc.sections:
        low = s.text.lower()
        for n in needles:
            i = low.find(n)
            if i < 0:
                continue
            start = max(0, low.rfind(".", 0, i) + 1)
            end = low.find(".", i)
            end = len(s.text) if end < 0 else end + 1
            quote = " ".join(s.text[start:end].split())[:_MAX_QUOTE]
            return f"s{s.section_idx}", quote
    return "", ""


def assess(doc: PaperDoc) -> ReimplementationReadiness:
    """Is there enough in this paper to rebuild the experiment independently?

    Pure. Every ingredient carries the locator it was found at, so the judgement is
    auditable rather than asserted, and `missing` names what a reader would have to
    supply — which is the useful half when the answer is no.
    """
    found: list[ReimplementationIngredient] = []
    for name, required, needles in INGREDIENTS:
        ref, quote = _find(needles, doc)
        if name == "method" and not ref:
            # An equation or a numbered algorithm block outranks prose for this one.
            if doc.equations:
                e = doc.equations[0]
                ref, quote = f"E{getattr(e, 'number', 1)}", (getattr(e, "body", "") or "")[:_MAX_QUOTE]
            else:
                for s in doc.sections:
                    if _ALGORITHM_BLOCK.search(s.text):
                        ref = f"s{s.section_idx}"
                        quote = " ".join(s.text.split())[:_MAX_QUOTE]
                        break
        found.append(ReimplementationIngredient(
            kind=name, required=required, present=bool(ref), ref=ref, quote=quote))

    # A target to compare against is an ingredient too, and the one most often absent
    # from a paper that otherwise reads as fully specified: without an ADDRESSED cell
    # there is nothing to reconcile a measurement with, so any run would produce a number
    # in a vacuum. Tables, not prose — the same rule the provenance ceiling already keeps.
    has_target = any(t.rows for t in doc.tables)
    found.append(ReimplementationIngredient(
        kind="comparison_target", required=True, present=has_target,
        ref=f"T{doc.tables[0].table_idx}" if has_target and doc.tables else "",
        quote=(doc.tables[0].caption[:_MAX_QUOTE] if has_target and doc.tables else "")))

    missing = [i.kind for i in found if i.required and not i.present]
    established = not missing
    if established:
        reason = ("every required ingredient is present in the paper, so an independent "
                  "reimplementation can be attempted and compared against a printed cell")
    else:
        reason = ("the paper does not supply " + ", ".join(missing) +
                  f" — an implementation would have to invent "
                  f"{'that' if len(missing) == 1 else 'those'}, which would make any result a "
                  f"statement about the invention rather than about the paper")
    return ReimplementationReadiness(
        established=established, ingredients=found, missing=missing, reason=reason)


if __name__ == "__main__":  # self-check: python -m harness.reimplement
    from .artifacts import Section, Table

    def _doc(text: str, tables: bool = True) -> PaperDoc:
        return PaperDoc(
            paper_id="p", sections=[Section(section_idx=0, title="M", text=text)],
            tables=[Table(table_idx=0, rows=[["a", "1.0"]])] if tables else [])

    full = ("We propose a method. The objective is defined as a sum. We train with the Adam "
            "optimizer for 30 epochs at a learning rate of 1e-3 on the CIFAR-100 dataset and "
            "report accuracy on the test set.")
    r = assess(_doc(full))
    assert r.established and not r.missing, r.missing
    assert {i.kind for i in r.ingredients if i.present} >= {"method", "training", "dataset", "metric"}
    assert all(i.ref for i in r.ingredients if i.present), "a present ingredient must be locatable"

    # Each required ingredient, removed one at a time, must be REPORTED missing rather
    # than inferred from the others.
    no_train = assess(_doc("We propose a method with an objective on the CIFAR-100 dataset "
                           "and report accuracy."))
    assert not no_train.established and "training" in no_train.missing, no_train.missing
    no_data = assess(_doc("We propose a method. We train with Adam for 30 epochs. Accuracy."))
    assert "dataset" in no_data.missing, no_data.missing

    # No printed cell to compare against ⇒ not eligible, however well specified.
    no_cell = assess(_doc(full, tables=False))
    assert not no_cell.established and "comparison_target" in no_cell.missing, no_cell.missing

    # A paper that says nothing usable must refuse, and must name what it lacked.
    empty = assess(_doc("This paper is about eyes.", tables=False))
    assert not empty.established and len(empty.missing) >= 3
    assert "does not supply" in empty.reason

    # Purity: same input, same answer, and the document is not mutated.
    d = _doc(full)
    before = d.model_dump_json()
    assert assess(d).model_dump() == assess(d).model_dump()
    assert d.model_dump_json() == before

    print("reimplement self-check OK")
