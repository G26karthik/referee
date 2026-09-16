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
here. That is the defect `PAPER_INTERNAL_CHECK` had when it sat in `planner._RESOLVING`:
it was cheap, it settled nothing, and it cancelled 100% of the shipped corpus's
escalations.
"""
from __future__ import annotations

from pathlib import Path

from .. import artifact_evidence, claims, state
from ..artifacts import (ArtifactFact, ArtifactInspection, DiscoveredObject, PaperDoc,
                         PlanDecision, TargetOutcome, TargetSet)
from ..config import Config

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


def run_route(cfg: Config, pid: str, doc: PaperDoc, target_set: TargetSet | None,
              root: str | Path, *, url: str = "",
              extra_facts: list[ArtifactFact] | None = None
              ) -> tuple[list[TargetOutcome], ArtifactInspection | None]:
    """Every ARTIFACT_INSPECTION_ONLY target, against one checkout. ([], None) when there are none.

    One snapshot for the whole pass, because every fact must be tied to ONE tree: two
    targets reading the same checkout at two moments could otherwise disagree about what
    the artifact says, and neither would be wrong.
    """
    pairs = planned(target_set)
    if not pairs or not Path(root).is_dir():
        return [], None

    snap = artifact_evidence.snapshot(root, url)
    outcomes: list[TargetOutcome] = []
    all_facts: list[ArtifactFact] = list(extra_facts or [])
    statements: list[str] = []
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
        facts = facts_for(doc, root, snap, obj)
        one = artifact_evidence.inspect(
            doc, root, url=url, target_id=obj.target_id,
            statements=[statement] if statement else [],
            facts=list(extra_facts or []) + facts)
        all_facts += facts
        if statement:
            statements.append(statement)
        outcomes.append(TargetOutcome(
            target_id=obj.target_id,
            disposition=artifact_evidence.outcome_disposition(one),
            action=plan.action, route=plan.route, launched=0, provenance="artifact",
            reason=(f"the pinned checkout was read for this question: {one.reason}. "
                    f"Reading the artifact establishes what the released code does and "
                    f"never that a reported result is wrong.")))

    whole = artifact_evidence.inspect(doc, root, url=url, statements=statements,
                                      facts=all_facts)
    try:
        state.write_json(
            state.project_dir(cfg, pid) / "artifact" / f"{pid}.route.json",
            whole.model_dump())
    except OSError:
        pass
    return outcomes, whole


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.stages.artifact
    import subprocess
    import sys
    import tempfile

    from ..artifacts import ClaimRef, Section

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
        settled, refused = outcomes[0], outcomes[1]
        # An artifact-only question REACHES the state that had no way in.
        assert settled.disposition == "ARTIFACT_RESOLVED", settled
        assert settled.evidence_state == "ARTIFACT_EVIDENCE"
        assert settled.resolution_state == "RESOLVED_FROM_ARTIFACT"
        # And it is NOT a material failure, whatever it found.
        assert settled.establishes_failure is False
        # A REPRODUCTION-shaped question cannot be answered by reading.
        assert refused.disposition == "COMPARISON_BLOCKED", refused
        assert "whose answer is a measured result" in refused.reason
        assert whole is not None and whole.discharged
        assert (state.project_dir(cfg, "p") / "artifact" / "p.route.json").exists()

        # A checkout that is not there is not a route.
        assert run_route(cfg, "p", doc, ts, Path(td) / "nope") == ([], None)

    print("harness.stages.artifact self-check ok")
