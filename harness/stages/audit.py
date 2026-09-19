"""S2 — the four-lens audit, and how a paper longer than one prompt is read.

`run_audit` does not call a model. It renders one self-contained prompt per READING UNIT
and stops. The driver — the interactive session, or a subprocess per unit — opens one
prompt at a time and returns its JSON, which the harness validates and writes itself.

**A unit is a lens, or a lens and a part of the paper.** A paper that fits in one pass has
one unit per lens and the artifact layout is exactly what it has always been:
`audit/prompts/<lens>.md` in, `audit/<lens>.json` out, four model calls. A paper that does
not fit has N part units per lens plus one cross-part SYNTHESIS unit, and `compose_lens`
assembles `audit/<lens>.json` from them — so nothing downstream knows or needs to know how
the paper was traversed.

Why parts at all. `pdf.render_sections` divided one character budget equally across every
section and hard-sliced each, so a long paper was cut before any lens read a word of it:
between 0.335 and 0.841 of the extracted prose across the evaluated corpus, while the
review's scope line said four lenses read the paper. `harness.reading` replaces the cut
with a plan of bounded parts that tile the document.

Why a synthesis. Splitting buys coverage and costs the one thing the contradiction lens
exists for — "the abstract says one thing and the conclusion another" is unraisable when
the two are in different prompts. Two mechanisms answer it: an ANCHOR PACKET (title,
abstract, conclusion, outline) repeated identically in every part, extracted and carrying
no model output; and one synthesis per lens, after that lens has read every part blind,
over its own already-verified observations. The synthesis PROPOSES and decides nothing.

**Isolation, stated precisely, because it is easy to overclaim.** Between lenses it is
absolute and unchanged: `overclaim` never sees `protocol`'s output. Between the parts of
ONE lens it means no part sees another part's findings — so a concern is anchored by the
span that produced it rather than by an earlier pass's framing. It has never meant that
one scientific reader must forget the first half of a paper before reading the second,
and claiming whole-paper review while forbidding that would be the overclaim this system
exists to catch.

What is NOT weaker anywhere is provenance. `load_reports` re-checks every finding's
`evidence_quote` against the parsed paper and, when the finding cites a cell, against that
cell's real contents — every side of a multi-location concern included. A reading is a
language model; the harness does not take its word for anything it can verify itself.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import functools
import hashlib
import re
import time

from .. import (audit_driver, delegation, grading, materiality, pdf, reading, sealing,
                state, taxonomy)
from ..artifacts import (BASELINE_CLASSES, CANDIDATE_CLASSES, CONFIDENCES, DISCREPANCY_TYPES,
                         EVIDENCE_ORIGINS, PRIOR_ART_BASES, SEVERITIES, Equation,
                         EvidencePointer, Figure, Finding, LensReport, PaperDoc)
from ..config import Config
from ..prompts import audit as P
from ..prompts import claimlink as CLP

LENSES = tuple(P.LENSES)
# THE DEFAULT, not the live value. `SH_AUDIT_BUDGET_CHARS` is read by `budget_chars()`
# below, at call time, through `harness.coverage` — one reader of that variable in the
# whole codebase. Captured at import, as this was, the budget a review PRINTS and the
# budget the prompts USED could differ whenever the variable was set after this module
# loaded, which is exactly the drift `coverage.budget_chars` was written to avoid on the
# other side of the same number.
SECTION_BUDGET_CHARS = 70_000
_SEVERITIES = SEVERITIES
_CELL_REF = re.compile(r"T(\d+):r(\d+):c(\d+)")
# The four admissible shapes for `evidence_ref`. Anything else — empty, "figure 3",
# "section 5", a bare page number — names no location a reader can check, so a finding
# carrying it is not substantiated. See `_substantiated`.
_PAGE_REF = re.compile(r"p\d+", re.I)
_FIG_REF = re.compile(r"F(\d+)")
_EQ_REF = re.compile(r"E(\d+)")
_QUOTE_MIN = 8
# No letter, no digit: a bare arrow, bullet, dash or other glyph. Checked against the
# FLATTENED quote (`_flat`, already lowercased), so this is deliberately about alphabet
# and digit characters only — matched after normalisation, not before.
_MEANINGLESS_QUOTE = re.compile(r"^[^0-9a-z]*$")
_WS = re.compile(r"\s+")


def budget_chars() -> int:
    """How much section prose one reading pass may carry, read at call time.

    Delegates to `harness.coverage.budget_chars`, which owns the environment variable and
    its malformed-value fallback. Two readers of one setting is one setting and one place
    to drift, and the drift here is invisible: the prompts would be built at one budget
    and the review would report another.
    """
    from .. import coverage as _coverage           # noqa: PLC0415 — one reader of the env
    return _coverage.budget_chars()


def _flat(s: str) -> str:
    return _WS.sub("", (s or "").lower())


def _enum(v, vocab: tuple[str, ...], default: str = "") -> str:
    """Uppercase and clamp to a closed vocabulary. Garbage becomes `default`, never a
    silent pass-through — the same discipline `severity`'s clamp-to-MINOR already has,
    generalised so every new lens-supplied enum gets it for free."""
    s = str(v or "").strip().upper()
    return s if s in vocab else default


def _prose(v, n: int = 4000) -> str:
    return str(v or "").strip()[:n]


def _origin_from_ref(ref: str) -> str:
    """PAPER_TABLE/PAPER_TEXT/PAPER_FIGURE/PAPER_EQUATION from the shape of `ref` alone.

    Deliberately not read from the lens: the ref shape already determines the origin
    for anything that can be a finding's PRIMARY evidence, so trusting the lens to name
    it adds nothing and invites a lens calling a page citation a table to go unnoticed.
    """
    ref = (ref or "").strip()
    if _CELL_REF.fullmatch(ref):
        return "PAPER_TABLE"
    if _PAGE_REF.fullmatch(ref):
        return "PAPER_TEXT"
    if re.fullmatch(r"F\d+", ref):
        return "PAPER_FIGURE"
    if re.fullmatch(r"E\d+", ref):
        return "PAPER_EQUATION"
    return ""


def _oneline(s: str, n: int) -> str:
    """Collapse to one line and cap the length.

    Applied to the two lens-supplied fields the renderer places directly into report
    markdown structure — `finding_id` and `title` — never to evidence, which is verified
    verbatim instead. Without this, an embedded newline plus a line starting with `#`
    lets a lens's own chosen text forge a heading (a fake "## Verdict: GREEN") or a table
    row in the rendered report, whether from a prompt injection or an LLM's habit of
    formatting a string field as if it were markdown.
    """
    return " ".join((s or "").split())[:n]


def source_units(doc: PaperDoc) -> tuple[tuple[int, str, str], ...]:
    """The paper as SEPARATE searchable units, one per section. Never one string.

    A quote is verified against each unit independently. Concatenating the sections into
    a single corpus first — which is what this did — let any string that straddles a
    section boundary verify, because after whitespace removal the seam is invisible.
    Reproduced on the real APT paper: the join of section 0's tail and section 1's head
    is `'owen Zhao 1 Hannaneh Hajishirzi 1 2 Qingqing Cao*3Fine-tuning and inference'`,
    which does not occur in the PDF, and the harness certified in its own voice that it
    "occurs verbatim in the parsed section text". Four separate seams verified.

    That is the one place the design says the MACHINE, not the model, is the author of
    the observation, so a false attestation there is not a cosmetic bug — it is the trust
    anchor asserting something untrue. Sections are the unit because they are the unit
    the parser produced and the unit a reader can open; whitespace normalisation still
    happens, inside a unit, where it cannot invent adjacency.
    """
    from ..claims import flatten, soft_hyphen_projection
    out = []
    for section in doc.sections:
        if not (section.text or "").strip():
            continue
        flat, offsets = flatten(section.text)
        # A THIRD ELEMENT, and it is the same text with the TYPESETTER'S hyphens gone.
        # A line break inside "generation" leaves "gener-" and "ation" in the PDF, which
        # flattens to `gener-ation`; a reader quoting the sentence writes `generation` and
        # the evidence gate refuses a correctly-quoted sentence. Measured over the
        # evaluated corpus, 4 of the 5 findings that gate drops are exactly this — 80% of
        # every drop in the corpus is punctuation the typesetter inserted.
        #
        # Note `_flat(section.text)` and `flatten(section.text)[0]` are the same string:
        # one lowercases and strips whitespace in this module, the other in `claims`. The
        # projection needs the offsets, which only `claims.flatten` returns.
        projected, _index = soft_hyphen_projection(flat, offsets, section.text)
        out.append((section.section_idx, flat, projected))
    return tuple(out)


def _units(corpus) -> tuple[tuple[int, str, str], ...]:
    """Accept the unit tuple, or a single pre-flattened string as one anonymous unit.

    Each unit carries the section's OWN `section_idx`, not its position in this tuple.
    Empty sections are skipped, so the two differ — and the first version of this fix
    reported the tuple position as the section identity. On the real APT paper (59
    sections, 4 of them empty) that misaddressed 12 of 12 prose findings: the observation
    said "#50" for text in section 53, and a reader opening #50 in doc.json finds nothing.
    Replacing one false machine attestation with another is not a fix, so the index is
    carried with the text rather than inferred from the ordering.
    """
    if isinstance(corpus, str):
        return ((-1, corpus, corpus),)
    # Padded rather than required: `verify_evidence` is called directly from many tests
    # with two-element units and with a bare string, and a unit that carries no separate
    # de-hyphenated projection is its own projection — which is exactly right for text
    # that was never line-broken.
    return tuple(u if len(u) >= 3 else (u[0], u[1], u[1]) for u in corpus)


def render_numbers(doc: PaperDoc) -> str:
    out = []
    for n in doc.reported_numbers:
        where = n.table_ref or f"p{n.page}"
        if n.table_ref:
            out.append(f"- [{where}] {n.method} · {n.metric} = {n.value}"
                       f"{f' (±{n.seeds_or_variance})' if n.seeds_or_variance else ' (NO VARIANCE REPORTED)'}"
                       f"  << {n.benchmark}")
        else:
            out.append(f"- [{where}] prose claim: \"{n.source_quote}\"")
    return "\n".join(out)


def context(doc: PaperDoc, part=None, anchor=None) -> dict[str, str]:
    """Everything a lens prompt is built from, for one pass over the paper.

    `sections_text` is the only field that depends on WHICH pass this is. Tables, figure
    captions, equations and the paper's reported quantities are repeated in every part
    deliberately: they are addressed globally (`T2:r3:c4` means the same thing in every
    pass), a concern about a cell is only citable by a reader that can see the cell, and
    the character budget this module owns has only ever governed section prose.

    `part=None` renders the whole paper in one pass. It does NOT fall back to
    `pdf.render_sections`, which is the function this work exists to stop using: it
    divides one budget equally across sections and hard-slices each, so it cut a paper
    that fits — measured at 34% to 84% of the extracted prose across the evaluated corpus.
    """
    if part is None:
        whole = pdf.plan_reading(list(doc.sections), max(400, budget_chars()))
        sections_text = pdf.render_part(whole[0]) if whole else ""
    else:
        sections_text = (reading.render_part(part, anchor) if anchor is not None
                         else pdf.render_part(part))
    return {
        "sections_text": sections_text,
        "tables_text": pdf.render_tables(doc.tables),
        "claims_text": "(none pre-extracted — identify the paper's claims yourself "
                       "from the sections below; that judgement is part of your job)",
        "numbers_text": render_numbers(doc),
        "pdf_path": doc.source_path,
        "figures_text": pdf.render_figures(doc.figures),
        "equations_text": pdf.render_equations(doc.equations),
    }


def _header(lens: str, pid: str, unit_id: str = "", out_name: str = "") -> str:
    """The driver-facing preamble on `audit/prompts/<lens>.md`.

    States the ONE rule that holds on every channel — never write `audit/<lens>.json`
    yourself — without asserting which channel is in use. An earlier version said "only
    what you print is read" and "a file you write directly is discarded unread", which
    is true of `audit_driver.run_lens` (it captures stdout) and FALSE of the manual
    channel (`accept_lens` / `run.py accept`, which reads a staged file). A blinded
    reviewer asked to stage its output correctly noticed the prompt contradicted the
    instruction it had been given, and was right to; a prompt that describes one
    transport as if it were the only one makes the other look like a violation.
    """
    out_name = out_name or f"{lens}.json"
    unit_id = unit_id or lens
    return f"""<!-- generated by `run.py stage audit --paper {pid}` — do not edit -->

# Audit lens: `{unit_id}`  ·  paper `{pid}`

**Driver instructions.** Perform this audit now and return a single JSON object matching
the schema at the end of this file. Print it to standard output unless whoever dispatched
you named a different destination — some channels capture stdout, others collect a staged
file, and either way the harness validates what it receives and writes the result itself.

**Never write `{out_name}` directly.** That path is the harness's to write, after
validation. A file placed there by hand carries no provenance record and is refused by
`lens_is_accepted`, so it cannot be counted as a lens that ran — see that function for
why file existence is not provenance.

Run each lens in a SEPARATE session and do not let one lens's findings influence another
— they are meant to be independent readings, and four readings that have seen each other
are one reading repeated four times.

Every finding is re-verified when the report is built: a finding whose
`evidence_quote` is not actually present in this paper, or whose cited cell does not
contain what it says, is DROPPED. Quote exactly.

---

"""



# --------------------------------------------------------------------------- #
# ONE READING UNIT — a part, a synthesis, or a whole paper
# --------------------------------------------------------------------------- #
# A paper that fits in one pass is one unit per lens and the artifact layout is unchanged.
# A paper that does not is N part units plus one synthesis unit per lens, and the lens
# file downstream reads is COMPOSED from them (`compose_lens`) rather than written by any
# single reading. The composition is not a weakening of provenance: each part carries its
# own sealed sidecar and the composed record names every one of them, so "which reader
# produced this finding" is answerable per finding rather than per paper.
UNIT_KINDS = ("whole", "part", "synthesis")
SYNTHESIS_ID = "synthesis"
# The `written_by` token on a composed lens file. Distinct from every delegation mode
# because nothing delegated wrote it — this harness assembled it from artifacts that were
# themselves sealed, and a reader must be able to tell those two apart.
COMPOSED_WRITER = "composed_from_parts"


def part_id_of(number: int) -> str:
    """Zero-padded so a directory listing sorts in reading order at 10 parts and beyond."""
    return f"part-{number:02d}"


def _part_number(part_id: str) -> int:
    """`part-03` -> 3. Returns 0 for anything that is not a part id, including SYNTHESIS_ID."""
    tail = (part_id or "").rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


class AuditUnit(NamedTuple):
    lens: str
    kind: str                  # whole | part | synthesis
    part_id: str               # 'part-01' | 'synthesis' | '' for a whole-paper unit
    prompt_path: Path
    out_path: Path
    # Under `audit/reading/`, not beside the output. `audit/*.json` has meant "a lens
    # result" since this harness existed — `run.py`, the controller's completeness list
    # and at least one test all read the directory that way — and dropping an input record
    # into it makes an unaudited paper look like it holds audit artifacts. The input
    # records get their own tree instead.
    manifest_path: Path

    @property
    def unit_id(self) -> str:
        """Stable across runs, and the key every caller addresses this unit by."""
        return self.lens if self.kind == "whole" else f"{self.lens}/{self.part_id}"

    @property
    def sidecar_path(self) -> Path:
        return self.out_path.with_suffix(".driver.json")


def plan_for(doc: PaperDoc) -> reading.ReadingPlan:
    """The reading strategy for this paper, at the budget the prompts will actually use."""
    return reading.plan(doc, budget_chars())


def units_for(root: Path, lenses: tuple[str, ...], plan: reading.ReadingPlan) -> list[AuditUnit]:
    """Every reading this paper needs, in dispatch order.

    Parts before that lens's synthesis, and lenses in declaration order. The order matters
    only for reporting — the parts of one lens are independent of each other and could run
    in any order or at once — but a synthesis cannot be dispatched before the parts it
    reads, and `run_audit` refuses to render its prompt until they are sealed.
    """
    audit = root / "audit"
    pdir, mdir = audit / "prompts", audit / "reading"
    out: list[AuditUnit] = []
    n = max(1, plan.coverage.parts)
    for lens in lenses:
        if n == 1:
            out.append(AuditUnit(lens, "whole", "", pdir / f"{lens}.md",
                                 audit / f"{lens}.json",
                                 mdir / f"{lens}.manifest.json"))
            continue
        for i in range(1, n + 1):
            pid_ = part_id_of(i)
            out.append(AuditUnit(lens, "part", pid_, pdir / lens / f"{pid_}.md",
                                 audit / lens / "parts" / f"{pid_}.json",
                                 mdir / lens / f"{pid_}.manifest.json"))
        out.append(AuditUnit(lens, "synthesis", SYNTHESIS_ID,
                             pdir / lens / f"{SYNTHESIS_ID}.md",
                             audit / lens / f"{SYNTHESIS_ID}.json",
                             mdir / lens / f"{SYNTHESIS_ID}.manifest.json"))
    return out


def _sealed(path: Path) -> tuple[bool, str]:
    """Does a sealed provenance sidecar beside `path` describe `path`'s CURRENT bytes?

    A thin wrapper around `harness.sealing.verify_seal`, so a part artifact and a composed
    lens artifact are held to the identical standard rather than to a second implementation
    of it. See `lens_is_accepted` for the real six-paper run this check exists because of.
    """
    return sealing.verify_seal(path, accepted_writers=_ACCEPTED_WRITERS)


def unit_is_accepted(unit: AuditUnit) -> tuple[bool, str]:
    """A reading counts only if it is sealed AND was produced against the prompt on disk.

    The second half is what makes resume correct rather than merely cheap. `run_audit`
    regenerates every prompt on every invocation, so a part whose span moved — because the
    budget changed, or extraction changed, or an earlier section grew — has a sealed output
    that answers a question nobody is asking any more. Re-reading it is the only honest
    option, and `prompt_is_unchanged` reports `unpinned` rather than `changed` for a sidecar
    written before prompts were fingerprinted, so nothing already on disk is retroactively
    invalidated.

    A whole-paper unit is deliberately NOT prompt-pinned: that is the historical path, the
    seven shipped reviews and the evaluated corpus were produced under it, and tightening
    it here would invalidate them for a reason that has nothing to do with this work.
    """
    ok, why = _sealed(unit.out_path)
    if not ok or unit.kind == "whole":
        return ok, why
    fresh, note = audit_driver.prompt_is_unchanged(unit.sidecar_path, unit.prompt_path)
    if not fresh:
        return False, note
    return True, ""


def lens_units(units: list[AuditUnit], lens: str) -> list[AuditUnit]:
    return [u for u in units if u.lens == lens]


def lens_is_accepted(root: Path, lens: str) -> tuple[bool, str]:
    """A lens result counts only if the HARNESS recorded writing it.

    File existence is not provenance. `audit/<lens>.json` can exist because a reviewer
    with filesystem access wrote it directly, bypassing `parse_lens_json`'s validation
    (the `evidence_quote`-without-`evidence_ref` refusal among others) and the
    staging/promotion step `run_lens` exists to provide — which is exactly what happened
    on a real six-paper run: three reviewers side-wrote their lens file, their prose
    stdout was correctly rejected, no `.driver.json` sidecar was ever created for those
    lenses, and `run_audit`/`load_reports` (checking file existence alone) counted them
    as complete anyway. Findings reached reports with no record of who produced them and
    with the driver's own validation gate bypassed.

    Checked, in order: the lens file exists; a `.driver.json` sidecar exists beside it;
    `written_by` names a path that actually validates (`audit_driver` or
    `manual_accept`, see `accept_lens`); the sidecar's `content_sha256` matches the lens
    file's CURRENT bytes — the seal that makes this a real check rather than a token,
    since a file edited after acceptance is no longer the file that was accepted.

    ONE predicate, TWO callers (`run_audit`'s completeness list and `load_reports`) —
    the same discipline `_substantiated` already has as the boolean shadow of
    `verify_evidence`, for the same reason: if the two callers could disagree about
    whether a lens counts, the controller's audit/collect phases would oscillate
    (`_phase_audit` reports `ok` on an empty `awaiting` while `_phase_collect` reports
    `waiting` on the same lens), burning the bounded retry loop for no reason.
    """
    ok, why = _sealed(root / "audit" / f"{lens}.json")
    return ok, ("no lens file" if why == "no output file" else why)


# Every `written_by` token a validated path can produce, read off the delegation
# vocabulary rather than written out here. Two copies of this set would let a mode be
# added to one and refused by the other, which surfaces as "the reviewer never ran".
# `COMPOSED_WRITER` joins them because a lens file assembled from sealed part artifacts
# has provenance — it is just provenance by reference rather than by delegation, and
# `compose_lens` writes every referenced sidecar's hash into the composed record so the
# chain is walkable. Without it, a multi-part lens could never satisfy `lens_is_accepted`
# no matter how well its parts were sealed.
_ACCEPTED_WRITERS = tuple(delegation.WRITTEN_BY.values()) + (COMPOSED_WRITER,)


def accept_lens(cfg: Config, pid: str, lens: str, raw: str, *,
                reviewer: str = "", tool_policy: str = "unrecorded",
                mode: str = "MANUAL") -> dict:
    """Validate and persist a HAND-WRITTEN lens file through the SAME gate the
    auto-audit path uses, closing the asymmetry `lens_is_accepted` exists to police:
    before this, a manually-written `audit/<lens>.json` skipped `parse_lens_json`
    entirely (no ref-less-quote refusal, no structural check) and had no sidecar, so it
    could never itself satisfy `lens_is_accepted`. One validation path for both channels.

    VALIDATION IS THE SAME; ISOLATION IS NOT, and the sidecar has to say so. The
    auto-audit path records `tool_policy` because it can prove what it enforced: an
    empty sandbox cwd, `--allowedTools` restricted to what the lens is granted, and
    `--add-dir` limited to the papers directory. Nothing reaching this function has any
    of that, and a reader comparing two reports must not have to guess which channel
    produced a lens. `tool_policy` defaults to `unrecorded` rather than to anything
    reassuring, and `reviewer` names whatever actually did the reading.
    """
    report = audit_driver.parse_lens_json(raw, lens)
    root = state.project_dir(cfg, pid)
    out = root / "audit" / f"{lens}.json"
    # WHICH MODE, recorded, and `harness.delegation` decides what that mode is entitled
    # to claim. `mode` defaults to MANUAL because that is the mode that promises least: a
    # caller who does not say gets the token asserting no isolation, rather than
    # inheriting an autonomous mode's guarantees by omission. This function used to
    # hard-code `manual_accept`, which made an isolated subagent the controller dispatched
    # autonomously and a human pasting JSON into a file the same provenance.
    return sealing.seal(out, report.model_dump(), mode=mode, reviewer=reviewer,
                        tool_policy=tool_policy,
                        extra={"lens": lens, "paper_id": pid,
                               "findings": len(report.findings)})


def claimlink_prompt(doc: PaperDoc) -> str:
    """The claim-link reading's prompt, rendered here beside the lens prompts.

    Rendered unconditionally, gate open or shut, exactly as `audit/prompts/<lens>.md` is:
    a prompt on disk is what makes the manual channel possible on a host where no CLI is
    reachable, and `claimlink_driver.accept` is the door it goes back in through.

    Shown the Abstract and the Conclusion in full and the paper's addressable evidence,
    and NOT the body prose — see `prompts.claimlink.build` for why that is a bound on the
    task rather than a saving.
    """
    ctx = context(doc)
    by_idx = {sec.section_idx: sec for sec in doc.sections}
    abstract_idx = materiality.abstract_section_idx(doc)
    conclusion_idx = materiality.conclusion_section_idx(doc)
    return CLP.build(
        title=doc.title,
        abstract_text=(by_idx[abstract_idx].text if abstract_idx in by_idx else ""),
        conclusion_text=(by_idx[conclusion_idx].text if conclusion_idx in by_idx else ""),
        tables_text=ctx["tables_text"], figures_text=ctx["figures_text"],
        equations_text=ctx["equations_text"], numbers_text=ctx["numbers_text"],
        pdf_path=ctx["pdf_path"])


def _locate(quote: str, corpus) -> int | None:
    """Which parsed section contains this quote, or None. Used only to LABEL a candidate."""
    q = _flat(quote)
    if not q:
        return None
    return next((idx for idx, unit, _proj in _units(corpus) if q in unit), None)


def synthesis_inputs(doc: PaperDoc, lens: str,
                     units: list[AuditUnit]) -> dict[int, list[dict]]:
    """That lens's own VERIFIED part-local observations, keyed by part number.

    Verified, not merely produced. `load_reports` re-checks every quotation against the
    parsed paper before a finding reaches a report, and a synthesis reasoning over
    candidates that had not yet passed that check would be building cross-section concerns
    on top of quotations which may not be in the paper at all — the one thing this harness
    never lets a model do. So the same gate runs here, one stage earlier.

    Nothing about another lens, a grade, a target outcome or any decision is reachable
    from this function's arguments. The exclusion is a signature rather than a convention,
    which is the same discipline `coverage.surface` and `grading.derive` already have.
    """
    corpus = source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    out: dict[int, list[dict]] = {}
    for unit in units:
        if unit.lens != lens or unit.kind != "part":
            continue
        number = _part_number(unit.part_id)
        try:
            data = state.read_json(unit.out_path)
        except Exception:
            out[number] = []
            continue
        kept: list[dict] = []
        for f in (data.get("findings") or []) if isinstance(data, dict) else []:
            if not isinstance(f, dict):
                continue
            quote = str(f.get("evidence_quote") or "").strip()
            ref = str(f.get("evidence_ref") or "").strip()
            if not _substantiated(quote, ref, corpus, by_idx):
                continue
            kept.append({
                "evidence_quote": quote, "evidence_ref": ref,
                "statement": str(f.get("statement") or f.get("title") or ""),
                "finding_id": str(f.get("finding_id") or ""),
                "section_idx": _locate(quote, corpus),
                "page": None,
            })
        out[number] = kept
    return out


def _write_manifest(unit: AuditUnit, *, pid: str, part, anchor, prompt_text: str,
                    budget: int, inputs: dict | None = None) -> dict:
    """WHAT WENT IN, written when the prompt is.

    This is the artifact that makes the isolation claim checkable rather than asserted. It
    records the span this reading was given — the section ids and the exact character
    offsets inside each — the digest of the repeated anchor packet, and the digest of the
    prompt bytes themselves. A test does not have to take the harness's word that part 2
    never saw part 1's findings: it can hash the prompt this manifest names and read it.

    Deliberately NOT sealed with a `.driver.json`. A seal certifies that an OUTPUT came
    through a validated path; this is an input record the harness wrote about itself, and
    sealing it would dress a self-description up as an attestation. The prompt digest is
    what binds it to the reading, and `unit_is_accepted` checks that digest against the
    prompt on disk — so a manifest that no longer matches invalidates the reading instead
    of certifying it.
    """
    rec = {
        "paper_id": pid, "lens": unit.lens, "unit_id": unit.unit_id,
        "kind": unit.kind, "part_id": unit.part_id,
        "prompt_path": str(unit.prompt_path),
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "budget_chars": budget,
        "anchor_sha256": (hashlib.sha256(anchor.render().encode("utf-8")).hexdigest()
                          if anchor is not None and unit.kind != "whole" else ""),
        "section_ids": ([s.section_idx for s in part.sections] if part is not None
                        else "whole-paper"),
        "slices": ([list(t) for t in part.slices] if part is not None else []),
        "part_number": getattr(part, "number", 1) if part is not None else 1,
        "part_total": getattr(part, "total", 1) if part is not None else 1,
        "chars": getattr(part, "chars", 0) if part is not None else 0,
        "ts": state.now(),
    }
    if inputs is not None:
        # The synthesis's inputs, named rather than described: which of that lens's own
        # candidates reached it, by finding id. A reader can open each one and confirm it
        # came from this lens and from a part of this paper.
        rec["synthesis_inputs"] = inputs
    unit.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(unit.manifest_path, rec)
    return rec


def compose_lens(cfg: Config, pid: str, lens: str,
                 units: list[AuditUnit]) -> dict | None:
    """Assemble `audit/<lens>.json` from that lens's sealed part and synthesis artifacts.

    Returns the composed provenance record, or None while any unit is still missing. A
    lens is never half-composed: a lens file holding three of four parts is
    indistinguishable downstream from a lens that read the whole paper and found less.

    Composition adds nothing and is deterministic. Every finding is carried through
    verbatim apart from two harness-written labels — `source_part`, which names the
    reading that produced it, and a disambiguated `finding_id` where two readings happened
    to choose the same one. Verification, deduplication, the evidence ceiling, the
    severity caps and independent grading all still run downstream over the composed file
    exactly as they do over a single-pass one, so a concern the synthesis proposed is held
    to the identical standard as one a part reader raised.
    """
    mine = [u for u in units if u.lens == lens and u.kind in ("part", "synthesis")]
    if not mine or not all(unit_is_accepted(u)[0] for u in mine):
        return None
    root = state.project_dir(cfg, pid)
    findings: list[dict] = []
    composed_from: list[dict] = []
    seen_ids: set[str] = set()
    notes: list[str] = []
    unasked = ""
    for unit in mine:
        data = state.read_json(unit.out_path)
        side = state.read_json(unit.sidecar_path)
        raw = (data.get("findings") or []) if isinstance(data, dict) else []
        for i, f in enumerate(raw):
            if not isinstance(f, dict):
                continue
            f = dict(f)
            fid = str(f.get("finding_id") or "").strip() or f"{lens}-{unit.part_id}-{i + 1:02d}"
            if fid in seen_ids:
                fid = f"{unit.part_id}:{fid}"
            seen_ids.add(fid)
            f["finding_id"] = fid
            # HARNESS-WRITTEN. `audit_driver.strip_harness_keys` removes whatever a reading
            # tried to put here before its artifact was sealed, so this is the only writer.
            f["source_part"] = unit.part_id
            findings.append(f)
        if isinstance(data, dict):
            note = str(data.get("notes") or "").strip()
            if note:
                notes.append(f"[{unit.part_id}] {note}")
            question = str(data.get("unasked_question") or "").strip()
            # The synthesis saw this lens's observations from the whole paper, so its
            # answer to "what comparison did this paper avoid" is the one to keep; a part
            # reader answered it from one span.
            if question and (unit.kind == "synthesis" or not unasked):
                unasked = question
        composed_from.append({
            "unit_id": unit.unit_id, "kind": unit.kind, "path": str(unit.out_path),
            "content_sha256": side.get("content_sha256", ""),
            "written_by": side.get("written_by", ""),
            "prompt_sha256": side.get("prompt_sha256", ""),
            "model_reported": (side.get("envelope") or {}).get("model_reported", ""),
            "findings": len(raw),
        })
    out = root / "audit" / f"{lens}.json"
    payload = {"lens": lens, "schema_version": 2, "findings": findings,
              "unasked_question": unasked, "notes": "  ".join(notes)}
    # `compose_lens` has no real `mode`/`reviewer` to record — it is a synthesis over
    # already-sealed parts, not a delegated reading of its own. `mode="MANUAL"` produces
    # harmless defaults, and `extra`'s own explicit `written_by`/`tool_policy` below
    # override them, exactly preserving this artifact's literal historical content.
    return sealing.seal(out, payload, mode="MANUAL", extra={
        "lens": lens, "paper_id": pid, "written_by": COMPOSED_WRITER,
        "composed_from": composed_from,
        "parts": sum(1 for u in mine if u.kind == "part"),
        "synthesis": any(u.kind == "synthesis" for u in mine),
        "findings": len(findings),
        # NOT a tool policy this harness enforced. Each reading records its own, and a
        # composed artifact must not inherit a guarantee it did not produce.
        "tool_policy": "composed; each reading in composed_from records its own policy",
    })


def reading_record(cfg: Config, pid: str, doc: PaperDoc,
                   lenses: tuple[str, ...] = LENSES) -> dict:
    """How much of this paper reached a reader, and how the reading was carried out.

    Reported as separate numbers for the reason the four axes are kept apart: they answer
    different questions, and one of them — `reader_visible_fraction` — is the ceiling on
    every recall claim this system makes. None of them is issue recall, and
    `CoverageReport.semantic_coverage` says so in the same artifact.

    `extracted_text_fraction` is about EXTRACTION and `reader_visible_fraction` is about
    the reader, and they compose: a page extraction recovered nothing from is invisible to
    a reader however complete the reading plan is. Its denominator is the PDF's own page
    count, so it cannot be inflated by a worse extractor the way a rate computed over
    recovered objects can.

    WHAT IT DOES NOT SAY, because it reads 1.000 on all twelve documents in this
    repository and a saturated measure invites over-reading. It says extraction recovered
    prose covering every page; it says nothing about how much of each page it got, which
    is the harder question and one this artifact cannot answer — the only denominator for
    that is the PDF's own text, and comparing against it is a second extraction. What
    bounds a review is `reader_visible_fraction` over what extraction actually produced,
    and the extraction ceiling underneath it stays unmeasured rather than
    measured-and-good.
    """
    root = state.project_dir(cfg, pid)
    plan = plan_for(doc)
    units = units_for(root, lenses, plan)
    cov = plan.coverage
    syntheses = [u for u in units if u.kind == "synthesis"]
    # EVERY page a recovered section covers, not only the page it starts on. A section
    # running from page 3 to page 8 makes text available on six pages, and counting its
    # `page_start` alone reported 11 of 25 pages for `acl` — 44%, against a document every
    # page of which extraction did recover prose from. A fraction that low would have read
    # as an extraction defect that is not there.
    pages: set[int] = set()
    for sec in doc.sections:
        if not (sec.text or "").strip():
            continue
        last = sec.page_end if sec.page_end >= sec.page_start else sec.page_start
        pages.update(range(sec.page_start, last + 1))
    pages.discard(0)
    return {
        "extracted_prose_chars": cov.extracted_prose_chars,
        "pages_with_text": len(pages),
        "pages_total": doc.n_pages or 0,
        "extracted_text_fraction": (len(pages) / doc.n_pages) if doc.n_pages else None,
        "reader_visible_chars": cov.part_local_chars,
        "reader_visible_fraction": cov.part_local_fraction,
        "anchor_chars": cov.anchor_chars,
        "anchor_repeat_chars": cov.anchor_repeat_chars,
        "anchor_repeat_fraction": cov.anchor_overhead_fraction,
        "number_of_parts": cov.parts,
        "lenses_total": len(lenses),
        "lenses_completed": sum(1 for ln in lenses if lens_is_accepted(root, ln)[0]),
        "lens_syntheses_required": len(syntheses),
        "lens_syntheses_completed": sum(1 for u in syntheses if unit_is_accepted(u)[0]),
    }


def missing_reading_artifacts(cfg: Config, pid: str,
                              lenses: tuple[str, ...] = LENSES) -> list[str]:
    """Reading units this paper needs and does not have. Empty is the only clean answer.

    Consulted at the LAST gate rather than only at the first. A review whose synthesis
    never ran has not been conducted the way the review says it was, and the difference is
    invisible in the finished report: the parts alone still produce findings, still grade,
    and still render. The audit phase can be resumed, rewound, or skipped past on a cached
    case, so a completeness check only that phase performs is a completeness check that a
    resumed run never makes.
    """
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return ["paper/doc.json"]
    doc = PaperDoc(**state.read_json(doc_path))
    units = units_for(root, lenses, plan_for(doc))
    missing = [u.unit_id for u in units if not unit_is_accepted(u)[0]]
    missing += [f"{ln}.json" for ln in lenses if not lens_is_accepted(root, ln)[0]]
    return missing


def run_audit(cfg: Config, pid: str, lenses: tuple[str, ...] = LENSES) -> dict:
    """Write one prompt file per reading unit, and compose any lens whose units are in.

    Does not call a model. A paper that fits in one pass produces exactly what it always
    did: one prompt and one lens file per lens, no synthesis, no extra call. A paper that
    does not produces N part prompts per lens, then — once those parts are sealed — one
    synthesis prompt per lens, and finally a composed lens file per lens.

    The synthesis prompt is deliberately not written until its parts exist, and that is a
    correctness property rather than an optimisation. The brief IS that lens's own
    part-local observations, so a synthesis prompt written early would be a synthesis over
    nothing — and a reading that returned no cross-part concerns because it had no inputs
    is indistinguishable, in the artifact, from one that looked and found none.
    """
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "audit")

    plan = plan_for(doc)
    units = units_for(root, lenses, plan)
    by_number = {part.number: part for part in plan.parts}
    pdir = root / "audit" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    deferred: list[str] = []
    for unit in units:
        part = by_number.get(_part_number(unit.part_id)) if unit.kind == "part" else None
        if unit.kind == "synthesis":
            siblings = [u for u in units if u.lens == unit.lens and u.kind == "part"]
            if not all(unit_is_accepted(u)[0] for u in siblings):
                deferred.append(unit.unit_id)
                continue
            brief = reading.synthesis_brief(pid, unit.lens, plan.anchor,
                                            synthesis_inputs(doc, unit.lens, units))
            ctx = context(doc)
            body = P.build_synthesis(
                unit.lens, doc.title, brief.render(),
                "\n".join(f"  - {t}" for t in reading.SYNTHESIS_TARGETS),
                tables_text=ctx["tables_text"], figures_text=ctx["figures_text"],
                equations_text=ctx["equations_text"], pdf_path=ctx["pdf_path"])
            inputs = {"parts_read": brief.parts_read,
                      "candidates": len(brief.candidates),
                      "candidate_finding_ids": [c["finding_id"] for c in brief.candidates]}
        else:
            ctx = context(doc, part, plan.anchor if unit.kind == "part" else None)
            note = P.part_note(part.label) if part is not None else ""
            body = P.build(unit.lens, doc.title, reading_note=note, **ctx)
            inputs = None
        # Sanitised at the boundary, not at extraction: `doc.json` stays byte-faithful to
        # what the PDF gave up, and this is where the paper's text leaves the harness for
        # another process's stdin. Real prompts carry these today — each
        # `projects/iclr/audit/prompts/*.md` holds 3 NUL bytes and 30 other control
        # characters, straight out of PDF text extraction. Not a live failure on this
        # host, but a reader that truncates at NUL would fail as "the command exited
        # without writing anything", which `audit_driver` itself notes is the one shape of
        # failure it cannot attribute to a cause.
        body = pdf.sanitise_controls(
            _header(unit.lens, pid, unit.unit_id, unit.out_path.name) + body)
        unit.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        unit.prompt_path.write_text(body, encoding="utf-8")
        written[unit.unit_id] = str(unit.prompt_path)
        _write_manifest(unit, pid=pid, part=part, anchor=plan.anchor, prompt_text=body,
                        budget=budget_chars(), inputs=inputs)

    # The claim-link prompt, beside the lens prompts and on the same terms: written every
    # run, never edited by hand, and the only way a host with no reachable CLI can supply
    # the one correspondence the paper does not print.
    link_prompt = root / "links" / "prompt.md"
    link_prompt.parent.mkdir(parents=True, exist_ok=True)
    link_prompt.write_text(pdf.sanitise_controls(claimlink_prompt(doc)), encoding="utf-8")

    done = [u.unit_id for u in units if unit_is_accepted(u)[0]]
    todo = [u.unit_id for u in units if u.unit_id not in done and u.unit_id in written]

    # Compose every lens whose readings are all in. Idempotent: the composed bytes are a
    # pure function of the part artifacts, so re-running writes the same file and the same
    # seal rather than invalidating what a previous run sealed.
    composed: dict[str, int] = {}
    if plan.coverage.parts > 1:
        for lens in lenses:
            record = compose_lens(cfg, pid, lens, units)
            if record:
                composed[lens] = record["findings"]

    cov = plan.coverage
    state.append_log(
        cfg, pid, artifact_type="audit_prompts", phase="audit",
        headers={"lenses": list(lenses), "units": [u.unit_id for u in units],
                 "awaiting": todo, "deferred": deferred, "complete": done,
                 "parts": cov.parts,
                 "reader_visible_fraction": cov.part_local_fraction,
                 "anchor_repeat_fraction": cov.anchor_overhead_fraction,
                 "chars": sum(len(Path(v).read_text(encoding="utf-8"))
                              for v in written.values())},
        path=str(pdir),
    )
    return {
        "paper_id": pid, "title": doc.title,
        "prompts": written,
        "units": [{"unit_id": u.unit_id, "lens": u.lens, "kind": u.kind,
                   "part_id": u.part_id, "prompt": str(u.prompt_path),
                   "out": str(u.out_path)} for u in units],
        "awaiting": todo, "deferred": deferred, "complete": done,
        "lenses_complete": [ln for ln in lenses if lens_is_accepted(root, ln)[0]],
        "claimlink_prompt": str(link_prompt),
        "composed": composed, "parts": cov.parts,
        "reader_visible_fraction": cov.part_local_fraction,
        "next": (f"Read each prompt in audit/prompts/, perform that reading, and return "
                 f"its JSON. Awaiting: {', '.join(todo) or 'none'}."
                 + (f" Deferred until their parts are in: {', '.join(deferred)}."
                    if deferred else ""))
        if (todo or deferred) else "Every reading is in; run synthesize_report.",
    }


def load_reports(cfg: Config, pid: str,
                 doc: PaperDoc) -> tuple[list[LensReport], int, list[str]]:
    """Load every lens result, dropping findings that cannot be substantiated.

    Returns (reports, n_dropped, invalid_lenses). A lens whose file is missing,
    unparseable, empty, `null`, or not the `{"findings": [...]}` shape a lens report has
    to be is named in `invalid_lenses` — it still yields a report (so `lenses_run`
    bookkeeping and diagnostics stay visible) but MUST NOT be read as "this lens ran and
    found nothing": the caller — `run_report` and the controller's `collect` phase — has
    to refuse a verdict rather than render one over a panel that never actually completed.
    A `{"findings": []}` file some lens genuinely wrote is not in this list; a lens that
    never produced a well-formed report at all is.
    """
    root = state.project_dir(cfg, pid)
    corpus = source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    by_figure = {f.figure_idx: f for f in doc.figures}
    by_equation = {e.equation_idx: e for e in doc.equations}
    max_page = doc.n_pages or 0
    reports, dropped, invalid = [], 0, []
    for lens in LENSES:
        path = root / "audit" / f"{lens}.json"
        if not path.exists():
            invalid.append(lens)
            continue
        ok, why = lens_is_accepted(root, lens)
        if not ok:
            # A file at this path that the harness did not seal — see `lens_is_accepted`.
            # Named explicitly rather than silently invalid, because the operator's fix
            # (re-run the lens) is different from the fix for a malformed file.
            reports.append(LensReport(lens=lens, notes=f"(no provenance record: {why})"))
            invalid.append(lens)
            continue
        try:
            raw = state.read_json(path)
        except Exception:
            reports.append(LensReport(lens=lens, notes="(unparseable lens file)"))
            invalid.append(lens)
            continue
        report, n, valid = _coerce(lens, raw, corpus, by_idx, max_page,
                                   by_figure=by_figure, by_equation=by_equation)
        reports.append(report)
        dropped += n
        if not valid:
            invalid.append(lens)
    return reports, dropped, invalid


def verify_evidence(quote: str, ref: str, corpus: str | Sequence[tuple[int, str]],
                    by_idx: dict, max_page: int = 0, *,
                    by_figure: dict[int, Figure] | None = None,
                    by_equation: dict[int, Equation] | None = None) -> tuple[str, str]:
    """(evidence_class, verified_observation) — WHAT THE HARNESS ITSELF CONFIRMED.

    This is the machine half of a finding, and the reason it lives here rather than in
    the lens file is that a lens must not be able to assert that its own reasoning was
    checked. Everything a lens writes is a claim; everything this function returns is an
    observation, and the report renders the two apart so a reader can tell which is which.

    The observation text is generated, never copied: it names the address, states that the
    contents matched, and quotes what was found. A reader can re-run the same comparison
    from `doc.json` in a few seconds.

    `max_page`, when given, bounds a `p<N>` reference against the paper's own page count.
    Optional (0 = unchecked) so the many unit tests that exercise this function directly
    do not have to carry a page count they have no opinion about; `load_reports` — the one
    caller that matters for a real review — always passes the parsed paper's `n_pages`.
    """
    q = _flat(quote)
    if not q or _MEANINGLESS_QUOTE.fullmatch(q):
        # A quote with no letter and no digit — a bare arrow, a bullet, a dash, a lone
        # punctuation mark — cannot support a claim about anything, however exactly it
        # matches a cell. `cell_verified` is the STRONGEST evidence class this harness
        # grants, and existence of a cell is not the same as existence of a finding: a
        # trivial fragment earning it let a report cite "⇑" as strong, checkable evidence
        # for a claim the symbol says nothing about.
        return "unverified", ""
    ref = (ref or "").strip()
    m = _CELL_REF.fullmatch(ref)
    if m:
        t = by_idx.get(int(m.group(1)))
        if t is None:
            return "unverified", ""
        cell = t.cell(int(m.group(2)), int(m.group(3)))
        if _flat(cell) != q:
            return "unverified", ""
        return "cell_verified", (
            f"Cell {ref} of the parsed paper contains {cell.strip()!r}, which matches the "
            f"quoted evidence character for character after whitespace normalisation.")
    fm = _FIG_REF.fullmatch(ref)
    if fm:
        fig = (by_figure or {}).get(int(fm.group(1)))
        if fig is None or len(q) < _QUOTE_MIN or q not in _flat(fig.caption):
            return "unverified", ""
        return "caption_verified", (
            f"Figure caption {ref} (page {fig.page}, {fig.label!r}) of the parsed paper "
            f"contains the quoted text verbatim. A CAPTION NAMES A FIGURE; it does not "
            f"report the figure's plotted values, and nothing about those values is "
            f"verified here.")
    em = _EQ_REF.fullmatch(ref)
    if em:
        eq = (by_equation or {}).get(int(em.group(1)))
        if eq is None or len(q) < _QUOTE_MIN or q not in _flat(eq.text):
            return "unverified", ""
        return "equation_verified", (
            f"Equation {ref} (page {eq.page}"
            f"{f', numbered {eq.number}' if eq.number else ''}) of the parsed paper contains "
            f"the quoted text verbatim. Display-equation extraction is lossy: symbols, "
            f"sub/superscripts and inline math may be missing or mangled, and this verifies "
            f"the extracted text only.")
    pm = _PAGE_REF.fullmatch(ref)
    if not pm:
        return "unverified", ""
    if max_page > 0:
        page_num = int(pm.group()[1:])
        if page_num < 1 or page_num > max_page:
            # An impossible reference: this paper does not have a page `page_num`. A quote
            # that happens to occur somewhere in the corpus is not evidence for a claim
            # anchored to a page that cannot exist, and letting it verify anyway is exactly
            # the "page/table/cell references are actually valid" gap this check closes.
            return "unverified", ""
    if len(q) < _QUOTE_MIN:
        return "unverified", ""
    # WITHIN ONE SECTION. A quote spanning two sections is not in the paper.
    units = _units(corpus)
    hit = next((idx for idx, unit, _proj in units if q in unit), None)
    dehyphenated = False
    if hit is None:
        # THE TYPESETTER'S HYPHEN, and only after the exact search has failed. A quote
        # that matches the paper character for character is never resolved through a
        # normalisation; this recovers the one that differs only by a hyphen a line break
        # inserted, which over the evaluated corpus is 4 of the 5 findings this gate drops.
        # The observation below says so, because "occurs verbatim" would then be a false
        # machine attestation — the exact defect `source_units` itself was written to stop.
        from ..claims import _self_projection, soft_hyphen_projection
        probe = soft_hyphen_projection(*_self_projection(q))[0]
        if len(probe) >= _QUOTE_MIN:
            hit = next((idx for idx, _unit, proj in units if probe in proj), None)
            dehyphenated = hit is not None
    if hit is None:
        return "unverified", ""
    where = f"section_idx {hit}" if hit >= 0 else "the parsed section text"
    # "VERBATIM" IS DROPPED WHEN IT WOULD BE FALSE, and only then. An exact match is
    # verbatim and says so; a match recovered by removing a line break's hyphen is not,
    # and claiming it were would be a false machine attestation of exactly the kind
    # `source_units` itself exists to prevent.
    how = ("occurs verbatim inside" if not dehyphenated else
           "occurs inside")
    note = ("" if not dehyphenated else
            " — matched after removing the hyphens a line break inserted into the PDF, so "
            "the quotation differs from the extracted characters only by that hyphenation "
            "and is NOT a character-for-character match")
    return "prose_verified", (
        f"The quoted text {how} a single parsed section ({where}){note}; the lens "
        f"cited {ref}, which is NOT checked — only the section containing the quote is. "
        f"Verified as a substring of one section, not across sections and not as a page.")


def _substantiated(quote: str, ref: str, corpus: str | Sequence[tuple[int, str]],
                   by_idx: dict) -> bool:
    """Is this quote really in the paper — and if it cites a cell, is it that cell?

    The length floor applies to PROSE quotes only. A short prose fragment matches
    anywhere and proves nothing, but a cell citation is already pinned to exact
    coordinates, so "0.0001" at T1:r0:c1 is the most checkable evidence there is —
    and rejecting it would throw away precisely the table numbers this harness exists
    to audit.

    A MISSING OR MALFORMED `ref` IS NOT SUBSTANTIATED. This is the load-bearing line.
    Earlier the reference was optional: an absent `evidence_ref` fell through to the
    prose branch, so a finding that cited a table cell and then lost its reference in
    serialization was re-checked as a free-floating substring against the whole paper —
    and passed, because the cell's own text does appear somewhere in the corpus. The
    finding survived with its provenance deleted, silently demoted from "this exact cell
    says X" to "these characters occur somewhere", which is the difference between
    evidence an editor can check in seconds and no evidence at all. Requiring the
    reference to be present and well-formed means a lost one is DROPPED and counted,
    where the report already prints the count, instead of quietly downgraded.

    The predicate is now the boolean shadow of `verify_evidence`, which returns the same
    decision plus the machine-written observation that goes on the finding. One
    implementation, so the thing that DROPS a finding and the thing that DESCRIBES a kept
    one can never disagree about whether the evidence held.
    """
    return verify_evidence(quote, ref, corpus, by_idx)[0] != "unverified"


def weakest_evidence_class(classes: Sequence[str]) -> str:
    """The evidence class a MULTI-LOCATION concern is held to: its weakest side.

    A concern that depends on both a table cell and a figure caption is only as checkable
    as the caption, because a reader who cannot confirm that half cannot confirm the
    concern. So the confidence ceiling is computed from the weakest side, and the primary
    citation wins a tie so a single-location finding is unaffected.

    Deliberately NOT the other arrangement. `grading.evidence_support` LIFTS its ceiling
    one step per additional INDEPENDENT source, and passing a lens's own second citation
    in as such a source would let one reader corroborate itself — a mechanism that raises,
    which invariant 11 forbids. Two quotations chosen in one pass by one reader are one
    reader's judgement cited twice. What they buy is that BOTH halves are checkable, which
    is the difference between a cross-section concern and an assertion; they do not buy
    confidence, and here they can only cost it.
    """
    order = {c: grading.CONFIDENCE_RANK[grading.EVIDENCE_CONFIDENCE_CEILING.get(c, "HIGH")]
             for c in classes}
    return min(classes, key=lambda c: order[c]) if classes else "unverified"


def _address_identity(f: Finding) -> tuple:
    """This concern's IDENTITY: the ordered locations it was established from.

    Ordered rather than a set, because the two halves of a cross-section concern are not
    interchangeable — "the abstract claims what the conclusion concedes" and its reverse
    are different readings of the same two spans, and folding them together would report
    one where a referee raised two.

    The quotation is carried beside the reference because a reference alone is too coarse:
    `p7` is a whole page, and two genuinely different concerns can cite it.
    """
    sides = [(f.evidence_ref, _flat(f.evidence_quote))]
    sides += [(e.evidence_ref, _flat(e.evidence_quote)) for e in f.additional_evidence]
    return tuple(sides)


def _concern_identity(f: Finding) -> tuple:
    """WHAT KIND of concern this is, in closed vocabulary only.

    The lens's own classification rather than `scientific_class`, which is a SUMMARY of
    it: `taxonomy.classify` folds every `discrepancy_type` a contradiction lens can choose
    onto the single class CONTRADICTION, so two concerns that disagree about what is wrong
    become one token.

    Measured on the evaluated corpus, that is not hypothetical. `apt-icml` carries
    `protocol-03` — Table 2 prints 100.0% for merged LoRA's inference time when Table 11
    shows the same computation — and `protocol-09` — the abstract's speedup is normalised
    against LoRA+Prune rather than against fine-tuning. Both anchor on the same quoted row
    of page 20, both summarise to CONTRADICTION, and they are different concerns:
    GENUINE_CONTRADICTION against DIFFERENT_DENOMINATOR, CONFIRMED_FINDING against
    PLAUSIBLE_CONCERN. Keyed on the summary, the review lost one of them.

    Which way this errs is the whole design. Keying too finely reports one concern twice,
    which a reader can see and the synthesis pass is explicitly asked to fold. Keying too
    coarsely DELETES a real concern from the review and leaves a merged id behind as the
    only trace. Those are not symmetric, so the key is the finest closed vocabulary
    available.
    """
    return (f.scientific_class, f.discrepancy_type, f.baseline_class,
            f.candidate_class, f.prior_art_basis)


def deduplicate(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Fold concerns that resolved to the identical address set. (kept, n_merged)

    OVER ADDRESSES AND CLOSED VOCABULARY, NEVER OVER TEXT. Two concerns are the same
    concern when the same lens raised them, they classify themselves identically, and they
    were established from exactly the same locations in the paper. They are not the same
    concern because their prose reads alike: text similarity would merge two real concerns
    about one table that a reader happened to phrase the same way, and would MISS the case
    this exists for — one concern raised in two parts, whose statements two independent
    readings wrote and therefore wrote differently.

    Run AFTER verification, so every address in the key is one that resolved against the
    paper. Deduplicating earlier would be deduplicating quotations that may not exist.

    The survivor keeps its own identity and records the ids folded into it, so nothing is
    silently lost: a reader tracing a merged id finds where it went.
    """
    kept: list[Finding] = []
    first_by_key: dict[tuple, Finding] = {}
    merged = 0
    for f in findings:
        key = (f.lens, _concern_identity(f), _address_identity(f))
        first = first_by_key.get(key)
        if first is None:
            first_by_key[key] = f
            kept.append(f)
            continue
        merged += 1
        if f.finding_id and f.finding_id not in first.merged_from:
            first.merged_from.append(f.finding_id)
    return kept, merged


def _coerce(lens: str, data, corpus: str | Sequence[tuple[int, str]],
            by_idx: dict, max_page: int = 0, *,
            by_figure: dict[int, Figure] | None = None,
            by_equation: dict[int, Equation] | None = None) -> tuple[LensReport, int, bool]:
    """(report, n_dropped, valid). `valid` is whether the FILE ITSELF was the shape a
    lens report has to be — a JSON object carrying a `findings` list — independent of
    whether any individual finding inside it survived verification (C9). `{}`, `null`,
    a bare list, or a `findings` value that is not a list are each structurally invalid
    the same way `audit_driver.parse_lens_json` already refuses them on the auto-audit
    path; this is that same validation applied to the persisted/manual path, which had
    none — `{}` used to coerce to zero findings with no distinction from a lens that
    genuinely ran clean.
    """
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        note = ("(lens file was not a JSON object)" if not isinstance(data, dict)
                else "(lens file has no 'findings' list)")
        return LensReport(lens=lens, notes=note), 0, False
    verify = functools.partial(verify_evidence, by_figure=by_figure, by_equation=by_equation)
    # THE REPORT'S schema version, not each finding's. `prompts/audit._RETURN` puts it at
    # the top level beside `findings`, so it has to be read here and threaded down — see
    # `grading.pass_b_state`, which read it off the finding dict and therefore returned
    # `legacy` (uncapped) for every finding of every real lens file.
    schema_version = data.get("schema_version")
    findings, dropped = [], 0
    for i, f in enumerate(data.get("findings") or []):
        if not isinstance(f, dict):
            dropped += 1
            continue
        statement = str(f.get("statement") or "").strip()
        quote = str(f.get("evidence_quote") or "").strip()
        ref = str(f.get("evidence_ref") or "").strip()
        evidence_class, observation = verify(quote, ref, corpus, by_idx, max_page)
        # EVERY SIDE, OR NONE. A concern established from two locations is checkable only
        # if both are; keeping it with one half verified would publish "the conclusion
        # concedes X" on the model's word, which is the assertion the whole verification
        # gate exists to refuse. Dropped whole and counted, exactly like a finding whose
        # single quotation is not in the paper.
        extra, extra_holds = [], True
        for side in (f.get("additional_evidence") or []):
            if not isinstance(side, dict):
                extra_holds = False
                break
            side_quote = str(side.get("evidence_quote") or "").strip()
            side_ref = str(side.get("evidence_ref") or "").strip()
            side_class, side_obs = verify(side_quote, side_ref, corpus, by_idx, max_page)
            if side_class == "unverified":
                extra_holds = False
                break
            extra.append(EvidencePointer(
                role=_oneline(str(side.get("role") or ""), 80),
                evidence_quote=side_quote, evidence_ref=side_ref,
                evidence_class=side_class, verified_observation=side_obs))
        if not statement or evidence_class == "unverified" or not extra_holds:
            dropped += 1
            continue
        if extra:
            observation = " ".join(
                [observation] + [f"Further location cited ({e.role or 'unlabelled'}), "
                                 f"{e.evidence_ref}: {e.verified_observation}" for e in extra])
            # The class the CAPS are computed from, not the class of the primary citation.
            evidence_class = weakest_evidence_class(
                [evidence_class] + [e.evidence_class for e in extra])
        sev = str(f.get("severity") or "").upper()
        sev = sev if sev in _SEVERITIES else "MINOR"
        calc = f.get("independent_calculation")
        calc = calc if isinstance(calc, dict) else {}
        verification_state = grading.pass_b_state(f, sev, schema_version)
        calc_class = grading.recheck_calculation(calc, verify, corpus, by_idx, max_page)
        candidate_class = _enum(f.get("candidate_class"), CANDIDATE_CLASSES)
        confidence = _enum(f.get("confidence"), CONFIDENCES)
        baseline_class = _enum(f.get("baseline_class"), BASELINE_CLASSES)
        prior_art_basis = _enum(f.get("prior_art_basis"), PRIOR_ART_BASES)
        evidence_ceiling, evidence_sources = grading.evidence_support(
            evidence_class, calc_class=calc_class)
        # Baseline derivation from the lens's OWN submission alone — no grader has run
        # yet (that happens in a later pipeline phase; see `stages/grade.py:attach`,
        # which re-derives these same four fields once a grade exists, strictly
        # DOWNWARD from whatever is set here). `graded=False` is what makes turning the
        # grading gate off, or never running it, leave the verdict exactly where it was
        # before this subsystem existed: `stages.report.counted()` falls back to
        # `severity` whenever `counted_severity` is empty, and it is empty here only
        # when neither this baseline nor a later grade capped it.
        finding_class, counted_severity, binding_cap, derivation = grading.derive(
            lens_severity=sev, verification_state=verification_state,
            calc_class=calc_class, graded=False, evidence_class=evidence_class,
            lens_confidence=confidence, candidate_class=candidate_class,
            baseline_class=baseline_class, prior_art_basis=prior_art_basis)
        if counted_severity == sev:
            # Nothing capped it — leave `counted_severity` empty rather than a value
            # identical to `severity`, so a reader can tell "verified equal" apart from
            # "never assessed" by checking whether it is set at all.
            finding_class, counted_severity, binding_cap, derivation = "UNGRADED", "", "", ""
        findings.append(Finding(
            finding_id=_oneline(str(f.get("finding_id") or f"{lens}-{i + 1:02d}"), 60),
            lens=lens, severity=sev,
            title=_oneline(str(f.get("title") or statement), 90), statement=statement,
            target=str(f.get("target") or ""), evidence_quote=quote, evidence_ref=ref,
            counter_explanations=[str(c) for c in (f.get("counter_explanations") or [])
                                 if isinstance(c, (str, int, float))],
            verifiable_by_experiment=bool(f.get("verifiable_by_experiment")),
            additional_evidence=extra,
            # The lens's own layers, kept apart from each other and from the evidence.
            # Each falls back to `statement` so a file written before the split still
            # produces a complete finding.
            claim=str(f.get("claim") or f.get("target") or "").strip(),
            reasoning=str(f.get("reasoning") or statement).strip(),
            conclusion=str(f.get("conclusion") or statement).strip(),
            severity_rationale=str(f.get("severity_rationale") or "").strip(),
            # The pass-B fields: lens-authored, non-degeneracy checked but not rewritten
            # — a lens's actual words are kept even when `verification_state` below
            # says they were not enough.
            candidate_class=candidate_class,
            confidence=confidence,
            baseline_class=baseline_class,
            prior_art_basis=prior_art_basis,
            discrepancy_type=_enum(f.get("discrepancy_type"), DISCREPANCY_TYPES),
            what_the_paper_says=_prose(f.get("what_the_paper_says")),
            alternative_interpretation=_prose(f.get("alternative_interpretation")),
            why_alternative_fails=_prose(f.get("why_alternative_fails")),
            steelman=_prose(f.get("steelman")),
            effect_on_claim=_prose(f.get("effect_on_claim")),
            recommended_resolution=_prose(f.get("recommended_resolution")),
            independent_calculation=calc,
            # NOT read from `f`. A lens supplying any of these is overwritten here,
            # because the whole point of a harness-written field is that the harness —
            # not the model — is its author (invariant #2, extended to every axis added
            # since: a lens cannot certify its own reasoning, its own arithmetic, or its
            # own non-degeneracy any more than it could certify its own evidence).
            evidence_class=evidence_class,
            verified_observation=observation,
            # WHAT KIND of issue this is, derived from the closed-vocabulary fields the
            # lens has already chosen above. Deliberately NOT passed to
            # `grading.derive`: kind must not decide severity, and keeping the call
            # signature free of it is what makes "a CONFOUND is always MAJOR"
            # inexpressible rather than merely absent.
            scientific_class=taxonomy.classify(
                lens=lens, discrepancy_type=_enum(f.get("discrepancy_type"), DISCREPANCY_TYPES),
                baseline_class=baseline_class, candidate_class=candidate_class),
            verification_state=verification_state,
            calc_class=calc_class,
            evidence_origin=_origin_from_ref(ref),
            origin_consistency=("consistent"
                                if _enum(f.get("evidence_origin"), EVIDENCE_ORIGINS)
                                in ("", _origin_from_ref(ref)) else "corrected"),
            # DERIVED, so it cannot say two locations were checked when one was.
            cross_section=bool(extra),
            # Written by `compose_lens` from which artifact the finding came out of, and
            # stripped from anything a reading itself submitted (see
            # `audit_driver.HARNESS_OWNED_FINDING_KEYS`). '' on a paper read in one pass.
            source_part=_oneline(str(f.get("source_part") or ""), 40),
            finding_class=finding_class, counted_severity=counted_severity,
            binding_cap=binding_cap, derivation=derivation,
            evidence_ceiling=evidence_ceiling, evidence_sources=list(evidence_sources),
        ))
    findings, merged = deduplicate(findings)
    return LensReport(
        lens=lens, findings=findings, merged_duplicates=merged,
        unasked_question=str(data.get("unasked_question") or "").strip(),
        notes=str(data.get("notes") or ""),
    ), dropped, True
