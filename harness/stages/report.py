"""S4 — rank the findings and render the evaluation report.

Two hard rules, both inherited from what the research pipeline learned:

  1. NO LLM DECIDES THE VERDICT. Severity ordering and the binary RED/GREEN call are
     a lexicographic sort and a materiality table in plain Python. A model that ranks
     its own findings will rank them differently on Tuesday; an editor needs the same
     paper to get the same verdict every time, and needs to be able to argue with the
     rule rather than with a mood.

  2. THE RENDERER COPIES, IT DOES NOT WRITE. `render_eval_report` is a pure function
     over the artifacts — no LLM, no network, no Config. Every number and every quote
     in the output was already in a `Finding`, put there by a lens that had to quote
     it from the paper. Nothing is generated at render time, so nothing can be
     hallucinated at render time.

`python -m harness.stages.report` runs the self-check.
"""
from __future__ import annotations

import re
from collections import Counter

from .. import selfaudit, state
from ..artifacts import (CodeAudit, CodeAuditFinding, EvalReport, ExperimentalChain, Finding,
                         LensReport, PaperDoc, ProbeResult, Reconciliation, RepoAcquisition)
from ..config import Config
from . import audit as audit_stage

# Severity → rank. Also the display order of the findings table. NOTE ranks below
# MINOR and, deliberately, is not one of the keys `overall_verdict` sums over — a NOTE
# can be displayed but can never cross a threshold (see `harness.artifacts.SEVERITIES`).
_SEVERITY_RANK = {"FATAL": 2, "MAJOR": 1, "MINOR": 0, "NOTE": -1}

# Tiebreak only, applied AFTER severity and evidence strength. Lenses that cite the
# paper against itself sort above lenses that argue about methodology, because an
# editor can check the first kind in seconds.
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}

# The materiality table. Stated as data so the rule is inspectable and arguable, exactly
# as the RED/YELLOW/GREEN threshold table it replaces was.
#
# WHAT CHANGED, AND WHY COUNTING HAD TO GO. The old table reached RED by ACCUMULATION:
# three MAJORs from one lens, or ten across all four. That makes the paper-level decision
# a function of how many things a panel of readers chose to write down, which is a
# property of the panel, not of the paper. Two lenses that happen to phrase the same
# doubt separately move a verdict; one that phrases two doubts together does not. There
# is no scale on which "10 MAJORs" is a discovery and "9" is not.
#
# The decision is now binary and definitional, not cumulative:
#
#   RED   = a scientifically material failure has been ESTABLISHED, by evidence strong
#           enough to REJECT the relevant claim.
#   GREEN = no such failure has been established WITHIN THE AUDITED SCOPE.
#
# GREEN is therefore not a certificate. It does not mean the paper is correct, it means
# this audit did not establish that it is wrong — which is why `claim_status` keeps
# VERIFIED_SUPPORT and NOT_VERIFIED apart underneath, and why the report prints the
# reproduction status beside the colour instead of folding it in.
#
# Only two things clear the bar, and neither is a count:
#
#   1. A failed reproduction, from a provenance the ceiling admits. Arithmetic against
#      a printed cell, not a judgement.
#   2. A finding whose COUNTED severity is FATAL — which the prompts define as "if true,
#      the paper's central claim does not stand", i.e. rejection, definitionally.
#
# MAJOR is deliberately absent. Its own definition is "materially weakens a headline
# claim" — that is a concern, and a concern is not a rejection however many of them
# there are. They are printed under `## Material concerns` and they move no colour.
MATERIAL_SEVERITY = ("FATAL",)
CONCERN_SEVERITY = ("MAJOR",)

# The internal epistemic states. RED/GREEN is a projection of these, not a replacement
# for them: only VERIFIED_FAILURE is RED, and BOTH of the other two are GREEN, because
# "we checked and it held" and "we could not check" are the same decision about the
# paper even though they are opposite states of knowledge. The report must never let
# them look alike, so it prints this field verbatim.
CLAIM_STATUSES = ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED")

# The provenance ceiling, enforced a SECOND time here. `local_exec.reconcile` already
# refuses to emit a verdict for anything outside this set, so in a correct system no
# inadmissible reconciliation ever reaches this module — which is precisely why the check
# belongs here too. It was not here, and a `FAILED_REPRODUCTION` carrying `synthesized`,
# `template` or a garbage provenance returned RED: a paper-independent placebo, or a
# hand-edited probe_results.json, convicting a paper. The old code even detected the case
# and printed "treat it as a harness defect" while still returning the conviction, so the
# report contradicted its own verdict. One upstream bug was all that stood between a
# diagnostic and a published RED.
ADMISSIBLE_REPRODUCTION_PROVENANCE = ("driver", "repo_exec")

# What actually ran, in the vocabulary a reader needs rather than the internal token.
# Fails closed: anything unrecognised reads as a diagnostic, never as author code, so a
# provenance this map has not been taught about cannot be reported as a reproduction.
PROVENANCE_LABEL = {
    "repo_exec": "AUTHOR_REPOSITORY",
    "driver": "INDEPENDENT_REIMPLEMENTATION",
    "synthesized": "SYNTHESIZED_DIAGNOSTIC",
    "template": "SYNTHESIZED_DIAGNOSTIC",
}

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
# Salvaged from the retired grounding tournament: pull the leading magnitude out of
# free text an LLM wrote ("+3-5% top-1", "0.8pp", "~2.5 points") and DEGRADE TO 0.0
# on anything unparseable. Never raises — a bad string must sort last, never invert
# the order and never sink the report.
_MAGNITUDE = re.compile(r"[-+]?\d*\.?\d+")

MAX_TABLE_ROWS = 14
MAX_THREAT_BULLETS = 6
_QUOTE_CHARS = 240

# The two sentences a reader most often gets wrong, written once so every report says
# them the same way. Both spell out what the state does NOT mean, because that is the
# half that gets dropped when a colour is quoted out of context.
_DECISION_GLOSS = {
    "VERIFIED_FAILURE": "A material failure was **established**: the evidence below is strong "
                        "enough to reject the claim it addresses. This is not a count of concerns.",
    "VERIFIED_SUPPORT": "Something was **positively checked and held** — an executed metric "
                        "reconciled with a printed cell. That is support for the cell that was "
                        "tested, not for the paper as a whole.",
    "NOT_VERIFIED": "**Nothing was established in either direction** within the audited scope. "
                    "This is not a finding that the paper is correct, and it is not a finding "
                    "that it is wrong — it is the honest state when the evidence available did "
                    "not settle the question.",
}
_REPRO_GLOSS = {
    "REPRODUCED": "An experiment ran and its metric reconciled with the paper's printed value.",
    "FAILED_REPRODUCTION": "An experiment ran, started successfully, and its metric did not "
                           "reconcile with the paper's printed value.",
    "NOT_VERIFIED": "An experiment was attempted and settled nothing — see the chain below for "
                    "the link that broke. An infrastructure or capability limit never counts "
                    "against the paper.",
    "NOT_ATTEMPTED": "No reproduction was attempted, so no experimental evidence stands behind "
                     "this decision either way.",
}


def parse_magnitude(text: str) -> float:
    if not text:
        return 0.0
    m = _MAGNITUDE.search(str(text))
    if not m:
        return 0.0
    try:
        return abs(float(m.group()))
    except (TypeError, ValueError):
        return 0.0


def counted(f: Finding) -> str:
    """The severity `overall_verdict` actually counts for `f`.

    Falls back to the lens's own `severity` whenever `counted_severity` is empty —
    which is exactly what makes turning grading off (or never running it) reproduce
    the pre-grading verdict byte for byte: nothing capped it, so there is nothing to
    fall back FROM. See `harness/grading.py:derive` for what can set it, and its one
    safety property (`RANK[counted_severity] <= RANK[severity]` always) for why this
    fallback can never be a promotion in disguise.
    """
    return f.counted_severity or f.severity


def finding_key(f: Finding) -> tuple:
    """Lexicographic sort key, most-severe-first under `reverse=True`.

    Order: COUNTED severity (so display order matches what the verdict actually used),
    then evidence strength (a cited table cell beats a page reference beats nothing),
    then whether a reproduction could settle it, then the lens tiebreak, then the id so
    the sort is total and therefore reproducible.
    """
    ref = (f.evidence_ref or "").strip()
    evidence = 2 if _CELL_REF.match(ref) else (1 if ref else 0)
    return (
        _SEVERITY_RANK.get(counted(f), 0),
        evidence,
        1 if f.verifiable_by_experiment else 0,
        _LENS_RANK.get(f.lens, 0),
        f.finding_id,
    )


def rank(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=finding_key, reverse=True)


def material_failures(findings: list[Finding]) -> list[Finding]:
    """The findings that REJECT a claim, as opposed to weakening one.

    COUNTED severity, never the lens's raw assertion — so everything `harness.grading`
    already enforces is inherited here rather than re-litigated: CANDIDATE_CAP (only a
    CONFIRMED_FINDING may reach FATAL), the confidence ceiling, and the evidence ceiling
    that bounds confidence by what the citation actually is. A counted FATAL is therefore
    already "a confirmed finding, at high confidence, on evidence the harness re-verified
    against the paper" before this function sees it.

    No SEPARATE precondition is layered on top — notably not "and it must have been
    graded". That would do nothing when grading is on and silently suppress every RED
    when it is off (the default), breaking the one guarantee the grading subsystem
    exists to keep: turning grading off reproduces the ungraded verdict exactly.
    """
    return [f for f in findings if counted(f) in MATERIAL_SEVERITY]


def material_concerns(findings: list[Finding]) -> list[Finding]:
    """Counted MAJORs: real, verified, and NOT sufficient to reject a claim. Printed
    prominently, counted toward nothing. See MATERIAL_SEVERITY for why."""
    return [f for f in findings if counted(f) in CONCERN_SEVERITY]


def claim_status(findings: list[Finding],
                 reconciliation: Reconciliation | None = None) -> tuple[str, str]:
    """The epistemic state underneath the colour: what this audit ESTABLISHED.

    Three states, and the distinction the binary decision cannot carry on its own:

      VERIFIED_FAILURE  a material failure was established — a reproduction that failed
                        from an admissible provenance, or a counted-FATAL finding.
      VERIFIED_SUPPORT  something was positively checked and held: a reproduction that
                        reconciled against the printed cell.
      NOT_VERIFIED      nothing was established in either direction. The honest state
                        for most papers, and the one a research auditor is allowed to
                        report rather than resolving it into a fabricated conclusion.

    INCONCLUSIVE is deliberately NOT a failure. A missing dataset, a units mismatch, an
    unparsed metric, a shut execution gate and an 8 GiB card facing a 24 GiB demand all
    land in NOT_VERIFIED, and none of them is evidence against a paper.
    """
    if (reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION"
            and reconciliation.provenance in ADMISSIBLE_REPRODUCTION_PROVENANCE):
        return "VERIFIED_FAILURE", "a reproduction attempt failed against the printed cell"
    fatal = material_failures(findings)
    if fatal:
        return "VERIFIED_FAILURE", f"{len(fatal)} finding(s) counted FATAL"
    if reconciliation is not None and reconciliation.status == "RESOLVED_VERIFIED":
        return "VERIFIED_SUPPORT", "an executed metric reconciled with the printed cell"
    return "NOT_VERIFIED", "no material failure established, and nothing positively reproduced"


def reproduction_status(probe: ProbeResult | None) -> str:
    """REPRODUCED | FAILED_REPRODUCTION | NOT_VERIFIED | NOT_ATTEMPTED — reported beside
    the verdict, never folded into it.

    NOT_ATTEMPTED and NOT_VERIFIED are kept apart on purpose: "no experiment was run"
    and "an experiment ran and settled nothing" are different facts about the audit, and
    collapsing them is how a report starts implying it tried harder than it did.
    """
    if probe is None:
        return "NOT_ATTEMPTED"
    rec = probe.reconciliation
    if rec is None or rec.status == "NOT_ATTEMPTED":
        return "NOT_ATTEMPTED"
    if rec.status == "RESOLVED_VERIFIED":
        return "REPRODUCED"
    if rec.status == "FAILED_REPRODUCTION":
        return "FAILED_REPRODUCTION"
    return "NOT_VERIFIED"


def provenance_label(provenance: str) -> str:
    """Internal provenance token → the reader-facing vocabulary. Fails closed to
    SYNTHESIZED_DIAGNOSTIC, so an unrecognised token can never be reported as the
    authors' own code."""
    return PROVENANCE_LABEL.get(provenance, "SYNTHESIZED_DIAGNOSTIC")


def overall_verdict(findings: list[Finding],
                    reconciliation: Reconciliation | None = None) -> tuple[str, str]:
    """The binary paper-level decision, plus the rule that produced it.

    RED iff `claim_status` is VERIFIED_FAILURE. Nothing else — no count, no accumulation,
    no second mechanism. See MATERIAL_SEVERITY for why counting was removed, and
    `claim_status` for the states this projects from.
    """
    if reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION":
        if reconciliation.provenance not in ADMISSIBLE_REPRODUCTION_PROVENANCE:
            # The ceiling, held at the verdict gate as well as at the reconciler. Reaching
            # here means an upstream bug or an edited artifact; either way a program that
            # is not entitled to reconcile a printed cell does not get to convict a paper,
            # so this reports the harness defect INSTEAD of the conviction rather than
            # alongside it.
            return "GREEN", (
                f"A reconciliation reported FAILED_REPRODUCTION with provenance "
                f"'{reconciliation.provenance or '(none)'}', which the provenance ceiling "
                f"does not admit as a reproduction of a printed cell. That status should "
                f"have been unreachable, so it is treated as a harness defect and NOT as "
                f"evidence about the paper. Nothing here counts against the authors.")
        where = f" at {reconciliation.table_ref}" if reconciliation.table_ref else ""
        # WHO ran decides what the failure means, so the sentence is derived from
        # provenance rather than asserted. Only `repo_exec` is the authors' own checkout;
        # this used to claim "the paper's own code does not reproduce the number it prints"
        # for a `driver` script the operator wrote — which the harness's own vocabulary
        # defines as NOT the authors' code, and which `authorize` reports as
        # `not_repo_execution`. Attributing our script's failure to them is the one
        # accusation this system must never make by accident.
        if reconciliation.provenance == "repo_exec":
            blame = ("The audited repository's own code does not reproduce the number it "
                     "prints, so the central claim does not stand on the evidence the "
                     "authors supplied.")
        elif reconciliation.provenance == "driver":
            # `driver` is a human-written faithful reproduction, which the provenance ceiling
            # deliberately admits in BOTH directions — so this verdict stands, and the
            # sentence has to say what stands rather than disclaim it. The first version of
            # this fix said "not established as a failure of the paper's code" while still
            # returning RED, so the report contradicted its own verdict.
            blame = ("The program that ran was a human-written reproduction of the paper's "
                     "method, not the authors' checkout. Whether it is faithful is not "
                     "machine-checked, so read the script before relying on this verdict.")
        else:
            blame = (f"The program that ran was '{reconciliation.provenance}', which the "
                     f"provenance ceiling does not admit as a reproduction of a printed cell, "
                     f"so this status should not have been reachable — treat it as a harness "
                     f"defect rather than as evidence about the paper.")
        return "RED", f"Failed code reproduction{where}: {reconciliation.reason} {blame}"

    # COUNTED severity, not the lens's raw assertion — see `counted()` and
    # `material_failures()`. Grading can only soften what reaches here; nothing can
    # promote into it.
    fatal = material_failures(findings)
    concerns = material_concerns(findings)
    if fatal:
        lenses = ", ".join(sorted({f.lens for f in fatal}))
        return "RED", (f"{len(fatal)} finding(s) counted FATAL ({lenses}): a central claim does "
                       f"not stand as argued, on evidence the harness re-verified against the "
                       f"paper.")
    n_minor = sum(1 for f in findings if counted(f) == "MINOR")
    if concerns:
        return "GREEN", (f"No material failure established. {len(concerns)} MAJOR concern(s) and "
                         f"{n_minor} MINOR were recorded and are printed in full — a concern "
                         f"weakens a claim, it does not reject one, and no number of them "
                         f"accumulates into a rejection.")
    return "GREEN", (f"No material failure established; {n_minor} MINOR finding(s). This is the "
                     f"absence of an established failure within the audited scope, not a "
                     f"certificate of correctness.")


def severity_review(findings: list[Finding]) -> list[str]:
    """Currently-COUNTED FATAL/MAJOR findings whose evidence is not a checkable cell
    citation — i.e. this looks at `counted(f)`, not the lens's raw `severity`, so a
    finding graded down below MAJOR before this runs no longer appears here even if the
    lens itself asserted MAJOR. What remains is exactly what still carries weight in the
    verdict on prose alone.

    This FLAGS and does not demote further. `harness.grading.derive` is where earning
    happens now — a real subsystem, not a speculative one (see `harness/grading.py` and
    `harness/prompts/grade.py`) — but even a blinded second reviewer is still a model,
    and what IS machine-checked is only its citation and the derivation table, not its
    judgement. This list is what remains for a human to check first.
    """
    return [f"[{counted(f)}] {f.finding_id or f.title[:40]} ({f.lens}) — evidence is "
            f"{f.evidence_class.replace('_', ' ')}, not a cited table cell"
            for f in findings
            if counted(f) in ("FATAL", "MAJOR") and f.evidence_class != "cell_verified"]


def questions(findings: list[Finding]) -> list[Finding]:
    """The findings that are QUESTIONS rather than defects, from either reader's answer.

    Kept as its own predicate because two sections need the same set — the report's
    `## Open review questions` and the whole-paper prompt's own question block — and a
    strong paper's review is mostly this list. `harness.grading.CANDIDATE_CAP` already
    holds them to NOTE, so nothing here affects a threshold; what matters is that they
    are PRINTED as questions instead of quietly folded in among the defects.
    """
    return [f for f in findings
            if f.finding_class == "OPEN_QUESTION"
            or (f.finding_class == "UNGRADED" and f.candidate_class == "OPEN_QUESTION")]


def grading_review(findings: list[Finding]) -> list[str]:
    """Every finding where grading actually changed what counts — `counted(f) !=
    f.severity`. The direct answer to "where did grading move this verdict", one line
    per finding, naming the cap that bound it."""
    return [f"{f.finding_id or f.title[:40]} ({f.lens}): lens asserted {f.severity}, "
            f"counted as {counted(f)} — capped by `{f.binding_cap or 'unknown'}` "
            f"({f.finding_class})"
            for f in findings if f.counted_severity and f.counted_severity != f.severity]


def verdict_sensitivity(findings: list[Finding],
                        reconciliation: Reconciliation | None = None) -> str:
    """The same verdict, over only the findings whose evidence is a verified table cell.

    NOT a second grader and not a second opinion. It is `overall_verdict` — the identical
    materiality table — applied to a subset of the identical findings, chosen by
    `evidence_class`, which the harness wrote. Nothing here judges anything.

    What it answers is the question `severity_review` could only gesture at. That function
    lists the FATAL/MAJOR findings resting on prose; it cannot say whether they MATTER. On
    the pilot corpus they do: 15 of 29 MAJOR findings are prose-backed, and for two of the
    three papers the RED verdict softens to GREEN without them. "Severity is not
    machine-verified" and "this RED depends on grades that are not machine-verified" are
    very different statements to hand an editor, and only the second is actionable.

    A reproduction failure is passed through unchanged, because it is not a graded finding
    at all — it is arithmetic against a cell, and dropping it here would report a
    sensitivity to severity for a verdict that never depended on severity.
    """
    cell_backed = [f for f in findings if f.evidence_class == "cell_verified"]
    return overall_verdict(cell_backed, reconciliation)[0]


def verdict_if_lens_severity_only(findings: list[Finding],
                                  reconciliation: Reconciliation | None = None) -> str:
    """The verdict this paper would have received before independent grading existed —
    the mirror of `verdict_sensitivity`, chosen by `counted_severity` instead of
    `evidence_class`. Not a second opinion: `overall_verdict`, the identical threshold
    table, over the identical findings with `counted_severity` erased so every count
    falls back to the lens's own raw assertion. The only way a reader can see WHERE
    grading moved a verdict, same as `verdict_if_cell_backed_only` already shows where
    the evidence class does."""
    lens_only = [f.model_copy(update={"counted_severity": ""}) for f in findings]
    return overall_verdict(lens_only, reconciliation)[0]


def build_chain(findings: list[Finding], probe: ProbeResult | None) -> ExperimentalChain | None:
    """Link the reproduction attempt back to the finding it was about. Copies, never derives.

    Every field comes from an artifact some earlier stage wrote. The one thing computed
    here is `broken_link`: the FIRST precondition that was not established, so a reader
    of an INCONCLUSIVE sees which link failed instead of having to re-walk the chain.
    Naming the first one matters — later links are not assessed once an earlier one
    fails, so listing them all would imply checks that never ran.
    """
    if probe is None:
        return None
    rec, acq = probe.reconciliation, probe.repo
    anchor = next((f for f in findings if f.finding_id and f.finding_id == probe.finding_id), None)

    chain = ExperimentalChain(
        finding_id=probe.finding_id or "",
        claim=(anchor.as_claim() if anchor else probe.claim or ""),
        evidence_refs=[anchor.evidence_ref] if anchor and anchor.evidence_ref else (
            [rec.table_ref] if rec and rec.table_ref else []),
        repo_url=(acq.url if acq else ""),
        commit=(acq.commit if acq else ""),
        backend=probe.backend or "",
        provenance=probe.provenance or "",
        executed=bool(probe.seeds_run),
        executions=probe.executions,
        execution_log=probe.execution_log,
        reconciliation=(rec.status if rec else ""),
        failure_class=(rec.failure_class if rec else "") or "",
    )
    # The fresh execution-time check when there was one, else what planning established.
    # A probe that never executed has only the second, and reporting it as `unassessed`
    # would name the wrong broken link — the commit was checked, something later failed.
    chain.commit_state = (probe.commit_verification.state
                          if probe.commit_verification is not None
                          else (probe.commit_state or "unassessed"))
    if probe.backend_selection:
        chain.backend = f"{probe.backend} ({probe.backend_selection})"
    if probe.experiment is not None:
        chain.experiment_state = probe.experiment.state
    if probe.metric_identity is not None:
        chain.metric_state = probe.metric_identity.state
    if probe.configuration is not None:
        chain.configuration_state = probe.configuration.state
    if probe.capability is not None:
        chain.capability_code = probe.capability.reason_code
    if probe.resources is not None:
        chain.resource_state = probe.resources.state
    if probe.authorization is not None:
        chain.authorization = probe.authorization.decision

    for label, ok in (
            ("repository", bool(chain.repo_url)),
            ("audited commit", chain.commit_state == "verified"),
            ("experiment identity", chain.experiment_state == "established"),
            ("metric identity", chain.metric_state == "established"),
            ("configuration identity", chain.configuration_state == "established"),
            ("execution capability", chain.capability_code == "established"),
            ("resource sufficiency", chain.resource_state == "satisfied"),
            ("authorization", chain.authorization == "authorized"),
            ("execution", chain.executed)):
        if not ok:
            chain.broken_link = label
            break
    return chain


def _chain_block(c: ExperimentalChain) -> list[str]:
    """The experimental chain as a table a reader can walk link by link."""
    rows = [("finding", f"`{c.finding_id}`" if c.finding_id else "— none targeted"),
            ("paper claim", _cell(c.claim, 160) or "—"),
            ("evidence", ", ".join(f"`{r}`" for r in c.evidence_refs) or "—"),
            ("repository", f"`{c.repo_url}`" if c.repo_url else "— none advertised"),
            ("audited commit", f"`{c.commit[:12]}` ({c.commit_state})" if c.commit else "— none"),
            ("experiment identity", c.experiment_state),
            ("metric identity", c.metric_state),
            ("configuration identity", c.configuration_state),
            ("execution capability", c.capability_code),
            ("resource sufficiency", c.resource_state),
            ("backend", c.backend or "—"),
            ("authorization", c.authorization or "—"),
            ("code that ran", c.provenance or "—"),
            ("executed", f"yes — {c.executions} process(es)" if c.executed else "no"),
            ("execution log", f"`{c.execution_log}`" if c.execution_log else "—"),
            ("reconciliation", c.reconciliation or "—")]
    out = ["| link | state |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    if c.broken_link:
        out += ["", f"⛔ The chain breaks at **{c.broken_link}**. Nothing after that link was "
                    f"established, so no reproduction verdict follows — this is a limit of "
                    f"what could be proven here, not a finding about the paper."]
    elif c.reconciliation in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        out += ["", f"🟢 Every link established, and the reconciliation reached "
                    f"**{c.reconciliation}** — a real reproduction verdict."]
    else:
        # No broken link is not the same as a verdict. `reconcile` refuses for reasons that
        # break no chain link at all — a units mismatch, a zero noise band, an unparseable
        # cell — and this branch used to print "the reconciliation above is a real
        # reproduction verdict" over an INCONCLUSIVE, which the same file defines as
        # "no reproduction verdict can be drawn". Complete-chain does not imply verdict.
        out += ["", f"⚪ Every link was established, but the reconciliation is "
                    f"**{c.reconciliation or 'not recorded'}**, so no reproduction verdict "
                    f"follows. The chain shows the attempt was legitimate; the arithmetic "
                    f"did not settle the cell."]
    return out


def pick_unasked_question(reports: list[LensReport]) -> str:
    """First non-empty question in lens-priority order. Deterministic, verbatim."""
    for r in sorted(reports, key=lambda r: _LENS_RANK.get(r.lens, 0), reverse=True):
        if r.unasked_question.strip():
            return r.unasked_question.strip()
    return ""


def _probe_heading(p: ProbeResult) -> str:
    """Name the section after what actually ran, so the contents are not oversold."""
    if p.provenance == "synthesized":
        return f"Autonomous mechanism probe — `{p.mechanism}`"
    if p.provenance == "repo_exec":
        return "Measured reproduction (the paper's own code)"
    return "Measured reproduction (local)"


def _probe_block(p: ProbeResult) -> list[str]:
    """The measured numbers, stated plainly. Every figure comes from the probe JSON."""
    if p.verdict == "blocked":
        # Deliberately worded as a fact about this harness. "The probe did not produce
        # measurements" would read, next to a paper, as though the paper's code had been
        # tried and found wanting; nothing was tried.
        auth = p.authorization
        why = auth.detail if auth else p.reason
        return [f"⛔ **The repository was not executed** — this harness declined to run it "
                f"(`{auth.decision if auth else 'unauthorized'}`). {why} "
                f"Nothing about the paper follows from this."]
    if p.verdict == "failed":
        return [f"The reproduction probe did not produce usable measurements. {p.reason}"]
    if p.verdict == "degenerate":
        return [f"⚠️ The probe ran but its noise estimate is unusable. {p.reason}"]

    # A probe with no paper-specific script did not reproduce anything — it calibrated
    # this machine's seed noise. Saying otherwise would dress a calibration run up as
    # evidence about the paper, which is the exact move this harness exists to catch
    # other people making. `calibration` is recorded on the result by the runner; the
    # fallback is only for probe_results.json written before that field existed.
    calibration = (p.calibration if p.calibration is not None
                   else (p.verdict == "calibration" or p.claimed_delta is None))
    synthesized = p.provenance == "synthesized"
    if calibration:
        head = (f"⚪ **Hardware Noise-Floor Calibration** (σ = {p.measured_std:.4f}, "
                f"2σ = {p.noise_band:.4f}) — No paper-specific script evaluated.")
    elif synthesized:
        placebo = p.mechanism == "placebo"
        if placebo:
            # Inverted on purpose relative to a genuine mechanism probe below: a placebo
            # arm has no hypothesis, so its clearing the noise band is the CONCERNING
            # result — an arbitrary perturbation moves the metric as much as the paper's
            # claimed mechanism did — and its staying inside the band is the benign,
            # expected one. Marking "detectable" 🟢 here read as the harness endorsing an
            # auxiliary term with no hypothesis as if it were a positive finding.
            head = {"within_noise": ("🟢 **Placebo control — an auxiliary term with no "
                                     "hypothesis did NOT clear the noise band.**"),
                    "detectable": ("⚠️ **Placebo control — an auxiliary term with no "
                                   "hypothesis ALSO clears the noise band.**")}.get(p.verdict, "")
        else:
            head = {"within_noise": (f"🔴 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                                     f"sits INSIDE the noise band.**"),
                    "detectable": (f"🟢 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                                   f"CLEARS the noise band.**")}.get(p.verdict, "")
    else:
        head = {"within_noise": "🔴 **The measured effect sits inside the noise band.**",
                "detectable": "🟢 **The measured effect clears the noise band.**"}.get(p.verdict, "")
    out = [head, "", f"Ran on `{p.device}` over {len(p.seeds_run)} seeds"
                     f"{f' ({len(p.seeds_failed)} failed)' if p.seeds_failed else ''}"
                     f" in {p.seconds:.0f}s.", "",
           "| quantity | value |", "|---|---|"]
    # A calibration run's arms are identical by construction, so its "measured delta" is
    # a property of the template, not a measurement of anything. Printing it next to a
    # claimed delta is what made the old block read as a reproduction.
    if not calibration:
        out.append(f"| measured delta | {p.measured_delta:+.4f} |")
    out += [f"| seed noise (1σ) | {p.measured_std:.4f} |",
            f"| detectability band (2σ) | {p.noise_band:.4f} |"]
    for arm, st in p.arms.items():
        out.append(f"| {arm} mean (n={st.n}) | {st.mean:.4f} ± {st.std:.4f} |")
    if p.claimed_delta is not None:
        out.append(f"| **claimed delta** | **{p.claimed_delta:+.4f}** |")
    out += ["", p.reason]

    # Secondary measurements. A headline metric that moved is only half the answer; the
    # other half is whether anything else moved with it.
    if p.aux:
        arms = list(p.arms)
        out += ["", "**Secondary measurements** (same runs, same seeds):", "",
                "| quantity | " + " | ".join(arms) + " | delta |", "|---" * (len(arms) + 2) + "|"]
        for key, per_arm in p.aux.items():
            cells = [f"{per_arm[a].mean:.4f} ± {per_arm[a].std:.4f}" if a in per_arm else "—"
                     for a in arms]
            if len(arms) == 2 and all(a in per_arm for a in arms):
                delta = f"{per_arm[arms[-1]].mean - per_arm[arms[0]].mean:+.4f}"
            else:
                delta = "—"
            out.append(f"| `{key}` | " + " | ".join(cells) + f" | {delta} |")

    if synthesized:
        if p.mechanism == "placebo":
            # The placebo is the ONLY synthesized template — probe_synth.plan has no
            # mechanism dispatch — and it is paper-independent BY CONSTRUCTION: the same
            # script runs for every paper. Calling it "written from the paper's own
            # published formulation" was false for every synthesized probe this harness
            # has ever produced against a real paper.
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a generic control, not a reproduction of the paper's "
                    f"mechanism.** `runs/{p.paper_id}/probe.py` is a paper-independent "
                    f"placebo control this harness runs whenever no mechanism-specific "
                    f"template matches — it was NOT derived from this paper's formulation. "
                    f"It measures how much an auxiliary term with no hypothesis moves the "
                    f"metric under the same budget, and it is **not** evidence about any "
                    f"number printed in the paper's tables, nor about the paper's own "
                    f"claimed mechanism specifically." + (" The reconciliation below is "
                    f"capped at INCONCLUSIVE for that reason." if p.reconciliation else "")]
        else:
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a reimplementation, not a reproduction.** `runs/{p.paper_id}/probe.py` "
                    f"was written by the harness from the paper's own published formulation and run at "
                    f"toy scale on synthetic data. It is evidence about whether the stated mechanism "
                    f"behaves as described, and it is **not** evidence about any number printed in the "
                    f"paper's tables — those were produced at a scale and on datasets this probe does "
                    f"not touch." + (" The reconciliation below is capped at INCONCLUSIVE for that "
                                     "reason." if p.reconciliation else "")]
    if calibration:
        out += ["", f"Read this as a detectability floor: on this hardware and metric, any claimed "
                    f"gain below {p.noise_band:.4f} could not be distinguished from run-to-run "
                    f"variation at {len(p.seeds_run)} seeds. It is a measurement of this machine, "
                    f"not of the paper. Supply a reproduction script at "
                    f"`runs/<paper_id>/spec.json` to test an actual claim."]
    if p.claim_within_noise:
        out += ["", f"**The paper's claimed delta of {p.claimed_delta:+.4f} is smaller than this "
                    f"machine's 2σ seed-noise band of {p.noise_band:.4f}.** A gain that size cannot "
                    f"be distinguished from run-to-run variation at this seed count."]
    return out


_REPO_HEAD = {
    "cloned": "🟢 **Cloned and inspected.**",
    "cached": "🟢 **Inspected from an existing checkout.**",
    "synthesized": "⚪ **No repository advertised** — a standalone scaffold was written instead.",
    "unavailable": "⚪ **The paper advertises no code repository.**",
    "blocked": "⚪ **Not fetched — the network gate is closed.**",
    "failed": "⚠️ **Acquisition failed.**",
    "not_attempted": "⚪ **Code acquisition did not run.**",
}
_RECONCILE_HEAD = {
    "RESOLVED_VERIFIED": "🟢 **RESOLVED / VERIFIED** — the executed metric matches the printed cell.",
    "FAILED_REPRODUCTION": "🔴 **FATAL — FAILED CODE REPRODUCTION.**",
    "INCONCLUSIVE": "⚪ **Inconclusive** — no reproduction verdict can be drawn.",
    "NOT_ATTEMPTED": "⚪ **Not attempted.**",
}
MAX_CODE_ROWS = 12


def _repo_block(a: RepoAcquisition) -> list[str]:
    """Where the code came from. States plainly when the answer is 'we did not look'."""
    out = [_REPO_HEAD.get(a.status, f"**{a.status}**"), ""]
    rows = [("source", f"`{a.url}`" if a.url else "— none found in the paper"),
            ("status", a.status)]
    if a.commit:
        rows.append(("commit", f"`{a.commit}`"))
    if a.path:
        rows.append(("path", f"`{a.path}`"))
    if a.frameworks:
        rows.append(("frameworks", ", ".join(a.frameworks)))
    if a.dependency_files:
        rows.append(("dependency files", ", ".join(f"`{f}`" for f in a.dependency_files)))
    if a.dependencies:
        shown = ", ".join(a.dependencies[:12])
        extra = f" _(+{len(a.dependencies) - 12} more)_" if len(a.dependencies) > 12 else ""
        rows.append(("declared packages", shown + extra))
    if a.entrypoint:
        rows.append(("entrypoint", f"`{a.entrypoint}`"))
    rows.append(("environment", a.env_status))
    out += ["| item | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    if a.reason:
        out += ["", a.reason]
    if a.reimplementation is not None:
        # A paper with no code is where a reader most needs to see a DECISION rather than
        # a stop, so the ingredient table is printed in both directions: it is the
        # evidence for "we could have rebuilt this and here is the brief", and equally the
        # evidence for "we could not, and these are the sentences the paper never wrote".
        r = a.reimplementation
        out += ["", "### Independent reimplementation (PATH B)", "",
                ("🟢 **Eligible** — the paper specifies enough to rebuild the experiment."
                 if r.established else
                 "⚪ **Not eligible** — the paper does not specify enough to rebuild the "
                 "experiment."), "", r.reason, "",
                "| ingredient | required | supplied | found at |", "|---|---|---|---|"]
        out += [f"| {i.kind} | {'yes' if i.required else 'no'} | "
                f"{'yes' if i.present else '**no**'} | {('`' + i.ref + '`') if i.ref else '—'} |"
                for i in r.ingredients]
        out += ["", "This is an ELIGIBILITY assessment about the paper's completeness. It "
                    "concludes nothing about whether the paper's claims are correct, and a "
                    "reimplementation — if one is written — is reported as "
                    "`INDEPENDENT_REIMPLEMENTATION`, never as the authors' own code."]
    return out


def _runtime_block(c: CodeAudit) -> list[str]:
    """What the checkout says it needs. Observations with citations, and nothing more.

    Deliberately separate from the findings block above it, and worded so it cannot be read
    as an accusation: these are requirements, not defects, and not one of them gates
    execution. Six detection rules were designed for this and each was attacked; all six
    came back saying the evidence supports an observation rather than a conclusion, so what
    is printed is a file, a line and a verbatim token for a human to judge.
    """
    if c.skipped or not c.declarations_scanned:
        return []
    if not c.runtime:
        return [f"⚪ **No runtime demand declared** in {len(c.declarations_scanned)} "
                f"declaration file(s). Absence of a declaration, not evidence that the "
                f"experiment needs nothing."]
    by_kind: dict[str, list] = {}
    for d in c.runtime:
        by_kind.setdefault(d.kind, []).append(d)
    out = [f"Read out of {len(c.declarations_scanned)} declaration file(s) and "
           f"{c.files_scanned} Python file(s). **None of this gates execution** — a demand "
           f"here can neither permit a reproduction nor refuse one.", ""]
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        out.append(f"- **{kind.replace('_', ' ')}** ({len(rows)})")
        for d in rows[:4]:
            where = f"`{d.file}:{d.line}`" if d.line else f"`{d.file}`"
            value = f" `{_cell(d.value, 70)}`" if d.value else ""
            out.append(f"  - {d.state} · {d.scope} · {where}{value}")
        if len(rows) > 4:
            out.append(f"  - _…and {len(rows) - 4} more._")
    return out


def _code_audit_block(c: CodeAudit) -> list[str]:
    """Static findings, each with a file, a line and the source line itself."""
    if c.skipped:
        return [f"⚪ **Not run.** {c.skipped}"]
    if not c.findings:
        return [f"🟢 **No cheat patterns matched** across {c.files_scanned} Python file(s) "
                f"({c.lines_scanned:,} lines). This is the absence of a signature, not a "
                f"clean bill of health: the detectors cover baseline crippling, split "
                f"leakage and metric redefinition, and nothing else."]
    counts = Counter(f.category for f in c.findings)
    out = [f"⚠️ **{len(c.findings)} pattern(s)** across {c.files_scanned} Python file(s) "
           f"({c.lines_scanned:,} lines): "
           + ", ".join(f"{n} {cat.replace('_', ' ')}" for cat, n in sorted(counts.items())) + ".",
           "", "Static hits are suspicions with line numbers, never verdicts — each is "
           "listed with the counter-explanation that would clear it.", ""]
    for f in c.findings[:MAX_CODE_ROWS]:
        out.append(f"- **[{f.severity}] {f.title}** — `{f.file}:{f.line}` · `{f.rule_id}`")
        out.append(f"  {f.statement}")
        if f.code_quote:
            out.append(f"  > `{_cell(f.code_quote, _QUOTE_CHARS)}`")
        if f.counter_explanations:
            out.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
    if len(c.findings) > MAX_CODE_ROWS:
        out.append(f"- _…and {len(c.findings) - MAX_CODE_ROWS} further pattern(s); "
                   f"full set in `runs/<paper_id>/probe_results.json`._")
    if c.unparseable:
        out += ["", f"_{len(c.unparseable)} file(s) could not be parsed and were skipped._"]
    return out


def _reconciliation_block(r: Reconciliation) -> list[str]:
    """Executed number against the printed cell, with the arithmetic shown."""
    out = [_RECONCILE_HEAD.get(r.status, f"**{r.status}**"), "", "| quantity | value |", "|---|---|"]
    out.append(f"| cited cell | `{r.table_ref or '—'}` |")
    out.append(f"| cell contents (verbatim) | `{r.claimed_raw or '—'}` |")
    out.append(f"| claimed value | "
               f"{f'{r.claimed_value:g}' if r.claimed_value is not None else '— unparseable'} |")
    out.append(f"| reproduced value | "
               f"{f'{r.reproduced_value:g}' if r.reproduced_value is not None else '— none produced'} |")
    if r.reproduced_std is not None:
        out.append(f"| reproduced spread (1σ) | {r.reproduced_std:.4f} |")
    if r.delta_error is not None:
        out.append(f"| **Δ_error \\|reproduced − claimed\\|** | **{r.delta_error:.4f}** |")
    out.append(f"| hardware noise band (2σ) | {r.noise_band:.4f} |")
    if r.seeds_run:
        out.append(f"| seeds | {len(r.seeds_run)} ({', '.join(str(s) for s in r.seeds_run)}) |")
    out += ["", r.reason]
    return out


def _cell(s: str, n: int) -> str:
    """One markdown table cell: pipes escaped, newlines flattened, length capped."""
    s = " ".join((s or "").split()).replace("|", "\\|")
    return (s[: n - 1] + "…") if len(s) > n else s


def render_eval_report(r: EvalReport) -> str:
    """EvalReport → markdown. Pure: no LLM, no network, no Config.

    Target is 1-2 pages, so the findings table is capped and the threat bullets are
    capped — but a truncation is always STATED, never silent. A report that quietly
    drops findings reads as a clean bill of health for the ones it dropped.
    """
    ranked = rank(r.findings)
    # GREEN is deliberately not "PASS". The decision is "no material failure was
    # ESTABLISHED", which is a statement about this audit's reach, not a certificate
    # about the paper — and a badge reading PASS is exactly how that gets misread.
    badge = {"RED": "🔴 RED — a material failure was established",
             "GREEN": "🟢 GREEN — no material failure established"}.get(r.verdict, r.verdict)
    counts = {s: sum(1 for f in r.findings if counted(f) == s) for s in _SEVERITY_RANK}
    lens_counts = {s: sum(1 for f in r.findings if f.severity == s) for s in _SEVERITY_RANK}
    graded_delta = counts != lens_counts

    L = [
        f"# First-Round Review — {r.title or r.paper_id}",
        "",
        f"**Verdict: {badge}**",
    ]
    if r.verdict_contested:
        L += ["", f"🚩 **CONTESTED** — the independent substantive read below "
                 f"(`{r.substantive_verdict.verdict if r.substantive_verdict else ''}`) disagrees "
                 f"sharply with this deterministic verdict. Neither is overruled; this needs a "
                 f"human look before the verdict above is relied on as-is."]
    L += [
        "",
        f"> {r.verdict_reason}",
        "",
        f"`{r.paper_id}` · {r.n_pages} pages · {r.n_sections} sections · {r.n_tables} tables · "
        f"{r.n_numbers} reported numbers",
        f"Lenses run: {', '.join(r.lenses_run) or '(none)'} · "
        f"Counted: {counts['FATAL']} FATAL / {counts['MAJOR']} MAJOR / {counts['MINOR']} MINOR"
        + (f" · lens-asserted: {lens_counts['FATAL']} FATAL / {lens_counts['MAJOR']} MAJOR / "
           f"{lens_counts['MINOR']} MINOR" if graded_delta else "")
        + (f" · {r.dropped_findings} dropped as unsubstantiated" if r.dropped_findings else ""),
        "",
        "## Decision",
        "",
        "| | |",
        "|---|---|",
        f"| **Paper decision** | {r.verdict} |",
        f"| **Claim status** | `{r.claim_status or 'NOT_VERIFIED'}` |",
        f"| **Reproduction** | `{r.reproduction_status or 'NOT_ATTEMPTED'}` |",
        f"| **What ran** | `{r.execution_provenance or 'SYNTHESIZED_DIAGNOSTIC'}` |",
        "",
        _DECISION_GLOSS.get(r.claim_status, _DECISION_GLOSS["NOT_VERIFIED"]),
        "",
        _REPRO_GLOSS.get(r.reproduction_status, _REPRO_GLOSS["NOT_ATTEMPTED"]),
        "",
        "## Critical validity threats",
        "",
    ]

    # COUNTED, not asserted — a MAJOR a grader could not confirm no longer belongs among
    # "critical" threats even though the lens still says MAJOR on its own file.
    threats = [f for f in ranked if counted(f) in ("FATAL", "MAJOR")]
    if not threats:
        L.append("None. No finding rises above MINOR.")
    else:
        for f in threats[:MAX_THREAT_BULLETS]:
            where = f" — `{f.evidence_ref}`" if f.evidence_ref else ""
            conf = f" ({f.confidence} confidence)" if f.confidence else ""
            sev_label = (f"{counted(f)} (lens asserted {f.severity})"
                        if f.counted_severity and f.counted_severity != f.severity else f.severity)
            L.append(f"- **[{sev_label}] {f.title}**{conf}{where}")
            # Evidence and inference are printed as separate, labelled lines. They are
            # different kinds of thing: the quote and the observation were checked by the
            # harness against the parsed paper, and the reasoning is a model's argument
            # from them. A reader who cannot tell which is which cannot audit either.
            L.append(f"  > \"{_cell(f.evidence_quote, _QUOTE_CHARS)}\"")
            if f.verified_observation:
                L.append(f"  ✅ *Verified* ({f.evidence_class}): "
                         f"{_cell(f.verified_observation, 260)}")
            L.append(f"  🧠 *Inference (model reasoning, not verified):* "
                     f"{_cell(f.as_reasoning(), 400)}")
            if f.alternative_interpretation:
                L.append(f"  🔍 *Falsification attempted (not verified):* "
                         f"{_cell(f.alternative_interpretation, 300)}"
                         + (f" — did not resolve it: {_cell(f.why_alternative_fails, 200)}"
                            if f.why_alternative_fails else ""))
            if f.steelman:
                L.append(f"  🛡 *Steelman (not verified):* {_cell(f.steelman, 300)}")
            if f.counter_explanations:
                L.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
        if len(threats) > MAX_THREAT_BULLETS:
            L.append(f"- _…and {len(threats) - MAX_THREAT_BULLETS} further "
                     f"FATAL/MAJOR finding(s) — see the table below._")
    L.append("")

    if qs := questions(ranked):
        L += ["## Open review questions", "",
              "Questions a reviewer should ask, not evidence of a flaw — these count "
              "toward no threshold. A strong paper legitimately has several.", ""]
        for f in qs[:MAX_THREAT_BULLETS]:
            L.append(f"- {_cell(f.title or f.statement, 200)}")
        L.append("")

    refuted = [f for f in ranked if f.finding_class == "REFUTED"]
    if refuted:
        L += ["## Refuted candidates", "",
              "A blinded second reviewer specifically refuted these — withdrawn, not "
              "deleted, so the accusation and its refutation both stay visible.", ""]
        for f in refuted[:MAX_THREAT_BULLETS]:
            reason = f.grade.falsification if f.grade else ""
            L.append(f"- **{f.title}** (lens asserted {f.severity}) — "
                     f"{_cell(reason, 240) or 'no reason recorded'}")
        L.append("")

    dismissed = [f for f in ranked if f.candidate_class == "DISMISSED"]
    if dismissed:
        L += ["## Candidates raised and withdrawn", "",
              "The lens itself considered these and a reasonable reading resolved them. "
              "Printed so a reader can see the question was asked and answered rather "
              "than never asked.", ""]
        for f in dismissed[:MAX_THREAT_BULLETS]:
            L.append(f"- **{_cell(f.title, 110)}** — resolved by: "
                     f"{_cell(f.alternative_interpretation, 240) or 'no reading recorded'}")
        L.append("")

    if r.grade_coverage.get("candidates"):
        graded_findings = [f for f in ranked if f.grade_state == "graded"]
        L += ["## Independent grading", "",
              f"{r.grade_coverage.get('graded', 0)} of {r.grade_coverage.get('candidates', 0)} "
              f"serious candidate(s) independently graded by a second, blinded reviewer that saw "
              f"none of: the lens's severity, the lens's name, any other finding, or how findings "
              f"are counted (see `harness/prompts/grade.py`).", ""]
        if r.verdict_if_lens_severity_only and r.verdict_if_lens_severity_only != r.verdict:
            L += [f"**Grading moved this verdict.** On the lenses' own asserted severities alone, "
                  f"the same materiality table would have yielded "
                  f"**{r.verdict_if_lens_severity_only}** rather than **{r.verdict}**.", ""]
        moved = grading_review(r.findings)
        for line in moved[:MAX_THREAT_BULLETS]:
            L.append(f"- {line}")
        if moved:
            L.append("")
        for f in graded_findings[:MAX_THREAT_BULLETS]:
            g = f.grade
            if g is None:
                continue
            L.append(f"- `{f.finding_id}` grader verdict **{g.verdict}** / {g.severity} / "
                     f"{g.confidence} confidence → counted {counted(f)}")
            if g.falsification:
                L.append(f"  🔍 *Grader falsification (not verified):* {_cell(g.falsification, 260)}")
            if g.steelman:
                L.append(f"  🛡 *Grader steelman (not verified):* {_cell(g.steelman, 260)}")
            if g.impact_statement:
                L.append(f"  🧠 *Grader impact statement (not verified):* {_cell(g.impact_statement, 260)}")
            if g.independent_evidence_quote:
                label = "✅ *Verified*" if f.grader_evidence_class != "unverified" else "⚠️ *Unverified*"
                L.append(f"  {label} ({f.grader_evidence_class}): grader cited "
                         f"`{g.independent_evidence_ref}` — {_cell(f.grader_verified_observation or g.independent_evidence_quote, 220)}")
        L.append("")
    elif r.grade_coverage:
        L += ["## Independent grading", "",
              "Grading is off (`SH_ALLOW_GRADING`). Severity is lens-asserted and is what "
              "this verdict counts.", ""]

    if r.substantive_verdict:
        sv = r.substantive_verdict
        L += ["## Whole-paper assessment (model opinion, not counted)", "",
              "Reasoned independently rather than derived from the counts above — the one "
              "judgement a materiality table structurally cannot make. It moves no colour: "
              f"agreement with the deterministic verdict is `{r.verdict_agreement}`.", "",
              f"**{sv.verdict}**"
              + (f" · core contribution stands: **{sv.core_contribution_stands}**"
                 if sv.core_contribution_stands else "")
              + f" · weaknesses are {sv.weaknesses_are or 'unstated'}", "",
              f"> {_cell(sv.reason, 500)}", ""]
        for label, value in (
                ("Real contribution", sv.real_contribution or sv.strongest_contribution),
                ("Strongest support", sv.strongest_support),
                ("Strongest threat", sv.strongest_threat or sv.weakest_link)):
            if value:
                L.append(f"- **{label}:** {_cell(value, 300)}")
        if sv.claims_well_supported:
            L.append("- **Claims that stand as stated:**")
            L += [f"  - {_cell(c, 200)}" for c in sv.claims_well_supported[:5]]
        if sv.claims_needing_qualification:
            L.append("- **Claims needing qualification:**")
            L += [f"  - {_cell(c, 200)}" for c in sv.claims_needing_qualification[:5]]
        L.append("")

    if r.probe:
        L += [f"## {_probe_heading(r.probe)}", "", *_probe_block(r.probe), ""]
        if r.probe.repo is not None:
            L += ["## Code acquisition", "", *_repo_block(r.probe.repo), ""]
        if r.probe.code_audit is not None:
            L += ["## Static code audit", "", *_code_audit_block(r.probe.code_audit), ""]
            if runtime := _runtime_block(r.probe.code_audit):
                L += ["## Declared runtime demands", "", *runtime, ""]
        if r.probe.reconciliation is not None:
            L += ["## Table-cell reconciliation", "",
                  *_reconciliation_block(r.probe.reconciliation), ""]
        if r.experimental_chain is not None:
            L += ["## Experimental evidence chain", "",
                  *_chain_block(r.experimental_chain), ""]

    ungraded = severity_review(r.findings)
    if ungraded:
        L += ["## Severity caveat", "",
              f"{len(ungraded)} of {sum(1 for f in r.findings if f.severity in ('FATAL', 'MAJOR'))} "
              f"FATAL/MAJOR finding(s) rest on prose rather than a cited table cell. Severity is "
              f"assigned by the lens and is not machine-verified, and it is what the verdict "
              f"counts \u2014 check these first:", ""]
        L += [f"- {u}" for u in ungraded]
        L += [""]
        # How much the verdict actually leans on those grades. The same materiality table
        # over the cell-verified findings alone — no second judgement, no demotion.
        if r.verdict_if_cell_backed_only and r.verdict_if_cell_backed_only != r.verdict:
            L += [f"**This verdict depends on them.** Counting only findings whose evidence is a "
                  f"verified table cell, the same materiality table yields "
                  f"**{r.verdict_if_cell_backed_only}** rather than **{r.verdict}**. The "
                  f"difference is carried entirely by grades the harness cannot check.", ""]
        elif r.verdict_if_cell_backed_only:
            L += [f"The verdict does not depend on them: counting only cell-verified findings, the "
                  f"same materiality table still yields **{r.verdict_if_cell_backed_only}**.", ""]

    # Blockquoted, not interpolated bare: this is the one lens-supplied field the schema
    # invites to be a paragraph or two, so it cannot be collapsed to one line the way
    # `title` and `finding_id` are — but a bare embed lets a line starting with '#' forge
    # a heading, or a stray ``` open an unclosed code fence that swallows the rest of the
    # report. A blockquote renders every line as quoted prose regardless of what it starts
    # with, which neutralises both without touching legitimate multi-paragraph text.
    L += ["## The unasked obvious question", ""]
    if r.unasked_question.strip():
        L += ["> " + ln for ln in r.unasked_question.strip().splitlines()]
    else:
        L.append("_No lens identified a conspicuously missing comparison._")
    L.append("")

    if r.self_audit is not None:
        sa = r.self_audit
        L += ["## Reviewer self-audit", "",
              ("🟢 " if sa.complete else "⚠️ ") + f"**{sa.summary}**", "",
              "Machine-checked against harness-written fields, never against a reviewer's "
              "own assessment of its diligence. **A failed check does not change the "
              "verdict** — the materiality table is the verdict — it refuses to let this "
              "review call itself complete, and names what was not done.", ""]
        for i in sa.items:
            mark = {"pass": "✅", "fail": "❌", "not_applicable": "—"}.get(i.state, "?")
            scope = (f" ({i.n_offenders}/{i.n_in_scope})" if i.state == "fail"
                    else (f" ({i.n_in_scope})" if i.state == "pass" else ""))
            L.append(f"- {mark} {i.question}{scope}")
            if i.state == "fail":
                L.append(f"  {i.detail} — {', '.join(i.offenders)}"
                        + (f" _(+{i.n_offenders - len(i.offenders)} more)_"
                           if i.n_offenders > len(i.offenders) else ""))
        L.append("")

    L += ["## Audit lens findings", "",
          "| Severity | Lens | Target | Finding | Evidence |",
          "|---|---|---|---|---|"]
    for f in ranked[:MAX_TABLE_ROWS]:
        sev_cell = (f"{counted(f)} (was {f.severity})"
                   if f.counted_severity and f.counted_severity != f.severity else f.severity)
        L.append(
            f"| {sev_cell} | {f.lens} | {_cell(f.target, 60) or '—'} | "
            f"{_cell(f.title, 110)} | {_cell(f.evidence_ref, 24) or '—'} |"
        )
    if not ranked:
        L.append("| — | — | — | No findings returned | — |")
    elif len(ranked) > MAX_TABLE_ROWS:
        L.append(f"| … | … | … | _{len(ranked) - MAX_TABLE_ROWS} further finding(s) "
                 f"omitted for length; full set in `audit/`_ | … |")

    verifiable = [f for f in ranked if f.verifiable_by_experiment]
    if verifiable:
        L += ["", "## Settleable by reproduction", "",
              "These findings are claims a targeted rerun could confirm or kill:", ""]
        L += [f"- `{f.finding_id}` {_cell(f.title, 110)}" for f in verifiable[:6]]

    return "\n".join(L).rstrip() + "\n"


def run_report(cfg: Config, pid: str) -> dict:
    """Assemble the ranked report from the ingested doc + whichever lenses ran."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))

    # Verified load: a finding whose evidence is not really in the paper is dropped
    # here, whoever wrote it. The driver is a language model; the harness checks.
    reports, dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    if not reports:
        return {"error": f"no audit lenses have run for '{pid}' — run audit_paper first, "
                         f"then write audit/<lens>.json for each prompt"}
    if invalid:
        # C9 — a missing, unparseable, empty, `null`, or malformed lens file must never
        # render as "ran clean, zero findings". Refused the same way `not reports` is:
        # no report is written, and the caller is told exactly which lens(es) to redo.
        # This guard stands even when `run_report` is invoked directly (the CLI path, or
        # a script), independent of whether the controller's own `collect` gate ran first.
        return {"error": f"{len(invalid)} lens(es) produced no usable result for '{pid}' and "
                         f"cannot be counted as run: {', '.join(invalid)}. Write a valid "
                         f"audit/<lens>.json for each, then re-run synthesize_report."}
    state.set_phase(cfg, pid, "report")

    probe_path = root / "runs" / pid / "probe_results.json"
    probe = ProbeResult(**state.read_json(probe_path)) if probe_path.exists() else None

    # Local import: `stages.grade` imports `rank` from this module, so a module-level
    # import here would be a cycle. By the time `run_report` is actually CALLED both
    # modules are fully loaded, so the cycle only exists at parse time, not at call time.
    from . import grade as grade_stage
    grade_stage.attach(cfg, pid, doc, reports)     # in-place: sets counted_severity etc.
    coverage = grade_stage.coverage(cfg, pid)
    if "error" in coverage:
        coverage = {"paper_id": pid, "candidates": 0, "graded": 0, "pending": 0}

    findings = rank([f for r in reports for f in r.findings])
    rec = probe.reconciliation if probe else None
    verdict, reason = overall_verdict(findings, rec)
    status, _status_why = claim_status(findings, rec)
    repro = reproduction_status(probe)
    ran_as = provenance_label(probe.provenance) if probe else "SYNTHESIZED_DIAGNOSTIC"

    from .. import verdict_driver
    # A SEALED whole-paper read wins over calling the subprocess: it is already produced,
    # already attributed, and re-running would spend a call to overwrite it. This is also
    # the only channel that works at all when the CLI is unreachable — see
    # `verdict_driver.accept_verdict`.
    substantive = verdict_driver.load_accepted(cfg, pid)

    # COUNTED severity, and the questions block alongside it. A whole-paper read shown
    # only the defects is being asked to weigh one side of the evidence: what a reviewer
    # asked and could not settle, and what it raised and then withdrew, are part of
    # judging the paper, and a reader deciding "are the weaknesses local or systemic"
    # needs both columns.
    findings_summary = "\n".join(
        f"- [{counted(f)}] ({f.lens}) {f.title}: {f.statement}" for f in findings[:30])
    withdrawn = [f for f in findings
                 if f.finding_class in ("REFUTED", "OPEN_QUESTION")
                 or f.candidate_class in ("OPEN_QUESTION", "DISMISSED")]
    questions_summary = "\n".join(
        f"- [{f.finding_class}/{f.candidate_class or 'unsorted'}] {f.title}"
        for f in withdrawn[:20])
    grading_summary = (f"{coverage['graded']} of {coverage['candidates']} candidate(s) "
                       f"independently graded.")
    from ..prompts import verdict as verdict_prompts
    verdict_prompt = verdict_prompts.build(
        doc.title, findings_summary, grading_summary, probe.reason if probe else "",
        questions_summary)
    # ALWAYS PERSISTED, gate open or shut. The audit and grading phases both write their
    # prompts to disk and stop, which is what makes a reader able to pick one up and do
    # the work by hand; this phase built its prompt inline and passed it straight to a
    # subprocess, so when that subprocess was unreachable there was nothing to hand
    # anyone — the only phase of the review with no manual entry point at all.
    prompt_path = root / "reports" / "verdict_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(verdict_prompt, encoding="utf-8")

    ok, _why = verdict_driver.available(cfg)
    if substantive is None and ok:
        substantive = verdict_driver.run(cfg, verdict_prompt,
                                         timeout_s=cfg.verdict_timeout_s)

    agreement, contested = "unavailable", False
    if substantive is not None:
        if substantive.verdict == "CENTRAL_CLAIM_NOT_ESTABLISHED" and verdict == "GREEN":
            agreement, contested = "contested", True
        elif substantive.verdict in ("STRONG", "SOUND_WITH_MINOR_CONCERNS") and verdict == "RED":
            agreement = "harness_harsher"
        elif substantive.verdict in ("SUBSTANTIAL_CONCERNS", "CENTRAL_CLAIM_NOT_ESTABLISHED") \
                and verdict == "GREEN":
            agreement = "model_harsher"
        else:
            agreement = "agree"

    report = EvalReport(
        paper_id=pid, title=doc.title, verdict=verdict, verdict_reason=reason,
        claim_status=status, reproduction_status=repro, execution_provenance=ran_as,
        verdict_if_cell_backed_only=verdict_sensitivity(findings, rec),
        verdict_if_lens_severity_only=verdict_if_lens_severity_only(findings, rec),
        findings=findings, unasked_question=pick_unasked_question(reports),
        n_pages=doc.n_pages, n_sections=len(doc.sections), n_tables=len(doc.tables),
        n_numbers=len(doc.reported_numbers),
        n_figures=len(doc.figures), n_equations=len(doc.equations),
        lenses_run=[r.lens for r in reports], dropped_findings=dropped, probe=probe,
        experimental_chain=build_chain(findings, probe),
        grade_coverage={k: v for k, v in coverage.items() if k != "paper_id"},
        substantive_verdict=substantive, verdict_agreement=agreement, verdict_contested=contested,
    )
    # LAST, over the finished report: the self-audit reads `substantive_verdict` and
    # `grade`, so it has to run after both are attached. It writes nothing else and
    # changes no verdict — see `harness/selfaudit.py`.
    report.self_audit = selfaudit.audit(report, counted)

    md_path, json_path = root / "reports" / f"{pid}.md", root / "reports" / f"{pid}.json"
    state.write_json(json_path, report.model_dump())
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_eval_report(report), encoding="utf-8")
    state.append_log(
        cfg, pid, artifact_type="eval_report", phase="report",
        headers={"verdict": verdict, "claim_status": status, "reproduction_status": repro,
                 "ran_as": ran_as, "findings": len(findings), "dropped": dropped,
                 "severities": {s: sum(1 for f in findings if counted(f) == s) for s in _SEVERITY_RANK},
                 "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
                 "grade_coverage": report.grade_coverage, "verdict_contested": contested,
                 "self_audit_failed": report.self_audit.failed if report.self_audit else []},
        path=str(md_path),
    )
    return {"paper_id": pid, "verdict": verdict, "reason": reason,
            "claim_status": status, "reproduction_status": repro, "ran_as": ran_as,
            "findings": len(findings), "dropped_unsubstantiated": dropped,
            "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
            "report_md": str(md_path), "report": f"reports/{pid}.json",
            "verdict_contested": contested,
            "self_audit_complete": bool(report.self_audit and report.self_audit.complete),
            "self_audit_failed": report.self_audit.failed if report.self_audit else []}


if __name__ == "__main__":  # self-check: python -m harness.stages.report
    def _f(fid, lens, sev, ref="", verifiable=False):
        return Finding(finding_id=fid, lens=lens, severity=sev, title=f"{fid} title",
                       statement="s", evidence_quote="q", evidence_ref=ref,
                       verifiable_by_experiment=verifiable)

    assert parse_magnitude("+3-5% top-1") == 3.0
    assert parse_magnitude("not a number") == 0.0
    assert parse_magnitude("") == 0.0

    # The binary decision: only a counted FATAL (or a failed reproduction, below) is RED.
    # No number of MAJORs accumulates into one — that is the whole point of the change.
    assert overall_verdict([_f("a", "overclaim", "FATAL")])[0] == "RED"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(3)])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(40)])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MINOR") for i in range(99)])[0] == "GREEN"
    assert overall_verdict([])[0] == "GREEN"
    assert overall_verdict([_f("a", "protocol", "MAJOR")])[0] == "GREEN"

    # ... and the epistemic state underneath keeps apart what the colour cannot.
    assert claim_status([_f("a", "overclaim", "FATAL")])[0] == "VERIFIED_FAILURE"
    assert claim_status([_f("a", "protocol", "MAJOR")])[0] == "NOT_VERIFIED"
    assert claim_status([])[0] == "NOT_VERIFIED"
    assert reproduction_status(None) == "NOT_ATTEMPTED"
    # Fails closed: an unknown provenance is never reported as the authors' own code.
    assert provenance_label("repo_exec") == "AUTHOR_REPOSITORY"
    assert provenance_label("driver") == "INDEPENDENT_REIMPLEMENTATION"
    assert provenance_label("nonsense") == "SYNTHESIZED_DIAGNOSTIC"

    # severity dominates; cell evidence outranks page evidence at equal severity
    order = rank([_f("m", "overclaim", "MINOR", "T0:r0:c0"),
                  _f("j", "protocol", "FATAL"),
                  _f("k", "contradiction", "MAJOR", "p3"),
                  _f("l", "contradiction", "MAJOR", "T1:r2:c3")])
    assert [f.finding_id for f in order] == ["j", "l", "k", "m"], order
    assert rank(order) == order, "rank must be stable/idempotent"

    rep = EvalReport(paper_id="p", title="T", verdict="RED", verdict_reason="because",
                     findings=order, unasked_question="why no baseline?", lenses_run=["protocol"])
    md = render_eval_report(rep)
    assert "🔴 RED — a material failure was established" in md and "why no baseline?" in md
    assert md.count("\n|") >= 4 and md.endswith("\n")
    assert _cell("a|b\nc", 99) == "a\\|b c", "pipes must be escaped or the table breaks"

    # --- S3 code reproduction ---------------------------------------------------------
    def _rec(status, **kw):
        kw.setdefault("provenance", "repo_exec")   # the ceiling is enforced here too
        return Reconciliation(table_ref="T1:r0:c1", claimed_raw="59.28", claimed_value=59.28,
                              noise_band=0.1, status=status, reason="r", **kw)

    # A failed reproduction is RED on its own, outranking an otherwise-clean panel.
    v, why = overall_verdict([], _rec("FAILED_REPRODUCTION", reproduced_value=64.1,
                                      delta_error=4.82))
    assert v == "RED" and "Failed code reproduction" in why, why
    # …but the softer statuses never escalate, because none of them is evidence.
    assert overall_verdict([], _rec("INCONCLUSIVE"))[0] == "GREEN"
    assert overall_verdict([], _rec("RESOLVED_VERIFIED"))[0] == "GREEN"
    assert overall_verdict([], _rec("NOT_ATTEMPTED"))[0] == "GREEN"
    assert overall_verdict([_f("a", "overclaim", "FATAL")], _rec("RESOLVED_VERIFIED"))[0] == "RED", \
        "a verified cell does not clear a FATAL the lenses found in the paper"

    blocked = _repo_block(RepoAcquisition(
        url="https://github.com/x/y", status="blocked", reason="network gate closed"))
    assert "network gate is closed" in "\n".join(blocked)
    clean = _code_audit_block(CodeAudit(repo_path="r", files_scanned=3, lines_scanned=90))
    assert "not a clean bill of health" in "\n".join(clean), \
        "an empty static audit must not read as an endorsement"
    assert "Not run." in "\n".join(_code_audit_block(CodeAudit(skipped="nothing acquired")))
    hit = _code_audit_block(CodeAudit(repo_path="r", files_scanned=1, lines_scanned=10, findings=[
        CodeAuditFinding(finding_id="code-01", rule_id="leak-fit-on-test",
                         category="data_leakage", severity="MAJOR", title="t", statement="s",
                         file="a.py", line=7, code_quote="scaler.fit(X_test)")]))
    assert "`a.py:7`" in "\n".join(hit) and "scaler.fit(X_test)" in "\n".join(hit)

    body = "\n".join(_reconciliation_block(_rec("FAILED_REPRODUCTION", reproduced_value=64.1,
                                                delta_error=4.82, seeds_run=[0, 1, 2])))
    assert "FAILED CODE REPRODUCTION" in body and "0.1000" in body and "4.8200" in body

    full = render_eval_report(EvalReport(
        paper_id="p", title="T", verdict="RED", verdict_reason="because", findings=order,
        lenses_run=["protocol"],
        probe=ProbeResult(paper_id="p", verdict="calibration", calibration=True,
                          measured_std=0.0048, noise_band=0.0096, seeds_run=[0, 1],
                          repo=RepoAcquisition(url="https://github.com/x/y", status="cloned",
                                               entrypoint="eval.py", frameworks=["pytorch"]),
                          code_audit=CodeAudit(repo_path="r", files_scanned=2, lines_scanned=40),
                          reconciliation=_rec("RESOLVED_VERIFIED", reproduced_value=59.30,
                                              delta_error=0.02))))
    for heading in ("## Code acquisition", "## Static code audit", "## Table-cell reconciliation"):
        assert heading in full, f"missing section: {heading}"
    print("report self-check OK")
