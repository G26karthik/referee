"""EXACT_CERTIFICATE — an exact-arithmetic check of a theorem/bound claim, for the case
static reading cannot settle: a paper prints a theorem with a proof, and the only way to
show the proof's CONCLUSION is wrong is to exhibit a concrete instance that satisfies every
stated hypothesis and violates the claimed inequality. `harness.discover` routes a
SPECIFICATION/PRINTED_QUANTITY finding here (as `MATHEMATICAL_BOUND`) when its own claim
text states a theorem/lemma/bound/rate together with an inequality or asymptotic operator
and cites no printed table cell — see `discover.is_mathematical_bound`.

**Two subagents, not a subprocess.** This module never spawns a `claude` CLI process of its
own: the controlling session dispatches its own isolated subagents — one to GENERATE a
certificate (`build_brief`), a SEPARATE one to VERIFY it (`verification_brief`) — and this
module only PARSES what comes back (`parse`), checks it (`conformance`), and SEALS it
(`accept`, mode `SESSION_SUBAGENT`, mirroring `harness.artifact_review_driver.accept`).
Nothing here ever executes the certificate script; `harness.execute`'s `cert_exec` branch of
`authorize()` is the only place that may, and only once `conformance().established` holds.

**Independence, exactly as `reimplement_driver.conformance` requires it.** A certificate
that certifies its own correctness is worth nothing — `accept()` refuses to mark one
`established` unless `generated_by != verified_by` (`ReimplementationConformance
.independently_verified`), the SAME pydantic type `reimplement_driver` uses, reused rather
than duplicated.

**The provenance ceiling, restated for a proof instead of an experiment.** A counterexample
this module sealed may CONVICT a paper's theorem (invariant 3) but a CLEAN certificate —
every tested instance satisfied the bound — may never ACQUIT one: checking finitely many
instances is not a proof, so `taxonomy`'s `NO_COUNTEREXAMPLE_FOUND`/`CERTIFICATE_NO_VIOLATION`
pair is deliberately excluded from `EVIDENCE_ABOUT_THE_PAPER`.

`python -m harness.certificate` runs the self-check.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import delegation, sealing, state
from .config import Config
from .prompts import certificate as CP
from .prompts import reimplement as RP
from .reimplement_driver import (_implementation_locator_matches, parse_verdict, read_revision,
                                 record_attempt)
from .schema import PaperDoc, ReimplementationBinding, ReimplementationConformance

# The three elements EVERY certificate must bind — an implementation locator in the script
# for all three, AND a verbatim paper quote for the two that are claims ABOUT the paper
# ("instance" is the generator's own concrete construction; it has no paper quote to
# verify, see `_PAPER_QUOTE_KINDS` below).
REQUIRED_KINDS = ("hypotheses", "claimed_bound", "instance")
_PAPER_QUOTE_KINDS = ("hypotheses", "claimed_bound")

# Every token a certificate seal may carry. SESSION_SUBAGENT is the only channel this
# module's own `accept()` produces; an operator may still hand-seal one manually (mirrors
# `reimplement_driver.WRITERS`'s inclusion of every real delegation mode).
WRITERS = tuple(delegation.WRITTEN_BY.values())

_MAX_INSTANCES = 25


class CertificateError(RuntimeError):
    pass


def _normalize_ws(text: str) -> str:
    """Whitespace-normalised, with control characters dropped: a PDF parse emits them for
    glyphs such as large brackets, and no quotation can reproduce them. Every printable
    character must still match exactly."""
    return " ".join("".join(c for c in (text or "")
                            if c.isprintable() or c.isspace()).split())


def _full_paper_text(doc: PaperDoc) -> str:
    return "\n\n".join(" ".join((s.text or "").split()) for s in doc.sections)


def section_excerpt(doc: PaperDoc, ref, *, before: int = 10000, after: int = 4000) -> str:
    """The section stating the claim, plus the section immediately before it (often where
    a theorem's setup/hypotheses live) — BOUNDED, never the whole paper. The window is
    anchored on the claim's own quote, mostly BEFORE it (hypotheses precede a bound), so a
    theorem deep in a long section is never cut off. `ref` is the target's `ClaimRef`."""
    budget = before + after
    idx = getattr(ref, "section_idx", -1) if ref is not None else -1
    if idx is None or idx < 0:
        text = _full_paper_text(doc)
    else:
        wanted = {i for i in (idx - 1, idx) if i >= 0}
        text = "\n\n".join(
            f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]\n"
            + " ".join((s.text or "").split())
            for s in doc.sections if s.section_idx in wanted)
    if len(text) <= budget:
        return text
    quote = _normalize_ws(getattr(ref, "quote", "") or "")
    at = text.find(quote[:120]) if quote else -1
    if at < 0:
        return text[:budget]
    lo = max(0, at - before)
    hi = min(len(text), at + len(quote) + after)
    return ("[…] " if lo else "") + text[lo:hi] + (" […]" if hi < len(text) else "")


_LABEL = re.compile(r"^\s*(Theorem|Proposition|Lemma|Corollary|Result)\s+([A-Z]?\d+(?:\.\d+)*)")
_MAX_PROOF_CHARS = 12000
_POINTER = re.compile(r"\s*(?:\([^)]*\)\s*)?(?:can\s+be\s+found|is\s+(?:given|deferred|provided|"
                      r"postponed|in|found|sketched)|are\s+(?:given|deferred)|appears|see\b|"
                      r"in\s+(?:Appendix|Section)|follows\s+(?:from|in))", re.I)
_SCOPES = ("conclusion", "proof_step")


def statement_label(claim: str = "") -> str:
    m = _LABEL.match(claim or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


def proof_location(doc: PaperDoc, label: str) -> tuple[int, int]:
    """(section_idx, char offset) of "Proof of <label>" in the parsed paper, or (-1, -1)."""
    if not label:
        return -1, -1
    pat = re.compile(rf"\bProof\s+of\s+(?:the\s+)?{re.escape(label)}(?![\d.]*\d)", re.I)
    last = (-1, -1)
    for sec in doc.sections:
        for m in pat.finditer(sec.text or ""):
            # "Proof of Theorem 3.1 can be found in Appendix B" points AT the proof. The
            # LAST real one wins: a main-text sketch precedes the full appendix proof.
            if not _POINTER.match((sec.text or "")[m.end():m.end() + 60]):
                last = (sec.section_idx, m.start())
    return last if last[0] >= 0 else _inline_proof(doc, label)


# ponytail: an inline proof must begin within this many characters after the statement's
# header (the statement itself is at most ~1200); a proof printed further away without a
# "Proof of <label>" heading is not found, and the target reports no proof to audit.
_INLINE_PROOF_WINDOW = 3200


def _inline_proof(doc: PaperDoc, label: str) -> tuple[int, int]:
    """A proof printed directly after its statement ("Theorem 2. ... Proof. ...") rather
    than under its own "Proof of Theorem 2" heading."""
    from .discover import formal_statements
    st = next((s for s in formal_statements(doc) if s["label"] == label), None)
    sec = next((x for x in doc.sections if st and x.section_idx == st["section_idx"]), None)
    if sec is None:
        return -1, -1
    text = sec.text or ""
    head = re.search(rf"\b{re.escape(label)}\s*(?:\([^()]{{0,120}}\))?\s*\.", text)
    if head is None:
        return -1, -1
    p = re.compile(r"\bProof\b\s*[.:]").search(text, head.end(), head.end() + _INLINE_PROOF_WINDOW)
    return (sec.section_idx, p.start()) if p else (-1, -1)


def statement_text(doc: PaperDoc, label: str) -> str:
    """The statement's own parsed words (whitespace-normalised, up to ~1200 chars) — the
    text every `paper_quotes` entry about it must be copied from."""
    from .discover import formal_statements
    return next((s["text"] for s in formal_statements(doc) if s["label"] == label), "")


# DEPENDENCY CONTEXT — a statement or proof that cites "Definition 2.1" or "Eq. (4)" cannot
# be checked from its own section alone. ponytail: depth 1 (what the statement/proof cites,
# not what those cite), at most 6 snippets of 1500 chars each.
_CITED = re.compile(r"\b(Definition|Assumption|Lemma|Proposition|Theorem|Corollary|Algorithm)"
                    r"\s+([A-Z]?\d+(?:\.\d+)*)")
_CITED_EQ = re.compile(r"\bEq(?:uation)?s?\.?\s*\((\d+)\)")
_MAX_CONTEXT, _CONTEXT_CHARS = 6, 1500


def _defining_occurrence(doc: PaperDoc, label: str) -> str:
    kind, num = label.split(" ", 1)
    head = re.compile(rf"\b{kind}\s+{re.escape(num)}(?![\d.]*\d)\s*(?:\([^()]{{0,120}}\))?"
                      rf"\s*[.:]?\s+(?=[A-Z(\u2200-\u22ff])")
    for sec in doc.sections:
        if (sec.title or "").strip().lower().startswith(("references", "bibliography")):
            continue
        text = sec.text or ""
        for m in head.finditer(text):
            before = text[:m.start()].rstrip()
            if before and before[-1] not in ".:;)]\n":
                continue
            return " ".join(text[m.start():m.start() + _CONTEXT_CHARS].split())
    return ""


def referenced_context(doc: PaperDoc, text: str, own_label: str = "") -> str:
    labels: list[str] = []
    for m in _CITED.finditer(text or ""):
        lab = f"{m.group(1)} {m.group(2)}"
        if lab != own_label and lab not in labels:
            labels.append(lab)
    eqs = list(dict.fromkeys(m.group(1) for m in _CITED_EQ.finditer(text or "")))
    out: list[str] = []
    for lab in labels:
        if len(out) >= _MAX_CONTEXT:
            break
        snippet = _defining_occurrence(doc, lab)
        if snippet:
            out.append(f"[{lab}] {snippet}")
    for n in eqs:
        if len(out) >= _MAX_CONTEXT:
            break
        eq = next((e for e in doc.equations if (e.number or "").strip("() ") == n), None)
        if eq is not None and (eq.text or "").strip():
            out.append(f"[Eq. ({n})] {' '.join(eq.text.split())[:_CONTEXT_CHARS]}")
    return "\n\n".join(out)


def proof_excerpt(doc: PaperDoc, label: str) -> str:
    """The printed proof of `label` wherever it lives (often an appendix the statement's
    own section never reaches), BOUNDED, up to the next "Proof of" — or ''."""
    idx, at = proof_location(doc, label)
    if idx < 0:
        return ""
    parts = [(x.text or "")[at:] if x.section_idx == idx else (x.text or "")
             for x in doc.sections if x.section_idx >= idx]
    text = " ".join(" ".join(parts).split())
    nxt = re.search(r"\bProof\s+of\s+(Theorem|Proposition|Lemma|Corollary|Result)", text[20:])
    end = 20 + nxt.start() if nxt else len(text)
    cut = min(end, _MAX_PROOF_CHARS)
    return text[:cut] + (" […]" if end > cut else "")


def page_images(cfg: Config, pid: str, doc: PaperDoc, ref=None, claim: str = "") -> list[str]:
    """Rendered PNGs of the statement's page and its proof's pages — the parsed text can
    garble mathematics (roots, fractions, indices), and a delegate that cannot read the
    exact inequality cannot check it. Best-effort; [] when the PDF is not on disk."""
    from .extraction_audit import render_pages

    src = Path(doc.source_path) if doc.source_path else None
    if src is None or not src.is_file():
        return []
    pages: set[int] = set()
    if ref is not None and getattr(ref, "page", None):
        pages.add(int(ref.page))
    idx, _at = proof_location(doc, statement_label(claim))
    sec = next((x for x in doc.sections if x.section_idx == idx), None)
    if sec is not None and sec.page_start:
        # ponytail: at most 4 proof pages; a longer proof is cut, and the brief says the
        # parsed text above continues it.
        pages.update(range(sec.page_start, min(sec.page_end or sec.page_start,
                                               sec.page_start + 3) + 1))
    out = render_pages(src, pages, state.project_dir(cfg, pid) / "paper" / "pages")
    return [out[k] for k in sorted(out)]


def _given(doc: PaperDoc, claim: str, ref) -> tuple[str, str, str]:
    """(proof, verbatim statement text, cited context) for one target."""
    label = statement_label(claim)
    proof = proof_excerpt(doc, label)
    verbatim = statement_text(doc, label) or (getattr(ref, "quote", "") or "")
    return proof, verbatim, referenced_context(doc, f"{verbatim} {proof}", label)


def build_brief(doc: PaperDoc, *, claim: str = "", ref=None,
                images: list[str] | None = None, step: str = "",
                revision: dict | None = None) -> str:
    """The GENERATOR's prompt — a bounded section excerpt plus the statement's own printed
    proof, the definitions it cites and its verbatim text; `step` binds one proof step;
    `revision` carries a rejected attempt and its verifier's required changes."""
    proof, verbatim, context = _given(doc, claim, ref)
    return CP.build(doc.title, claim, section_excerpt(doc, ref), proof=proof,
                    images=list(images or []), verbatim=verbatim, context=context, step=step,
                    revision=RP.revision_block(revision or {})).replace("\x00", "")


def verification_brief(doc: PaperDoc, *, claim: str = "", ref=None, script: str = "",
                       bindings: list[dict] | None = None,
                       paper_quotes: dict | None = None, scope: str = "",
                       images: list[str] | None = None, step: str = "") -> str:
    """The SEPARATE verifier's prompt. Independence is enforced later, at `accept()` time
    (`generated_by != verified_by`) — this function only builds the text; it does not know
    or care who reads it."""
    proof, verbatim, context = _given(doc, claim, ref)
    return CP.verification_build(doc.title, claim, section_excerpt(doc, ref), script,
                                 list(bindings or []), dict(paper_quotes or {}),
                                 proof=proof, scope=scope, images=list(images or []),
                                 verbatim=verbatim, context=context, step=step).replace("\x00", "")


def parse_scope(raw: str) -> str:
    """The generator's declared `checked_statement`, one of `_SCOPES`, else "conclusion"."""
    text = (raw or "").strip()
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except (ValueError, TypeError):
        return "conclusion"
    v = str((data or {}).get("checked_statement") or "").strip().lower()
    return v if v in _SCOPES else "conclusion"


def parse(raw: str) -> tuple[str, list[dict], dict, str, int]:
    """(script, bindings, paper_quotes, notes, instance_count) — the generator's raw JSON,
    unwrapped and validated. Unknown keys are dropped, exactly as `reimplement_driver
    .parse_reimplementation_report` drops them: a field this schema does not name is never
    carried into anything sealed."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if not text or start < 0 or end <= start:
        raise CertificateError(f"no JSON object in the output (first 120 chars: {text[:120]!r})")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise CertificateError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("script") or "").strip():
        raise CertificateError("output JSON has no non-empty 'script'")
    bindings = data.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
    clean_bindings = [b for b in bindings if isinstance(b, dict) and b.get("kind")]
    pq = data.get("paper_quotes")
    paper_quotes = {str(k): str(v) for k, v in pq.items()} if isinstance(pq, dict) else {}
    n = data.get("instance_count")
    instance_count = n if (isinstance(n, int) and not isinstance(n, bool)
                           and 1 <= n <= _MAX_INSTANCES) else 1
    return str(data["script"]), clean_bindings, paper_quotes, str(data.get("notes") or ""), \
        instance_count


def parse_verification(raw: str) -> tuple[bool, str]:
    """(approved, notes) from the verifier's raw JSON — approved only when every REQUIRED
    kind is named in `approved_kinds`, mirroring `reimplement_driver._parse_verification`."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return False, "verifier returned no JSON object"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return False, f"verifier JSON invalid: {e}"
    kinds = {str(x) for x in (data.get("approved_kinds") or [])}
    approved = bool(data.get("approved")) and kinds == set(REQUIRED_KINDS)
    return approved, str(data.get("notes") or "")


def conformance(paper_text: str, script: str, bindings: list[dict], paper_quotes: dict, *,
                generated_by: str = "", verified_by: str = "") -> ReimplementationConformance:
    """Pure. Pairs each REQUIRED kind's IMPLEMENTATION locator (from the generator's own
    `bindings`) with an independent check that its `impl_quote` occurs verbatim in `script`
    (`reimplement_driver._implementation_locator_matches`, reused rather than
    reimplemented) AND — invariant 1 — that "hypotheses"/"claimed_bound"'s own
    `paper_quotes` entry occurs verbatim (whitespace-normalised) in the paper text. Neither
    check trusts the generator's own say-so; both are the harness's.
    """
    norm_paper = _normalize_ws(paper_text)
    by_kind = {b.get("kind"): b for b in (bindings or [])}
    out: list[ReimplementationBinding] = []
    unbound: list[str] = []
    for kind in REQUIRED_KINDS:
        d = by_kind.get(kind) or {}
        impl_ref = str(d.get("impl_ref") or "").strip()
        impl_quote = str(d.get("impl_quote") or "").strip()
        impl_verified = _implementation_locator_matches(script, impl_ref, impl_quote)
        if kind in _PAPER_QUOTE_KINDS:
            paper_quote = str((paper_quotes or {}).get(kind) or "").strip()
            paper_verified = bool(paper_quote) and _normalize_ws(paper_quote) in norm_paper
        else:
            # "instance" is the generator's own concrete construction, not a paper claim —
            # there is nothing here for invariant 1 to re-find in the paper text.
            paper_quote = ""
            paper_verified = True
        verified = impl_verified and paper_verified
        bound = bool(impl_ref) and verified
        if not bound:
            unbound.append(kind)
        out.append(ReimplementationBinding(
            kind=kind, paper_ref="", paper_quote=paper_quote, impl_ref=impl_ref,
            impl_quote=impl_quote, verified=verified, bound=bound))
    generator = (generated_by or "").strip()
    verifier = (verified_by or "").strip()
    independent = bool(generator and verifier and generator != verifier)
    established = not unbound and independent
    if established:
        reason = ("every required element is bound to a verified implementation locator, "
                  "'hypotheses' and 'claimed_bound' are re-found verbatim in the paper, "
                  "and a separately attributed verifier approved the proposal; this "
                  "certificate may reconcile against the claimed bound")
    elif not unbound and not independent:
        reason = ("every element binds, but a generated certificate cannot certify its "
                  "own conformance. A separately attributed verifier must approve the "
                  "hypotheses, claimed bound and instance")
    else:
        reason = ("not bound: " + ", ".join(unbound) + " — the returned script or the "
                  "quoted paper text does not verifiably realize " +
                  ("this element" if len(unbound) == 1 else "these elements") +
                  ", so no verdict may be drawn from running it")
    return ReimplementationConformance(
        established=established, bindings=out, unbound=unbound, reason=reason,
        generated_by=generator, verified_by=verifier, independently_verified=independent)


def _paths(cfg: Config, pid: str, target_id: str) -> tuple[Path, Path]:
    out = (state.project_dir(cfg, pid) / "runs" / pid / "certificates" /
           f"{target_id or 'default'}.json")
    return out, out.with_suffix(".driver.json")


def revision_path(cfg: Config, pid: str, target_id: str) -> Path:
    return _paths(cfg, pid, target_id)[0].with_suffix(".revision.json")


def accept(cfg: Config, pid: str, target_id: str, raw: str, verdict_raw: str | None = None, *,
          generated_by: str = "", reviewer: str = "", brief_sha256: str = "",
          prebound: str = "", base_sha: str = "", max_revisions: int = 2) -> dict:
    """Parse the generator's `raw` output (and, when given, the verifier's `verdict_raw`),
    compute conformance against the ingested paper text, and seal to
    `runs/<pid>/certificates/<target_id>.json` — mode `SESSION_SUBAGENT`, `written_by`
    `"session_subagent"` (`delegation.WRITTEN_BY["SESSION_SUBAGENT"]`), mirroring
    `harness.artifact_review_driver.accept` exactly. `verdict_raw=None` seals honestly as
    NOT established (no verifier ran yet) rather than blocking — the same shape
    `reimplement_driver.accept_reimplementation` uses when a caller has only the generator's
    half so far.
    """
    script, bindings, paper_quotes, notes, instance_count = parse(raw)
    if prebound:
        # The harness chose this proof step and re-found it in the proof: what is checked is
        # the harness's quote, never a generator's re-typing of it.
        paper_quotes = dict(paper_quotes, claimed_bound=prebound)
    approved, verifier_notes = ((False, "no verifier output supplied") if verdict_raw is None
                                else parse_verification(verdict_raw))
    verdict, required = parse_verdict(verdict_raw or "", approved)
    doc_path = state.project_dir(cfg, pid) / "paper" / "doc.json"
    paper_text = (_full_paper_text(PaperDoc(**state.read_json(doc_path)))
                 if doc_path.exists() else "")
    conf = conformance(paper_text, script, bindings, paper_quotes,
                       generated_by=generated_by,
                       verified_by=(reviewer if approved else ""))
    conf.scope = "proof_step" if prebound else parse_scope(raw)
    attempt = (record_attempt(revision_path(cfg, pid, target_id), base_sha=base_sha or brief_sha256,
                              verdict=verdict, script=script, required=required,
                              notes=verifier_notes, max_revisions=max_revisions)
               if verdict_raw is not None else 1)
    prov = delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=reviewer)
    out, _sidecar = _paths(cfg, pid, target_id)
    record = sealing.seal(
        out, {"script": script, "conformance": conf.model_dump(),
             "instance_count": instance_count, "notes": notes},
        mode="SESSION_SUBAGENT", reviewer=prov["reviewer"], tool_policy=prov["tool_policy"],
        extra={"paper_id": pid, "target_id": target_id, "established": conf.established,
              "generated_by": generated_by, "independent_verification": approved,
              "verifier_notes": verifier_notes, "brief_sha256": brief_sha256,
              "verdict": verdict, "required_changes": required, "attempt": attempt,
              "base_sha256": base_sha or brief_sha256,
              "isolation_claim": prov["isolation_claim"],
              "tool_policy_provable": prov["tool_policy_provable"]})
    return record


def brief_sha(brief: str) -> str:
    return hashlib.sha256((brief or "").encode("utf-8")).hexdigest()


def sealed_record(cfg: Config, pid: str, target_id: str) -> dict:
    """The sealed sidecar for this target ({} when none): its `brief_sha256` says which
    generator brief the certificate answered, its `verifier_notes` why it was rejected."""
    _out, sidecar = _paths(cfg, pid, target_id)
    try:
        return state.read_json(sidecar) if sidecar.is_file() else {}
    except (OSError, ValueError):
        return {}


def load_accepted(cfg: Config, pid: str, target_id: str
                  ) -> tuple[str, ReimplementationConformance, int] | None:
    """A sealed (script, conformance, instance_count) for `pid`/`target_id`, or None.
    Verifies the seal before trusting it, exactly as `reimplement_driver.load_accepted`
    does, plus the SAME independence cross-check on top of `conformance.established`."""
    out, sidecar = _paths(cfg, pid, target_id)
    ok, _why = sealing.verify_seal(out, accepted_writers=WRITERS)
    if not ok:
        return None
    try:
        rec = state.read_json(sidecar)
        data = state.read_json(out)
        conf = ReimplementationConformance(**data.get("conformance") or {})
        if conf.established and not (rec.get("independent_verification")
                                     and conf.independently_verified
                                     and conf.generated_by and conf.verified_by
                                     and conf.generated_by != conf.verified_by):
            return None
        n = data.get("instance_count")
        instance_count = n if (isinstance(n, int) and 1 <= n <= _MAX_INSTANCES) else 1
        return str(data.get("script") or ""), conf, instance_count
    except Exception:
        return None


# === PROOF MAPS — every checkable step of a printed proof, not one a model happened to pick
# A `proof_map` subagent lists the explicit steps; the harness keeps only those it re-finds
# verbatim in the proof, and `harness.discover` turns each into its own certificate target.

def _map_paths(cfg: Config, pid: str, target_id: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "runs" / pid / "proof_maps" / f"{target_id}.json"
    return out, out.with_suffix(".driver.json")


def proof_map_brief(doc: PaperDoc, *, claim: str = "", ref=None,
                    images: list[str] | None = None) -> str:
    """The proof-mapping prompt, or "" when this statement has no located proof."""
    proof, verbatim, context = _given(doc, claim, ref)
    if not proof:
        return ""
    return CP.proof_map_build(doc.title, verbatim or claim, proof, context=context,
                              images=list(images or []))


def parse_proof_map(raw: str, proof_text: str, max_steps: int) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). A step is kept only if its quote is re-found verbatim in the proof
    (invariant 1), it has explicit constants, and it fits the ceiling; each drop says why."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise CertificateError("proof map: no JSON object in the output")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise CertificateError(f"proof map: output is not valid JSON: {e}") from e
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        raise CertificateError("proof map: output JSON has no 'steps' list")
    norm = _normalize_ws(proof_text)
    kept, dropped, seen = [], [], set()
    for s in steps:
        if not isinstance(s, dict):
            continue
        q = _normalize_ws(str(s.get("quote") or ""))[:400]
        why = ("not found verbatim in the parsed proof" if not q or q not in norm else
               "no explicit constants, so no finite instance can violate it"
               if not s.get("explicit_constants") else
               "a duplicate" if q in seen else
               f"beyond the {max_steps}-step ceiling" if len(kept) >= max_steps else "")
        if why:
            dropped.append({"quote": q[:200], "why": why})
            continue
        seen.add(q)
        kept.append({"quote": q, "relation": str(s.get("relation") or "")[:40],
                     "why_doubtful": " ".join(str(s.get("why_doubtful") or "").split())[:300]})
    return kept, dropped


def accept_proof_map(cfg: Config, pid: str, target_id: str, raw: str, *, label: str,
                     brief: str, max_steps: int, reviewer: str = "") -> dict:
    doc = PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))
    kept, dropped = parse_proof_map(raw, proof_excerpt(doc, label), max_steps)
    prov = delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=reviewer)
    out, _sidecar = _map_paths(cfg, pid, target_id)
    return sealing.seal(
        out, {"statement": label, "steps": kept, "dropped": dropped},
        mode="SESSION_SUBAGENT", reviewer=prov["reviewer"], tool_policy=prov["tool_policy"],
        extra={"paper_id": pid, "target_id": target_id, "brief_sha256": brief_sha(brief),
               "kept": len(kept), "dropped": len(dropped)})


def load_proof_maps(cfg: Config, pid: str) -> dict[str, dict]:
    """{parent target_id: {"statement", "steps", "dropped", "brief_sha256"}} for every
    validly sealed proof map."""
    d = state.project_dir(cfg, pid) / "runs" / pid / "proof_maps"
    out: dict[str, dict] = {}
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if f.name.endswith(".driver.json") or not sealing.verify_seal(f, accepted_writers=WRITERS)[0]:
            continue
        try:
            data = state.read_json(f)
            data["brief_sha256"] = state.read_json(f.with_suffix(".driver.json")).get("brief_sha256", "")
            out[f.stem] = data
        except (OSError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    from .schema import Section

    doc = PaperDoc(paper_id="p", title="Frank-Wolfe Attention", sections=[
        Section(section_idx=0, title="Setup", text="Let f be convex and L-smooth."),
        Section(section_idx=1, title="Convergence",
               text="Theorem 3.1. Under the above hypotheses, the regret after T steps "
                    "satisfies regret(T) <= C / sqrt(T) for a universal constant C."),
    ])
    assert "Theorem 3.1" in section_excerpt(doc, None), "no ref => falls back to whole doc"
    assert "Theorem 3.1" in section_excerpt(
        doc, type("R", (), {"section_idx": 1})())
    assert "Let f be convex" in section_excerpt(doc, type("R", (), {"section_idx": 1})())

    brief = build_brief(doc, claim="Theorem 3.1", ref=type("R", (), {"section_idx": 1})())
    assert "Theorem 3.1" in brief and "instance_count" in brief

    good = json.dumps({
        "script": "from fractions import Fraction\n"
                  "# hypotheses: f convex and L-smooth\n"
                  "assert True  # instance: T=224\n"
                  "print('SH_METRIC arm=certificate seed=0 value=1')",
        "instance_count": 1,
        "bindings": [
            {"kind": "hypotheses", "impl_ref": "line 2",
             "impl_quote": "# hypotheses: f convex and L-smooth"},
            {"kind": "claimed_bound", "impl_ref": "line 4",
             "impl_quote": "print('SH_METRIC arm=certificate seed=0 value=1')"},
            {"kind": "instance", "impl_ref": "line 3", "impl_quote": "assert True  # instance: T=224"},
        ],
        "paper_quotes": {
            "hypotheses": "Let f be convex and L-smooth.",
            "claimed_bound": "regret(T) <= C / sqrt(T) for a universal constant C",
        },
        "notes": "",
    })
    script, bindings, paper_quotes, notes, n = parse(good)
    assert "SH_METRIC" in script and len(bindings) == 3 and n == 1

    for bad in ("", "no json", '{"bindings":[]}', '{"script":""}'):
        try:
            parse(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except CertificateError:
            pass

    paper_text = _full_paper_text(doc)
    fully_bound = conformance(paper_text, script, bindings, paper_quotes,
                              generated_by="generator", verified_by="verifier")
    assert fully_bound.established, fully_bound.unbound
    assert all(b.verified and b.bound for b in fully_bound.bindings)

    # a paper_quote that does NOT resolve verbatim in the paper text => unbound
    bad_quotes = dict(paper_quotes, hypotheses="a hypothesis this paper never states")
    unresolved = conformance(paper_text, script, bindings, bad_quotes,
                             generated_by="generator", verified_by="verifier")
    assert not unresolved.established
    assert "hypotheses" in unresolved.unbound
    hyp_binding = next(b for b in unresolved.bindings if b.kind == "hypotheses")
    assert not hyp_binding.verified and not hyp_binding.bound

    # a binding the generator CLAIMS but that is not literally in the returned script
    lying_bindings = json.loads(good)["bindings"]
    lying_bindings[1]["impl_quote"] = "this text is not anywhere in the script"
    forged = conformance(paper_text, script, lying_bindings, paper_quotes,
                         generated_by="generator", verified_by="verifier")
    assert not forged.established and "claimed_bound" in forged.unbound

    # generated == verified => never established, however well everything else binds
    self_certified = conformance(paper_text, script, bindings, paper_quotes,
                                 generated_by="same", verified_by="same")
    assert not self_certified.established and self_certified.independently_verified is False

    # --- seal / load roundtrip -----------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td))
        (state.project_dir(cfg, "p1") / "paper").mkdir(parents=True)
        state.write_json(state.project_dir(cfg, "p1") / "paper" / "doc.json", doc.model_dump())
        assert load_accepted(cfg, "p1", "t1") is None
        verdict = json.dumps({"approved": True, "approved_kinds": list(REQUIRED_KINDS),
                              "notes": "looks right"})
        rec = accept(cfg, "p1", "t1", good, verdict, generated_by="builder", reviewer="alice")
        assert rec["written_by"] == "session_subagent" and rec["established"] is True
        got = load_accepted(cfg, "p1", "t1")
        assert got is not None
        got_script, got_conf, got_n = got
        assert got_script == script and got_conf.established and got_n == 1

        # no verifier output at all => sealed honestly as NOT established
        unverified_rec = accept(cfg, "p1", "t2", good, None, generated_by="builder")
        assert unverified_rec["established"] is False
        unverified = load_accepted(cfg, "p1", "t2")
        assert unverified is not None and not unverified[1].established

        # a tampered artifact is not the artifact that was sealed
        out_path, _ = _paths(cfg, "p1", "t1")
        state.write_json(out_path, {"script": "tampered", "conformance": {}})
        assert load_accepted(cfg, "p1", "t1") is None

    print("harness.certificate self-check ok")


if __name__ == "__main__":
    _self_check()
