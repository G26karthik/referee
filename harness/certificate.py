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

import json
from pathlib import Path

from . import delegation, sealing, state
from .config import Config
from .prompts import certificate as CP
from .reimplement_driver import _implementation_locator_matches
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
    return " ".join((text or "").split())


def _full_paper_text(doc: PaperDoc) -> str:
    return "\n\n".join(" ".join((s.text or "").split()) for s in doc.sections)


def section_excerpt(doc: PaperDoc, ref, *, max_chars: int = 6000) -> str:
    """The section stating the claim, plus the section immediately before it (often where
    a theorem's setup/hypotheses live) — BOUNDED, never the whole paper (invariant: minimize
    context per role, CLAUDE.md). `ref` is the target's own `ClaimRef`; `None` falls back to
    the whole document truncated, so a caller missing a resolved ref still gets something."""
    idx = getattr(ref, "section_idx", -1) if ref is not None else -1
    if idx is None or idx < 0:
        return _full_paper_text(doc)[:max_chars]
    wanted = {i for i in (idx - 1, idx) if i >= 0}
    parts = []
    for s in doc.sections:
        if s.section_idx in wanted:
            parts.append(f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]\n"
                        + " ".join((s.text or "").split()))
    return "\n\n".join(parts)[:max_chars]


def build_brief(doc: PaperDoc, *, claim: str = "", ref=None) -> str:
    """The GENERATOR's prompt — a bounded section excerpt, never the full paper."""
    return CP.build(doc.title, claim, section_excerpt(doc, ref))


def verification_brief(doc: PaperDoc, *, claim: str = "", ref=None, script: str = "",
                       bindings: list[dict] | None = None,
                       paper_quotes: dict | None = None) -> str:
    """The SEPARATE verifier's prompt. Independence is enforced later, at `accept()` time
    (`generated_by != verified_by`) — this function only builds the text; it does not know
    or care who reads it."""
    return CP.verification_build(doc.title, claim, section_excerpt(doc, ref), script,
                                 list(bindings or []), dict(paper_quotes or {}))


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


def accept(cfg: Config, pid: str, target_id: str, raw: str, verdict_raw: str | None = None, *,
          generated_by: str = "", reviewer: str = "") -> dict:
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
    approved, verifier_notes = ((False, "no verifier output supplied") if verdict_raw is None
                                else parse_verification(verdict_raw))
    doc_path = state.project_dir(cfg, pid) / "paper" / "doc.json"
    paper_text = (_full_paper_text(PaperDoc(**state.read_json(doc_path)))
                 if doc_path.exists() else "")
    conf = conformance(paper_text, script, bindings, paper_quotes,
                       generated_by=generated_by,
                       verified_by=(reviewer if approved else ""))
    prov = delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=reviewer)
    out, _sidecar = _paths(cfg, pid, target_id)
    record = sealing.seal(
        out, {"script": script, "conformance": conf.model_dump(),
             "instance_count": instance_count, "notes": notes},
        mode="SESSION_SUBAGENT", reviewer=prov["reviewer"], tool_policy=prov["tool_policy"],
        extra={"paper_id": pid, "target_id": target_id, "established": conf.established,
              "generated_by": generated_by, "independent_verification": approved,
              "verifier_notes": verifier_notes,
              "isolation_claim": prov["isolation_claim"],
              "tool_policy_provable": prov["tool_policy_provable"]})
    return record


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
