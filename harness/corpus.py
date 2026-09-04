"""Every requested paper is accounted for, by name, in exactly one terminal state.
Pure: cases in, accounting out. No I/O, no Config, no model.

"5 papers reviewed" when six were requested is the one summary shape this module exists
to make impossible. It is not a cosmetic problem: a batch summary is what a reader uses
to decide whether the corpus is done, and a paper that silently disappears between the
request and the summary is a paper nobody knows to look at again.

The old `controller.summarize` was three dicts — `complete`, `waiting`, `errors` — keyed
on `paper_id or source`, built by three independent comprehensions over the cases. Nothing
required them to partition, nothing tied them back to what was ASKED for, and a case whose
status was none of those three (or whose `paper_id` collided with another's) was in no
bucket at all and appeared nowhere. `account()` is built the other way round: it walks the
REQUESTS, resolves each to exactly one state, and asserts the conservation law before
returning. A discrepancy raises here rather than printing a smaller number downstream.

`python -m harness.corpus` runs the self-check.
"""
from __future__ import annotations

from .artifacts import CORPUS_STATES, CaseState, CorpusEntry, CorpusReport

# CaseState.status -> the corpus state it terminates in. `pending` and `running` mean the
# loop stopped mid-flight without recording why, which is a harness defect rather than a
# fact about the paper — kept as its own state so it cannot hide inside `failed`.
_FROM_STATUS = {
    "complete": "completed",
    "waiting": "inconclusive",
    "error": "failed",
    "pending": "started",
    "running": "started",
}


def _state_for(case: CaseState | None) -> tuple[str, str]:
    """(corpus_state, reason). One request, one answer."""
    if case is None:
        return "requested", "no case was ever opened for this input"
    state = _FROM_STATUS.get(case.status, "started")
    if state == "completed":
        return state, case.verdict or "complete"
    return state, case.blocked_reason or f"status={case.status} at phase={case.phase}"


def account(requests: list[str], cases: list[CaseState | None]) -> CorpusReport:
    """The batch, request by request. `requests` is the authority on what was asked.

    `cases` is positional against `requests` — the same pairing `review_papers` already
    does — and a shorter or `None`-padded list is accounted for rather than dropped, so a
    crash that loses a case still leaves that paper visible as `requested`.
    """
    padded = list(cases) + [None] * max(0, len(requests) - len(cases))
    entries: list[CorpusEntry] = []
    for source, case in zip(requests, padded):
        state, reason = _state_for(case)
        entries.append(CorpusEntry(
            source=source, paper_id=(case.paper_id if case else ""), state=state,
            reason=reason, verdict=(case.verdict if case else ""),
            phase=(case.phase if case else ""),
            reproduction_class=(case.reproduction_class if case else ""),
            failure_kind=(case.failure_kind if case else ""),
            resume_after=(case.resume_after if case else ""),
            report_path=(case.report_path if case else ""),
        ))

    by_state = {s: [e.source for e in entries if e.state == s] for s in CORPUS_STATES}
    report = CorpusReport(
        requested=len(requests), entries=entries,
        counts={s: len(v) for s, v in by_state.items()},
        by_state=by_state,
        complete=all(e.state == "completed" for e in entries) and bool(entries),
    )
    # The conservation law, checked rather than trusted. If this ever fails, the fix is
    # in `_FROM_STATUS` or in `CORPUS_STATES` — never in the summary that prints it.
    total = sum(report.counts.values())
    assert total == report.requested, (
        f"corpus accounting lost {report.requested - total} of {report.requested} "
        f"paper(s): {report.counts}")
    report.summary = (
        f"{report.requested} requested · "
        + " · ".join(f"{n} {s}" for s, n in report.counts.items() if n))
    return report


if __name__ == "__main__":  # self-check: python -m harness.corpus
    done = CaseState(paper_id="a", source="a.pdf", status="complete", phase="done",
                     verdict="GREEN", report_path="reports/a.md")
    stuck = CaseState(paper_id="b", source="b.pdf", status="waiting", phase="audit",
                      blocked_reason="rate-limited", failure_kind="rate_limited",
                      resume_after="3:20pm")
    broken = CaseState(paper_id="c", source="c.pdf", status="error", phase="ingest",
                       blocked_reason="not a PDF", failure_kind="extraction_failed")
    mid = CaseState(paper_id="d", source="d.pdf", status="running", phase="probe")

    r = account(["a.pdf", "b.pdf", "c.pdf", "d.pdf"], [done, stuck, broken, mid])
    assert r.requested == 4 and sum(r.counts.values()) == 4, r.counts
    assert r.counts == {"requested": 0, "started": 1, "completed": 1, "failed": 1,
                        "inconclusive": 1}, r.counts
    assert not r.complete
    assert r.by_state["completed"] == ["a.pdf"]
    assert r.entries[1].failure_kind == "rate_limited" and r.entries[1].resume_after == "3:20pm"

    # A LOST case is still a listed paper. This is the whole point: nothing silently omitted.
    lost = account(["a.pdf", "z.pdf"], [done])
    assert lost.requested == 2 and lost.counts["requested"] == 1, lost.counts
    assert lost.entries[1].source == "z.pdf" and lost.entries[1].state == "requested"
    assert "no case was ever opened" in lost.entries[1].reason

    # An all-clear batch, and the degenerate empty one.
    assert account(["a.pdf"], [done]).complete
    assert not account([], []).complete, "an empty corpus is not a completed corpus"

    # Two cases that slugify to the same paper_id must both still be listed — the old
    # `paper_id`-keyed dicts silently collapsed them into one entry.
    twin = CaseState(paper_id="a", source="other/a.pdf", status="complete", verdict="RED")
    both = account(["a.pdf", "other/a.pdf"], [done, twin])
    assert both.requested == 2 and both.counts["completed"] == 2, both.counts
    print("corpus self-check OK")
