"""The ARTIFACT_INSPECTION route, run against a checkout that already exists.

`python -m harness.stages.artifact` runs the self-check.

**Why this is a stage and not a branch inside `discover`.** `discover` is pure with
respect to the parsed paper and runs BEFORE anything is acquired — at that point
`repo_available` means only that the paper advertises a URL, so there is no checkout to
read. `probe` is where a clone exists, which is where this runs. Nothing here consults a
model on its own: the deterministic probes in `harness/artifact_evidence.py` do the work,
and the optional authors'-code auditor is a separate gated channel whose every citation
this route relocates before it counts.

**What it may settle, and what it may not.** A question whose answer is a MEASUREMENT is
refused outright — `artifact_evidence.requires_execution` — because "does this code produce
91.4?" is not answerable by reading it, and a route that claimed otherwise would let a
reading of the source acquit or convict a number. What it CAN settle is a question about
the artifact: whether a file the paper names is there, what a configuration sets, whether
the checkout implements an experiment at all.

**And it never suppresses an execution.** `planner` reaches `ARTIFACT_INSPECTION_ONLY`
only where NO executable route applies, so a target that could be run is never diverted
here — a cheap route that settles nothing must never cancel an escalation.

**The authors'-code auditor's TASK is persisted from HERE, once per paper, and nowhere
else.** `_reviewer_facts` below is gated on an AUDITED snapshot existing (no clean pinned
checkout means no task); when one exists and no valid sealed reading covers it, it persists
the prompt that asks the one question this route could not ask on its own — does the
released code do what the method section says — for `harness.tasks.pending` to list as a
task. Once answered and sealed, it gets back `ArtifactFact`s that are ALREADY relocated,
ALREADY authority-ranked, and ALREADY capped at `PAPER_ARTIFACT_MISMATCH` only where the
auditor named a DETERMINISTIC identity basis that itself relocated. Nothing here re-derives
or loosens any of that; it only decides WHERE a fact the auditor earned may be counted. A
fact whose `about_the_paper`/`endpoints_only` reading answers the BROAD
implementation-correspondence question is folded into a per-target inspection only when
that target's OWN scope is `IMPLEMENTATION_CORRESPONDENCE` — the scope essentially every
real repository-paper artifact target carries — and always into the paper-level aggregate,
which asks nothing narrower. It is never folded into a NARROW bounded per-target question
(`ENTRYPOINT_PRESENCE`, `FILE_PRESENCE`, ...): those stay answered purely by the
deterministic probes below, so a mismatch found anywhere in the checkout can never be
mistaken for an answer to an unrelated bounded question a different target asked.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import artifact_evidence, artifact_review_driver, state
from .. import locate as claims
from ..reviewer_cli import prompt_fingerprint
from ..schema import (ArtifactFact, ArtifactInspection, DiscoveredObject, PaperDoc,
                      PlanDecision, TargetOutcome, TargetSet)
from ..config import Config
from ..prompts import artifact_review as AP

ACTION = "ARTIFACT_INSPECTION_ONLY"


def planned(target_set: TargetSet | None) -> list[tuple[DiscoveredObject, PlanDecision]]:
    """The (object, plan) pairs this route owns. Never anything `probe` will execute."""
    if target_set is None:
        return []
    by_id = {o.target_id: o for o in target_set.objects}
    out = []
    for plan in target_set.plans:
        if plan.action != ACTION or plan.requires_execution:
            continue
        obj = by_id.get(plan.target_id)
        if obj is not None:
            out.append((obj, plan))
    return out


def facts_for(doc: PaperDoc, root: str | Path, snap, obj: DiscoveredObject
              ) -> list[ArtifactFact]:
    """The deterministic probes this target's question licenses. No model, no execution.

    Bounded by the QUESTION rather than by what the checkout happens to contain: a route
    that ran every probe over every file would produce a page of true and irrelevant
    statements, which is the `CROSSREF_UNRESOLVED` failure in another costume — twelve
    correct observations that mean nothing about the paper.

    Three probes, and each answers something a referee actually asks of a released
    artifact: does it advertise a way to run anything at all; does it declare the stack the
    paper's method needs; and — where the paper NAMES a path — is that path there.
    """
    out = [artifact_evidence.entrypoint_fact(root, snap)]
    for name in sorted(_frameworks_named(doc))[:4]:
        out.append(artifact_evidence.dependency_fact(root, snap, name))
    for rel, quote in _paths_named(doc)[:6]:
        out.append(artifact_evidence.file_fact(root, snap, rel, named_by_paper=quote))
    return out


_PATH_HINT = ("README", "requirements.txt", "environment.yml", "setup.py", "train.py",
              "main.py", "run.py", "eval.py", "config.yaml")


def _paths_named(doc: PaperDoc) -> list[tuple[str, str]]:
    """(relative path, the paper sentence that names it), for paths the PAPER mentions.

    The paper half is a real quotation minted by `claims`, not a paraphrase, because
    `file_fact` refuses to treat an absence as anything unless the paper named the thing —
    a file nobody claimed should exist is not a defect. Searching for a fixed list of
    conventional names rather than parsing arbitrary paths out of prose is deliberate: an
    extracted "path" that is really a hyphenated word would make this route report a
    missing file that was never claimed.
    """
    out: list[tuple[str, str]] = []
    for section in doc.sections:
        text = section.text or ""
        for hint in _PATH_HINT:
            at = text.find(hint)
            if at < 0:
                continue
            start = text.rfind(". ", 0, at) + 2
            end = text.find(". ", at)
            sentence = text[start:end + 1 if end > 0 else len(text)].strip()
            if len(sentence) < 20:
                continue
            minted = claims.mint(doc, sentence)
            if minted.resolved and not any(p == hint for p, _ in out):
                out.append((hint, minted.quote))
    return out


def _frameworks_named(doc: PaperDoc) -> set[str]:
    """Frameworks the paper's own text names, so a declaration probe has something to ask."""
    text = " ".join((s.text or "") for s in doc.sections).lower()
    return {name for name in ("torch", "pytorch", "tensorflow", "jax", "transformers",
                              "sklearn", "scikit-learn", "numpy")
            if name in text}


def distinct_facts(facts: list[ArtifactFact]) -> list[ArtifactFact]:
    """The same fact about the same checkout, established once rather than once per target.

    `facts_for` runs per target and a fact about the CHECKOUT does not vary with which
    paper statement it is being held against — so `acl`'s nine targets each produced the
    identical `ENTRYPOINT_PRESENCE` fact about `npv.py`, and the route's own record then
    read "9 bounded fact(s) were established about the checkout". One was. It was checked
    against nine statements and answered none of them, which is a different sentence and
    the true one.

    The key is what makes two facts THE SAME FACT: the probe that produced it, the exact
    span it relocated to, the bounded question it settles, and the paper reference it was
    raised against. Identity is deliberately not the statement text — that is precisely
    what varies while the fact does not — and deliberately not the `statement`, which is
    the harness's own prose about the fact rather than the fact.

    Order is preserved, so the first establishment of each fact is the one kept and the
    record still reads in the order the route worked.
    """
    seen: set[tuple] = set()
    out: list[ArtifactFact] = []
    for f in facts:
        span = f.span
        key = (f.probe, f.settles, f.authority, f.paper_ref,
               getattr(span, "file", ""), getattr(span, "line", 0),
               getattr(span, "end_line", 0), getattr(span, "file_sha256", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# --------------------------------------------------------------------------- #
# The authors'-code auditor: what it is shown, and what comes back
# --------------------------------------------------------------------------- #
# Bounded exactly the way `stages.validation.method_text` bounds a designer's method text:
# a reader handed the whole paper is being asked to find the method section, which is not
# the task, and an unbounded prompt is a cost nobody agreed to pay.
_METHOD_CHARS = 6000
_METHOD_TITLE_HINTS = ("method", "approach", "model", "architecture", "implementation",
                       "experiment", "setup", "training", "algorithm")


def _method_text(doc: PaperDoc) -> str:
    """The paper's own account of what it did, bounded — never the whole paper.

    Sections whose OWN title names a method/experimental concern, in document order. This
    is the closest bounded substitute for "the method section" this harness can name
    without asking a model to find it first — the same reasoning `stages.validation.
    method_text` uses, generalised from "names one of these arm labels" (which the
    artifact route has no arm labels for) to "looks like the part of the paper that
    describes how it was built or run". A title convention this extraction did not
    recognise falls back to the whole document, still bounded: an auditor shown nothing
    cannot read the paper any better than one shown everything within the same budget.
    """
    picked = [s for s in doc.sections
             if any(h in (s.title or "").lower() for h in _METHOD_TITLE_HINTS)]
    text = "\n\n".join(f"[{s.title or f'section {s.section_idx}'}] {s.text or ''}"
                       for s in picked)
    if not text.strip():
        text = "\n\n".join((s.text or "") for s in doc.sections)
    return text[:_METHOD_CHARS]


_REPORTED_MAX = 24


def _reported_text(doc: PaperDoc) -> str:
    """A bounded summary of the quantities THIS HARNESS already extracted, for context only.

    Not a new extraction, and not offered as anything the reader may cite in place of the
    paper: every code citation the reader returns is still relocated by `artifact_evidence.
    relocate`, and every paper citation still by `claims`, regardless of what it read here.
    This exists so the reader knows which numbers this review already has an address for,
    the same courtesy `_paths_named` extends by naming which paths are already claimed.
    """
    lines = []
    for q in doc.reported_numbers[:_REPORTED_MAX]:
        bits = " ".join(x for x in (q.benchmark, q.metric, q.method) if x)
        if bits and q.value:
            lines.append(f"{bits}: {q.value}")
    return "\n".join(lines)


_TREE_MAX_ENTRIES = 400
_TREE_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "env"}


def _tree_text(root: str | Path) -> str:
    """A bounded file listing of the pinned checkout — the MAP, not the contents.

    The reader's own confinement grants it `Read` and `Grep` over the checkout directly,
    so this is only what tells it the checkout has a shape at all; a hundred-thousand-file
    repository must not become a hundred-thousand-line prompt. Sorted, so the listing is
    the same call to call rather than a filesystem's own arbitrary order.
    """
    root = Path(root)
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:                      # pragma: no cover — rglob only yields children
            continue
        if any(part in _TREE_SKIP_DIRS for part in rel.parts[:-1]):
            continue
        out.append(str(rel).replace("\\", "/"))
        if len(out) >= _TREE_MAX_ENTRIES:
            break
    return "\n".join(out)


def _reviewer_task_prompt_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "tasks" / "artifact_review.md"


def _reviewer_facts(cfg: Config, pid: str, doc: PaperDoc, root: str | Path, snap,
                    url: str, statements: list[str]) -> list[ArtifactFact]:
    """The authors'-code auditor's facts for this paper, or `[]`.

    One precondition, checked HERE: `snap.audited` requires a commit, a tree hash and a
    clean working tree — "there IS a pinned, audited repository checkout" — so a directory
    that exists but is not a cleanly pinned git checkout gets no call, same as none at all.

    **A valid sealed inspection for THIS commit and THIS prompt is reused, never
    re-requested.** Re-dispatching on every unrelated rerun would overwrite
    `artifact/<pid>.route.json` with a fresh, possibly different model call — losing an
    already-established fact for no reason tied to this paper. The prompt hash is computed
    from the SAME `prompt_text` this function already builds, so a cache hit requires the
    pinned commit AND every paper-derived input the model was actually shown (title, method
    text, tree listing, URL, reported quantities) to still match. `statements` is
    deliberately NOT part of that hash: it is not shown to the auditor at all (`AP.build`'s
    signature has no such parameter), so a target set that changed between runs (a new
    grade, a different discovery pass) does not by itself force a fresh reading of code
    that has not moved.

    **On a cache miss, this PERSISTS the prompt instead of dispatching anything.** No
    subprocess this function may spawn any more: the prompt is written to
    `projects/<pid>/tasks/artifact_review.md`, a deterministic path `harness.tasks.pending`
    checks on every call, so the controlling session can list it as an `artifact_review`
    task for one of its own subagents. Writing the file is idempotent, so re-rendering it
    on every cache-missed pipeline pass is a cheap overwrite, not a new request. Until a
    subagent answers it and `harness.tasks.seal` calls `artifact_review_driver.accept`,
    this returns `[]` — "no reviewer answered yet", so nothing downstream needs to know
    delegation became asynchronous.
    """
    if snap is None or not snap.audited:
        return []
    prompt_text = AP.build(
        doc.title, _method_text(doc), _tree_text(root), snap.commit,
        repo_url=url, repo_path=str(root), reported_text=_reported_text(doc))
    path = _reviewer_task_prompt_path(cfg, pid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt_text, encoding="utf-8")
    except OSError:
        pass
    # Fingerprinted the SAME way `harness.tasks` fingerprints every other role's prompt
    # (`reviewer_cli.prompt_fingerprint`, hashing the BYTES ON DISK) rather than the
    # in-memory string: text-mode newline translation can change what actually landed on
    # disk, and a subagent — or `harness.tasks.seal` — can only ever hash what is really
    # there. Hashing the string instead would make a valid answer look permanently stale.
    prompt_sha = prompt_fingerprint(path)
    cached = artifact_review_driver.load(cfg, pid, commit=snap.commit,
                                         prompt_sha256=prompt_sha)
    if cached is not None:
        return list(cached.facts)
    return []


def run_route(cfg: Config, pid: str, doc: PaperDoc, target_set: TargetSet | None,
              root: str | Path, *, url: str = "",
              extra_facts: list[ArtifactFact] | None = None
              ) -> tuple[list[TargetOutcome], ArtifactInspection | None]:
    """Every ARTIFACT_INSPECTION_ONLY target, against one checkout. ([], None) when there are none.

    One snapshot for the whole pass, because every fact must be tied to ONE tree: two
    targets reading the same checkout at two moments could otherwise disagree about what
    the artifact says, and neither would be wrong.

    **A broad question is DECOMPOSED, not discharged.** A statement like *"the released
    repository <url> implements the described method"* is never answered by a bounded
    artifact fact (a file being present does not settle whether code implements a method):
    the route answers the bounded questions it CAN answer, records each against its own
    scope, and leaves the broad claim open with the reason. A referee reads "these things
    about the artifact are established, and whether the code implements the method is
    still open", which is what is actually true.
    """
    pairs = planned(target_set)
    if not pairs or not Path(root).is_dir():
        return [], None

    snap = artifact_evidence.snapshot(root, url)
    # EVERY CLAIM THIS ROUTE WAS ASKED ABOUT, computed ONCE rather than accumulated across
    # the loop below: the auditor is dispatched before any target's own inspection runs,
    # so it needs the full statement list up front, and the loop's own `statements.append`
    # would otherwise duplicate exactly this list one entry at a time.
    statements: list[str] = [s for s in ((o.claim_text or "").strip() for o, _ in pairs) if s]
    # THE AUTHORS'-CODE AUDITOR'S TASK, ONCE PER PAPER, BEFORE ANY PER-TARGET INSPECTION.
    # `_reviewer_facts` is a no-op (besides persisting the task prompt) unless `snap` is an
    # audited (clean, pinned) checkout with a valid sealed reading already covering it —
    # see its own docstring. What comes back is already relocated and already
    # authority-ranked; this stage decides only WHERE it may count.
    reviewer_facts = _reviewer_facts(cfg, pid, doc, root, snap, url, statements)

    outcomes: list[TargetOutcome] = []
    # ACCUMULATED PER TARGET AND DEDUPLICATED ONCE, at the bottom. Each target's own
    # `inspect` call reasons over its OWN facts and is unaffected; what this fixes is the
    # aggregate record and the number printed into it. `reviewer_facts` seeds it up front
    # for the same reason `extra_facts` already does: the aggregate asks the one BROAD
    # question (`IMPLEMENTATION_CORRESPONDENCE`) a verified paper/code mismatch actually
    # bears on, whichever target's claim happened to raise it.
    all_facts: list[ArtifactFact] = list(extra_facts or []) + reviewer_facts
    escalations: list[str] = []
    for obj, plan in pairs:
        # A MEASUREMENT IS NOT READABLE. Refused per target rather than filtered at plan
        # time as well, so a plan that reached here by another path still cannot use this
        # route to answer a question whose answer is a number.
        if artifact_evidence.requires_execution(obj.question_kind):
            outcomes.append(TargetOutcome(
                target_id=obj.target_id, disposition="COMPARISON_BLOCKED",
                action=plan.action, route=plan.route, launched=0,
                reason=(f"this is a {obj.question_kind} question, whose answer is a "
                        f"measured result. Reading the released code can narrow what "
                        f"would have to run and cannot produce the number, so static "
                        f"inspection is refused for it rather than allowed to appear to "
                        f"settle it.")))
            continue

        statement = (obj.claim_text or "").strip()
        scope = artifact_evidence.question_scope(obj.question_kind, statement)
        facts = facts_for(doc, root, snap, obj)
        all_facts += facts

        # THE AUDITOR'S FACTS REACH A NARROW PER-TARGET QUESTION ONLY WHEN THAT TARGET'S
        # OWN SCOPE IS THE BROAD ONE. A verified mismatch found anywhere in the checkout
        # is real evidence about "does the code implement the paper" — the scope every
        # real repository-paper artifact target carries (measured: all four corpus
        # targets ask exactly this) — and is never real evidence about an unrelated
        # NARROW question (does an entrypoint exist, is a path present, ...) that some
        # other target happens to be asking. Scope is the harness's OWN, already-derived
        # classification (`question_scope`), so this is the same bounded-fact discipline
        # `discharge`'s `matching` list already applies to level-1 facts — not a new rule.
        target_reviewer_facts = (reviewer_facts if scope == "IMPLEMENTATION_CORRESPONDENCE"
                                 else [])

        one = artifact_evidence.inspect(
            doc, root, url=url, target_id=obj.target_id, scope=scope,
            statements=[statement] if statement else [],
            facts=list(extra_facts or []) + facts + target_reviewer_facts)
        disposition = artifact_evidence.outcome_disposition(one)

        # WHAT THIS BUYS A LATER ROUTE, which is the honest value of an inspection that
        # settled no broad question. Derived from the PROBE rather than from any reading,
        # recorded into a list of sentences, and acted on by nothing here.
        escalations += artifact_evidence.escalations_from(facts)

        if scope == "IMPLEMENTATION_CORRESPONDENCE":
            settled = [f for f in facts if f.authority == "ARTIFACT_FACT" and f.settles]
            parts = artifact_evidence.decompose(statement, url)
            reason = (
                f"{one.reason} The bounded questions this route did answer against "
                f"{snap.commit[:10]}: "
                + "; ".join(f"{sc} — {q}" for sc, q in parts)
                + f". {len(settled)} of them produced a fact.")
        else:
            reason = (f"the pinned checkout was read for this {scope} question: "
                      f"{one.reason}. Reading the artifact establishes what the released "
                      f"code does and never that a reported result is wrong.")

        outcomes.append(TargetOutcome(
            target_id=obj.target_id, disposition=disposition,
            action=plan.action, route=plan.route, launched=0, provenance="artifact",
            reason=reason))

    whole = artifact_evidence.inspect(
        doc, root, url=url, statements=statements, facts=distinct_facts(all_facts),
        scope="IMPLEMENTATION_CORRESPONDENCE",
        escalations=sorted(set(escalations)))
    # UNSEALED, DELIBERATELY. `route.json` is a plain overwrite rather than a third
    # hand-rolled hash+sidecar, because it is not this stage's own cache to protect: its
    # content is `inspect()` applied to `doc` (fixed per paper), `root` at `snap.commit`
    # (fixed once the checkout is pinned), `statements` and `extra_facts` (this INVOCATION's
    # own target set — legitimately free to differ run to run as discovery/grading changes,
    # and it is correct for a rerun to reflect that), and `reviewer_facts` — which
    # `_reviewer_facts` above now serves from `artifact_review_driver.load`'s cache whenever
    # the commit and prompt are unchanged, rather than re-dispatching the live model call.
    # With the model call itself made idempotent, this write is: given the same target set,
    # byte-identical (`ArtifactInspection`/`ArtifactFact` carry no timestamp field), and
    # given a changed target set, correctly different. Sealing this file would either
    # duplicate a cache the inspection-level seal already provides, or — worse — freeze a
    # target-set-dependent artifact against a legitimate reason for it to change.
    try:
        state.write_json(
            state.project_dir(cfg, pid) / "artifact" / f"{pid}.route.json",
            whole.model_dump())
    except OSError:
        pass
    return outcomes, whole


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.stages.artifact
    import json
    import subprocess
    import sys
    import tempfile

    from .. import taxonomy
    from ..schema import ClaimRef, Section

    doc = PaperDoc(paper_id="p", title="T", n_pages=1, repo_url="https://example.invalid/r",
                   sections=[Section(section_idx=0, title="Method", page_start=1,
                                     text="We release requirements.txt alongside the "
                                          "code and train every model in PyTorch.")])

    def obj(tid, kind, claim="the released code accompanies the paper"):
        return DiscoveredObject(target_id=tid, question_kind=kind, claim_text=claim,
                                ref=ClaimRef(ref="P0:0-10", kind="prose_claim",
                                             resolution="resolved"),
                                routes=["ARTIFACT_INSPECTION"])

    def plan(tid):
        return PlanDecision(target_id=tid, action=ACTION, route="ARTIFACT_INSPECTION",
                            requires_execution=False)

    ts = TargetSet(paper_id="p",
                   objects=[obj("t1", "SPECIFICATION"), obj("t2", "PRINTED_QUANTITY")],
                   plans=[plan("t1"), plan("t2")])
    assert [o.target_id for o, _ in planned(ts)] == ["t1", "t2"]
    assert planned(None) == []
    # A plan that would EXECUTE is never this route's, whatever its action says.
    running = TargetSet(paper_id="p", objects=[obj("t3", "SPECIFICATION")],
                        plans=[PlanDecision(target_id="t3", action=ACTION,
                                            requires_execution=True)])
    assert planned(running) == []

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        (root / "requirements.txt").write_text("torch==2.1.0\n", encoding="utf-8")
        (root / "train.py").write_text("print('hi')\n", encoding="utf-8")

        def git(*a):
            subprocess.run(["git", *a], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            git("init", "-q")
            git("config", "user.email", "s@e")
            git("config", "user.name", "s")
            git("add", "-A")
            git("commit", "-qm", "c")
        except (OSError, subprocess.CalledProcessError):   # pragma: no cover
            print("harness.stages.artifact self-check skipped: git unavailable")
            sys.exit(0)

        cfg = Config(projects_dir=Path(td) / "projects")
        state.create_project(cfg, "", "T", pid="p")
        outcomes, whole = run_route(cfg, "p", doc, ts, root, url="https://example.invalid/r")
        assert len(outcomes) == 2, outcomes
        broad, refused = outcomes[0], outcomes[1]

        # "The repository implements the described method" is a semantic correspondence
        # question; bounded facts about the checkout do not answer it.
        assert broad.disposition == "ARTIFACT_INSPECTION_INCONCLUSIVE", broad
        assert broad.evidence_state != "ARTIFACT_EVIDENCE"
        assert broad.resolution_state == "UNRESOLVED"
        assert "supporting evidence for it and are not its answer" in broad.reason
        # ...and the bounded questions it DID answer are named, so the inspection is not
        # reported as having produced nothing.
        for scope in ("ENTRYPOINT_PRESENCE", "MANIFEST_PRESENCE", "DEPENDENCY_DECLARED",
                      "FILE_PRESENCE"):
            assert scope in broad.reason, scope

        # A REPRODUCTION-shaped question cannot be answered by reading.
        assert refused.disposition == "COMPARISON_BLOCKED", refused
        assert "whose answer is a measured result" in refused.reason

        # A BOUNDED target IS settled, and the state it reaches is the artifact one —
        # about the checkout, never about the paper.
        narrow_ts = TargetSet(
            paper_id="p",
            objects=[obj("t4", "ENTRYPOINT_PRESENCE",
                         claim="is there a runnable entrypoint the repository advertises?")],
            plans=[plan("t4")])
        narrow_out, _ = run_route(cfg, "p", doc, narrow_ts, root, url="u")
        assert narrow_out[0].disposition == "ARTIFACT_FACT_ESTABLISHED", narrow_out[0]
        assert narrow_out[0].evidence_state == "ARTIFACT_PROPERTY_ESTABLISHED"
        assert narrow_out[0].resolution_state == "RESOLVED_FROM_ARTIFACT"
        assert narrow_out[0].evidence_state not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
        assert narrow_out[0].establishes_failure is False

        # What the inspection buys a LATER route is recorded and acted on by nothing here.
        assert whole is not None and whole.escalations, whole.escalations
        assert any("narrows the candidate commands" in e for e in whole.escalations)
        assert (state.project_dir(cfg, "p") / "artifact" / "p.route.json").exists()

        # A checkout that is not there is not a route.
        assert run_route(cfg, "p", doc, ts, Path(td) / "nope") == ([], None)

        # On a cache miss over an audited checkout, `_reviewer_facts` PERSISTS the prompt
        # to a deterministic path for `harness.tasks.pending` to list as a task; once an
        # answer is sealed at that exact prompt hash, the next call reuses it from cache.
        # Authority/scope capping is proven by `artifact_review_driver.py`'s own
        # self-check; what is under test here is the GATING and FACT-FLOW between them.
        task_path = _reviewer_task_prompt_path(cfg, "p")

        # NO CHECKOUT -> nothing to inspect, whatever else is true.
        assert run_route(cfg, "p", doc, ts, Path(td) / "nope") == ([], None)

        # A REAL AUDITED CHECKOUT, NO SEALED READING YET -> the task prompt is persisted,
        # and this call's own reviewer facts are empty (nobody has answered it yet).
        run_route(cfg, "p", doc, ts, root, url="u")
        assert task_path.is_file(), "a cache miss must persist the artifact_review task prompt"
        prompt_sha = hashlib.sha256(task_path.read_bytes()).hexdigest()

        # A SUBAGENT ANSWERS: `artifact_review_driver.accept` seals a code-only reading at
        # that exact prompt hash — the same call `harness.tasks.seal` makes for a real
        # `artifact_review` task.
        concern_raw = json.dumps({"concerns": [
            {"kind": "SUSPICIOUS_IMPLEMENTATION", "title": "greeting", "statement": "odd",
             "file": "train.py", "code_quote": "print('hi')"}], "notes": "n"})
        artifact_review_driver.accept(
            cfg, "p", doc, root, concern_raw, url="u", reader="session subagent generator",
            mode="SESSION_SUBAGENT", prompt_sha256=prompt_sha)

        # THE NEXT CALL REUSES THE SEALED READING FROM CACHE, and its relocated fact
        # reaches the paper-level aggregate — the broad question a code-only observation
        # actually bears on.
        _outs, whole_r = run_route(cfg, "p", doc, ts, root, url="u")
        assert whole_r is not None and any(
            f.probe == "code_review:SUSPICIOUS_IMPLEMENTATION" for f in whole_r.facts)

        # AND A NARROW TARGET'S OWN BOUNDED QUESTION IS UNTOUCHED BY A CHECKOUT-WIDE
        # READING THAT HAS NOTHING TO DO WITH IT — the scope gate is what keeps a
        # checkout-wide finding from contaminating a target asking something unrelated.
        narrow_out2, _ = run_route(cfg, "p", doc, narrow_ts, root, url="u")
        assert narrow_out2[0].disposition == "ARTIFACT_FACT_ESTABLISHED", narrow_out2[0]

    print("harness.stages.artifact self-check ok")
