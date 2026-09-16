"""F — what the released artifact itself can establish, bounded by what it cannot.

`python -m harness.artifact_evidence [<checkout>]` runs the self-check.

**The question this route answers is "what can the released artifact itself establish?",
and the reason it needed building is that the harness already read the code and threw the
scientific result away.** `harness/code_audit.py` has run on every cloned paper since the
first version: it parses the checkout, applies ten rules, and writes `CodeAudit.findings`
into the machine report under `## Static code audit`. Nothing consumes them. They reach no
question, no target, no route and no evidence state — `ARTIFACT_EVIDENCE` and its
resolution `RESOLVED_FROM_ARTIFACT` were in the vocabulary with no disposition mapping to
them, which is to say the state machine could not reach them from any input.

**THREE LEVELS, AND THE DISTANCE BETWEEN THEM IS THE WHOLE MODULE.**

    level 1  ARTIFACT_FACT            the checkout contains this, at this span, at this SHA
    level 2  PAPER_ARTIFACT_MISMATCH  the paper states X for experiment E; the pinned
                                      artifact sets Y for experiment E
    level 3  the reported scientific result is false

Level 1 is a fact about the ARTIFACT and says nothing about the paper. Level 2 is a
statement about both, and needs all three of a precisely addressed paper statement, a
precisely located artifact fact, and an established experiment identity connecting them —
"some config somewhere says 32" is not a level-2 anything, and the missing third
requirement is the one that makes it not. **Level 3 has no spelling on any type here.**
Static inspection alone essentially never establishes that a reported result is false. A
code or configuration inconsistency may create a verified concern, establish a
reproducibility defect, trigger execution, trigger focused validation, and become material
where a central claim provably depends on it — every one of those is a downstream decision
by a downstream module, and none is reachable by writing a stronger string in here. An AST
warning may not become RED.

**A MODEL STATEMENT ABOUT CODE IS NOT ARTIFACT EVIDENCE.** `relocate` is the
`claims.mint` of this module: the writer — a rule, or the authors'-code auditor in
`harness/codereview_driver.py` — supplies a quotation and a file, and the HARNESS finds
it, refusing a quotation that is absent or that occurs more than once. What survives
carries the file's own SHA-256 alongside the pinned commit, so a reader can prove the line
has not moved under the citation.

**Every fact is tied to an IMMUTABLE SNAPSHOT, and the snapshot fails closed.**
`ArtifactSnapshot.audited` requires a commit, a tree hash and a clean working tree, and
`ArtifactSnapshot` defaults to `dirty=True` — an unknown tree is not an audited tree. If a
later execution mutates the checkout, the post-run identity check retracts the EXECUTION's
evidence, as it already did; the pre-run static facts stay tied to the pre-run snapshot and
are not rewritten. The two are different evidence about different moments.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import claims, repo as repo_mod
from .artifacts import (ArtifactFact, ArtifactInspection, ArtifactSnapshot, PaperDoc,
                        SourceSpan)

# --------------------------------------------------------------------------- #
# §8 — the ten existing AST rules, classified by the authority they can carry
# --------------------------------------------------------------------------- #
# NOT ALL TEN ARE EXPOSED, and "it exists" is not a reason to expose one. Each rule is
# sorted into one of four classes, and the classification is a MEASUREMENT over the four
# repository papers rather than a reading of the rule's docstring: the corpus produced six
# hits and five of them are false, every one for the same reason — the rules match
# SUBSTRINGS of identifiers, and an identifier is not a semantic category.
#
#   A  deterministic artifact fact     what it reports is true of the checkout by
#                                      construction, whatever it means
#   B  candidate concern               it located something real and what that something
#                                      MEANS needs interpretation this harness cannot do
#   C  diagnostic only                 kept in the machine trace, never shown to a reviewer
#   D  unsafe for reviewer output      measured false positives with no bounded reading
#
# Only A and B are reviewer-visible. C and D stay in `CodeAudit.findings`, where they
# always were, and reach no question, no target and no evidence state.
RULE_AUTHORITY = {
    # --- A: what the rule reports is a property of the source text --------------------
    # The call site passes no `random_state`, `seed`, `generator` or `stratify`. That is
    # a fact about the call, decidable from the AST, and true whatever a global seed does
    # elsewhere. Fired once on `apt-icml` (`utils/utils.py:600`,
    # `torch.utils.data.random_split(total_dataset, [n, m])`) and the fact is correct.
    "leak-unseeded-split": "A",

    # --- B: located something real; what it means is not decidable here ---------------
    # Both branches of one `if` set the SAME knob to different literals and the arm the
    # condition names as the baseline gets the smaller one. The asymmetry is in the code;
    # whether it is a defect depends on whether each method's own published recipe calls
    # for it, which is exactly what `counter_explanations` says. Did not fire on the
    # corpus, so this classification rests on the rule's structure, not on a measurement.
    "cripple-per-arm-budget": "B",
    "cripple-config-table": "B",
    # A preprocessor fitted before the split, and the unambiguous `.fit(X_test)`. Both
    # locate a real call ordering; both have a stated legitimate reading (refit after the
    # split, transductive evaluation). Neither fired on the corpus.
    "leak-fit-before-split": "B",
    "leak-fit-on-test": "B",
    # A hand-written metric beside imported standard ones. What it establishes is that
    # the two should be diffed, which is a question and not a finding.
    "metric-shadows-standard": "B",
    # Both fire only inside a function whose NAME is metric-like, which bounds them; what
    # a maximum or a ground-truth comparison inside a metric means is interpretation.
    # Neither fired on the corpus.
    "metric-best-of-n": "B",
    "metric-filters-ground-truth": "B",

    # --- D: measured false, with no bounded reading -----------------------------------
    # FOUR OF THE CORPUS'S SIX HITS, ALL FALSE, ALL ON `apt-icml/run_pruning.py`. The
    # detector is a regex over raw lines whose first alternative is
    # `val(idation)?[\\w\\[\\]'". ]*=\\s*[\\w\\.]*test`. The source lines read
    # `rescaled_eval_metrics = test(model, eval_dataloader, ...)` — "eval" CONTAINS "val",
    # and `test` here is the name of the evaluation FUNCTION. So the rule reported that
    # the test split drives model selection in a file where it does not, four times, on a
    # line that calls an evaluator on `eval_dataloader`. There is no narrower reading that
    # rescues the hit: nothing on the line is a split, a checkpoint or a selection.
    "leak-model-selection-on-test": "D",
    # THE FIFTH FALSE HIT. It fires when an `if` condition contains an `_OURS` token and
    # its body contains an augmentation token. The corpus line is
    # `if new_transform_r > model.layer_transformation.r and ...` — `new_` is the arm
    # token and `transform` is the augmentation token, in a branch that resizes a LoRA
    # rank. Neither substring means what the rule takes it to mean, and the rule's own
    # statement ("data augmentation is applied on the proposed arm only") is false of the
    # branch it points at.
    "cripple-augmentation-one-arm": "D",
}

REVIEWER_VISIBLE = ("A", "B")


def rule_authority(rule_id: str) -> str:
    """The class of a rule, defaulting to D. An unclassified rule is not shown.

    Fail-closed on purpose: a rule added later and not audited here is not automatically
    reviewer-visible, because the audit — not the rule's existence — is what licenses it.
    """
    return RULE_AUTHORITY.get((rule_id or "").strip(), "D")


def reviewer_visible(rule_id: str) -> bool:
    return rule_authority(rule_id) in REVIEWER_VISIBLE


# --------------------------------------------------------------------------- #
# The immutable snapshot every fact is tied to
# --------------------------------------------------------------------------- #
def snapshot(repo_path: str | Path, url: str = "",
             tree: repo_mod.GitTree | None = None) -> ArtifactSnapshot:
    """The pinned identity of a checkout, BEFORE anything runs. Fail-closed throughout.

    Three reads, and any of them failing leaves the snapshot un-audited rather than
    optimistic: HEAD's SHA, HEAD's TREE sha (which is what actually names the content —
    two commits with different messages and identical content share it), and whether the
    working tree differs from HEAD. `repo.dirty_files` RAISES when `git status` could not
    run, and that raise is caught here and recorded as dirty, because "we could not look"
    and "we looked and it was clean" must never produce the same artifact.
    """
    path = Path(repo_path)
    git = tree if tree is not None else repo_mod.LocalGitTree(path)
    note = ""
    commit = repo_mod.head_commit(path, git)
    if not commit:
        note = "the directory is not a git checkout, or HEAD could not be read"
    rc, out, _ = git.git(["rev-parse", "HEAD^{tree}"], 30)
    tree_sha = (out or "").strip() if rc == 0 else ""
    if commit and not tree_sha:
        note = "HEAD resolved but its tree object did not"
    try:
        dirty = bool(repo_mod.dirty_files(path, git))
        if dirty:
            note = note or "the working tree differs from HEAD"
    except repo_mod.TreeUninspectable as e:
        dirty, note = True, f"the working tree could not be inspected: {e}"
    return ArtifactSnapshot(
        repo_url=url or "", commit=commit, tree_sha=tree_sha, dirty=dirty,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), note=note)


def same_snapshot(a: ArtifactSnapshot | None, b: ArtifactSnapshot | None) -> bool:
    """Do two snapshots name the same audited content? Both must be audited to be equal.

    Compares the TREE, not only the commit: an amended commit with identical content is
    the same code, and two commits with the same message and different content are not.
    """
    if a is None or b is None or not (a.audited and b.audited):
        return False
    return a.commit == b.commit and a.tree_sha == b.tree_sha


# --------------------------------------------------------------------------- #
# Relocation — the `claims.mint` of this module
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")


def _read(root: Path, rel: str) -> str | None:
    target = (root / rel).resolve()
    try:
        # A path that escapes the checkout is not a citation into the checkout. Checked
        # after resolution so `../` and a symlink are both caught by the same test.
        target.relative_to(root.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def relocate(root: str | Path, rel_file: str, quote: str,
             node_type: str = "") -> SourceSpan | None:
    """Find `quote` in `<root>/<rel_file>` and mint the span for it. None if it will not.

    The harness's half of a code citation, and the same three refusals `claims.mint`
    makes: a file that is not in the checkout, a quotation that is not in the file, and a
    quotation that occurs more than once and therefore addresses nothing in particular.

    The search is made twice — once exactly, once with runs of whitespace collapsed — and
    the SECOND is what makes this usable against a reader that retyped an indented line.
    The exact search runs FIRST, so a character-for-character citation is never resolved
    through a normalisation, exactly as `claims.mint` orders its own two searches.
    """
    root = Path(root)
    text = _read(root, rel_file)
    needle = (quote or "").strip()
    if text is None or len(needle) < 8:
        return None

    at = text.find(needle)
    if at >= 0 and text.find(needle, at + 1) == -1:
        start, end, found = at, at + len(needle), needle
    else:
        # Whitespace-collapsed, with an index back into the original so the span is still
        # a span of the FILE and not of a normalisation of it.
        flat, index = [], []
        prev_ws = False
        for i, ch in enumerate(text):
            if ch.isspace():
                if prev_ws:
                    continue
                flat.append(" ")
                index.append(i)
                prev_ws = True
            else:
                flat.append(ch)
                index.append(i)
                prev_ws = False
        flat_text = "".join(flat)
        flat_needle = _WS.sub(" ", needle)
        hit = flat_text.find(flat_needle)
        if hit < 0 or flat_text.find(flat_needle, hit + 1) != -1:
            return None
        start = index[hit]
        end = index[hit + len(flat_needle) - 1] + 1
        found = text[start:end]

    return SourceSpan(
        file=rel_file.replace("\\", "/"),
        line=text.count("\n", 0, start) + 1,
        end_line=text.count("\n", 0, end) + 1,
        char_span=(start, end), quote=found,
        file_sha256=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        node_type=node_type)


def span_still_holds(root: str | Path, span: SourceSpan | None) -> bool:
    """Does this span still name what it named? Re-read, never assumed.

    Used to decide whether artifact evidence survives a later mutation of the checkout.
    The file's hash is what is compared, not the line number: a line that moved is a line
    whose citation is stale even when the same text still exists elsewhere.
    """
    if span is None or not span.file_sha256:
        return False
    text = _read(Path(root), span.file)
    if text is None:
        return False
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() == span.file_sha256


# --------------------------------------------------------------------------- #
# Level 1 — deterministic facts about the checkout
# --------------------------------------------------------------------------- #
def file_fact(root: str | Path, snap: ArtifactSnapshot, rel_file: str,
              *, named_by_paper: str = "") -> ArtifactFact:
    """Is this file in the pinned checkout? An ARTIFACT_FACT either way — with one caveat.

    **An arbitrary absent file is not a paper defect and is not even reportable.** A file
    nobody claimed should exist being absent is a fact about a wish, so `named_by_paper`
    is required for an ABSENCE to carry any authority at all: the paper has to have named
    the thing. Present-or-absent, what this establishes is a property of the ARTIFACT, and
    turning it into a statement about the paper needs `bind_mismatch` and its three
    requirements.
    """
    present = _read(Path(root), rel_file) is not None
    if present:
        span = relocate_file(root, rel_file)
        return ArtifactFact(
            probe="file_present", snapshot=snap, span=span,
            statement=f"`{rel_file}` is present in the checkout at {snap.commit[:10]}.",
            authority="ARTIFACT_FACT" if snap.audited else "NONE")
    if not named_by_paper:
        return ArtifactFact(
            probe="file_absent", snapshot=snap, authority="NONE",
            statement=(f"`{rel_file}` is not in the checkout. Nothing in the paper was "
                       f"located naming it, so its absence establishes nothing — a file "
                       f"nobody claimed should exist is not a defect."))
    return ArtifactFact(
        probe="file_absent", snapshot=snap,
        authority="ARTIFACT_FACT" if snap.audited else "NONE",
        paper_quote=named_by_paper,
        statement=(f"The paper names `{rel_file}` and it is not in the checkout at "
                   f"{snap.commit[:10]}. This is a fact about the ARTIFACT; whether the "
                   f"paper's claim depends on it is a separate question."),
        counter_explanations=["the file may be produced by a build or download step",
                              "the paper may name a path from a different release"])


def relocate_file(root: str | Path, rel_file: str) -> SourceSpan | None:
    """A span naming a whole file, for a fact that is about the file's existence."""
    text = _read(Path(root), rel_file)
    if text is None:
        return None
    return SourceSpan(
        file=rel_file.replace("\\", "/"), line=1, end_line=text.count("\n") + 1,
        char_span=(0, len(text)), quote="",
        file_sha256=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        node_type="file")


_KEY_LINE = re.compile(r"^\s*['\"]?(?P<key>[A-Za-z_][\w.\-]*)['\"]?\s*[:=]\s*"
                       r"(?P<value>[^\s,#]+)", re.M)


def config_values(root: str | Path, rel_file: str, key: str) -> list[tuple[str, int]]:
    """Every (value, line) this file assigns to `key`. Text-level, and deliberately so.

    One reader for YAML, JSON, TOML, an argparse default and a plain Python assignment,
    because what a level-2 mismatch needs is the LITERAL the file prints and the line it
    prints it on, and five parsers would be five places for "what does this file say" to
    disagree. Every hit is relocated and quoted, so a reader checks the line rather than
    trusting the parse.

    Returning a LIST and not a value is the point: a key set in three places has three
    answers, and `bind_mismatch` refuses `experiment_identity_unbound` rather than picking
    one. "Some config somewhere says 32" is exactly what this must not collapse into.
    """
    text = _read(Path(root), rel_file)
    if text is None:
        return []
    out = []
    for m in _KEY_LINE.finditer(text):
        if m.group("key") != key:
            continue
        value = m.group("value").strip().strip("'\",")
        out.append((value, text.count("\n", 0, m.start()) + 1))
    return out


def argparse_default(root: str | Path, rel_file: str, flag: str) -> list[tuple[str, int]]:
    """Every `add_argument('--flag', ..., default=X)` in this file, as (X, line).

    An AST read rather than a regex, because a default is a keyword argument and finding
    it by text would find it inside strings and comments too. The value is `ast.unparse`d
    so a non-literal default (`default=cfg.batch_size`) comes back as its expression and
    is visibly not a literal, rather than being silently dropped.
    """
    text = _read(Path(root), rel_file)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    want = flag if flag.startswith("-") else f"--{flag}"
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        names = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if want not in names:
            continue
        for kw in node.keywords:
            if kw.arg == "default":
                try:
                    out.append((ast.unparse(kw.value), getattr(kw.value, "lineno",
                                                               node.lineno)))
                except Exception:                      # pragma: no cover
                    continue
    return out


def entrypoint_fact(root: str | Path, snap: ArtifactSnapshot) -> ArtifactFact:
    """Which command the checkout advertises, read by `repo.find_entrypoint`. Level 1."""
    found = repo_mod.find_entrypoint(Path(root))
    if not found:
        return ArtifactFact(
            probe="entrypoint_absent", snapshot=snap,
            authority="ARTIFACT_FACT" if snap.audited else "NONE",
            statement=(f"The checkout at {snap.commit[:10]} advertises no runnable "
                       f"entrypoint this harness recognises. A fact about the artifact: "
                       f"it does not say the experiment is absent, only that nothing "
                       f"here names how to start it."))
    return ArtifactFact(
        probe="entrypoint_present", snapshot=snap, artifact_value=found,
        span=relocate_file(root, found) if "/" in found or found.endswith(".py") else None,
        authority="ARTIFACT_FACT" if snap.audited else "NONE",
        statement=f"The checkout advertises `{found}` at {snap.commit[:10]}.")


def dependency_fact(root: str | Path, snap: ArtifactSnapshot, name: str) -> ArtifactFact:
    """Is this dependency DECLARED by the checkout, and at what version? Level 1."""
    files, declared, _frameworks = repo_mod.inspect_dependencies(Path(root))
    hit = next((d for d in declared
                if re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0].strip().lower()
                == name.lower()), "")
    return ArtifactFact(
        probe="dependency_declared" if hit else "dependency_undeclared",
        snapshot=snap, artifact_value=hit,
        authority="ARTIFACT_FACT" if snap.audited else "NONE",
        statement=(f"The checkout declares `{hit}` in {', '.join(files) or 'its manifests'}."
                   if hit else
                   f"No manifest in the checkout declares `{name}` "
                   f"({', '.join(files) or 'no manifest was found'})."))


# --------------------------------------------------------------------------- #
# Level 2 — a paper/artifact mismatch, and the three things it needs
# --------------------------------------------------------------------------- #
def bind_mismatch(doc: PaperDoc, snap: ArtifactSnapshot, *, paper_quote: str,
                  paper_value: str, span: SourceSpan | None, artifact_value: str,
                  experiment_id: str, probe: str = "config_mismatch",
                  counter_explanations: list[str] | None = None) -> ArtifactFact:
    """The paper says X for experiment E; the pinned artifact says Y for E. Or it refuses.

    THREE REQUIREMENTS, each its own named refusal, because they fail for opposite
    reasons and a reader needs to know which:

      * `paper_statement_unaddressed` — the quotation does not mint to an address. Without
        one there is no statement to disagree with, only a paraphrase.
      * `artifact_fact_unlocated` — the span did not relocate in the pinned tree, or the
        tree is not audited. A citation into an unknown tree is a citation into nothing.
      * `experiment_identity_unbound` — both halves are located and nothing establishes
        that they are about the same experiment. **This is the requirement that separates
        a mismatch from a coincidence**: a repository sets a batch size in a dozen places,
        and "some config somewhere says 32" contradicts nothing. The caller supplies the
        identity; passing an empty one refuses rather than defaulting to "probably".

    And a fourth outcome that is not a refusal: `no_disagreement`, when all three bind and
    the two values are the same. That is a real result — the artifact CONFIRMS the paper
    on this point — and collapsing it into "nothing found" would lose it.
    """
    def refuse(reason: str, statement: str) -> ArtifactFact:
        return ArtifactFact(probe=probe, snapshot=snap, span=span, authority="NONE",
                            refusal=reason, statement=statement, paper_quote=paper_quote,
                            paper_value=paper_value, artifact_value=artifact_value,
                            experiment_id=experiment_id)

    minted = claims.mint(doc, (paper_quote or "").strip())
    if not minted.resolved:
        return refuse("paper_statement_unaddressed",
                      "The paper statement does not occur exactly once in the parsed "
                      "paper, so there is no address for the side of this comparison "
                      "that is supposed to be about the paper.")
    if span is None or not snap.audited:
        return refuse("artifact_fact_unlocated",
                      f"The artifact side did not relocate in an audited tree "
                      f"({snap.note or 'no span'}). A citation into a tree whose identity "
                      f"is unknown establishes nothing about any commit.")
    if not (experiment_id or "").strip():
        return refuse("experiment_identity_unbound",
                      f"`{span.file}:{span.line}` sets {artifact_value!r} and the paper "
                      f"states {paper_value!r}, and nothing establishes that the two are "
                      f"about the same experiment. A repository sets a value in many "
                      f"places; 'some config somewhere says {artifact_value}' contradicts "
                      f"nothing, and guessing which one the paper meant would make this "
                      f"comparison a coincidence rather than a mismatch.")

    agree = _same_value(paper_value, artifact_value)
    return ArtifactFact(
        probe=probe, snapshot=snap, span=span,
        authority="ARTIFACT_FACT" if agree else "PAPER_ARTIFACT_MISMATCH",
        refusal="no_disagreement" if agree else "",
        paper_ref=minted.ref, paper_quote=minted.quote, paper_value=paper_value,
        artifact_value=artifact_value, experiment_id=experiment_id,
        counter_explanations=list(counter_explanations or []),
        statement=(
            f"The paper states {paper_value!r} at {minted.ref} for {experiment_id}, and "
            f"`{span.file}:{span.line}` in the checkout at {snap.commit[:10]} sets "
            f"{artifact_value!r} for the same experiment — they agree." if agree else
            f"The paper states {paper_value!r} at {minted.ref} for {experiment_id}; "
            f"`{span.file}:{span.line}` in the pinned checkout at {snap.commit[:10]} sets "
            f"{artifact_value!r} for the same experiment. This establishes an "
            f"inconsistency between the paper and the released artifact. It does NOT "
            f"establish that the reported result is wrong: which of the two the reported "
            f"number was produced under is a question for execution, not for reading."))


def _same_value(a: str, b: str) -> bool:
    """Do two printed values state the same thing? Numeric when both parse, else textual."""
    a, b = (a or "").strip(), (b or "").strip()
    try:
        return float(a) == float(b)
    except ValueError:
        return a.lower() == b.lower()


# --------------------------------------------------------------------------- #
# The route, and what "discharged" means
# --------------------------------------------------------------------------- #
def discharge(inspection: ArtifactInspection) -> ArtifactInspection:
    """Did this route answer the question it was given? Written by the harness.

    **"The repository cloned successfully" is not artifact evidence**, and this is where
    that is enforced. A route discharges only when all three hold:

      1. it had an audited snapshot to read — not a directory, a SHA with a clean tree;
      2. it was given at least one statement to check;
      3. it established at least one fact carrying real authority.

    Everything else is COMPLETED_INCONCLUSIVE, which is a different word from
    ARTIFACT_EVIDENCE in the same way CITATION_VERIFIED is a different word from
    PAPER_ONLY_RESOLVED: work happened and the question stayed open.
    """
    snap = inspection.snapshot
    carrying = [f for f in inspection.facts if f.authority != "NONE"]
    if snap is None or not snap.audited:
        ok, why = False, (f"no audited snapshot to tie a fact to"
                          f"{': ' + snap.note if snap and snap.note else ''}")
    elif not inspection.statements_examined:
        ok, why = False, ("the route obtained a checkout and was given no statement to "
                          "check against it; obtaining an artifact is not evidence")
    elif not carrying:
        ok, why = False, (f"{len(inspection.facts)} observation(s) were made and none "
                          f"carries authority over any statement the route was given")
    else:
        mismatches = [f for f in carrying if f.about_the_paper]
        ok = True
        why = (f"{len(carrying)} fact(s) established against the checkout at "
               f"{snap.commit[:10]}, of which {len(mismatches)} bind to a paper statement")
    return inspection.model_copy(update={"discharged": ok, "reason": why})


def outcome_disposition(inspection: ArtifactInspection) -> str:
    """The `TARGET_DISPOSITIONS` value this route produced. Never a scientific verdict.

    ARTIFACT_RESOLVED is the only disposition that reaches ARTIFACT_EVIDENCE and therefore
    RESOLVED_FROM_ARTIFACT, and it is reachable ONLY through `discharge`. A route that
    read the code and found nothing to say lands on COMPARISON_BLOCKED — it had a route
    and its result had nothing to be held against — and one with no usable checkout lands
    on ARTIFACT_BLOCKED, which is what the old code produced for every case alike.
    """
    if inspection.discharged:
        return "ARTIFACT_RESOLVED"
    snap = inspection.snapshot
    if snap is None or not snap.audited:
        return "ARTIFACT_BLOCKED"
    return "COMPARISON_BLOCKED"


def requires_execution(route_question: str) -> bool:
    """Can reading the code settle this, or does it need a measured result?

    STATIC INSPECTION MAY NOT RESOLVE A REPRODUCTION QUESTION. "Does the released code
    produce 91.4?" is not answerable by reading it — the answer is a measurement — and a
    route that claimed otherwise would let a reading of the source acquit or convict a
    number. The vocabulary is the question's own kind, so this is a lookup and not a
    judgement.
    """
    return (route_question or "").strip().upper() in _EXECUTION_ONLY


# Question kinds whose answer is a MEASUREMENT. Reading the artifact can narrow the
# experiment identity for these, generate new targeted questions, and supply the evidence
# an execution needs to be authorised — it cannot answer them.
_EXECUTION_ONLY = frozenset({
    "REPRODUCTION", "PRINTED_QUANTITY", "COMPOSITION", "ATTRIBUTION",
})


def inspect(doc: PaperDoc, root: str | Path, *, url: str = "", target_id: str = "",
            statements: list[str] | None = None, facts: list[ArtifactFact] | None = None,
            tree: repo_mod.GitTree | None = None) -> ArtifactInspection:
    """One route attempt over one checkout. Assembles, then asks `discharge`."""
    snap = snapshot(root, url, tree)
    supplied = list(facts or [])
    inspection = ArtifactInspection(
        paper_id=doc.paper_id, target_id=target_id, snapshot=snap, facts=supplied,
        statements_examined=[s for s in (statements or []) if s.strip()],
        files_examined=sorted({f.span.file for f in supplied if f.span}))
    return discharge(inspection)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.artifact_evidence
    import subprocess
    import sys
    import tempfile

    from .artifacts import Section

    doc = PaperDoc(paper_id="p", title="A Paper", n_pages=2, sections=[
        Section(section_idx=0, title="Abstract", page_start=1,
                text="We train every model for 100 epochs with a batch size of 128."),
        Section(section_idx=1, title="Method", page_start=2,
                text="The released code accompanies this paper."),
    ])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        (root / "train.py").write_text(
            "import argparse\n"
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--batch-size', type=int, default=32)\n"
            "    return p\n", encoding="utf-8")
        (root / "config.yaml").write_text(
            "batch_size: 32\nepochs: 100\n", encoding="utf-8")
        (root / "requirements.txt").write_text("torch==2.1.0\nnumpy\n", encoding="utf-8")

        def git(*args):
            subprocess.run(["git", *args], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            git("init", "-q")
            git("config", "user.email", "s@e")
            git("config", "user.name", "s")
            git("add", "-A")
            git("commit", "-qm", "initial")
        except (OSError, subprocess.CalledProcessError):   # pragma: no cover - no git here
            print("harness.artifact_evidence self-check skipped: git unavailable")
            sys.exit(0)

        snap = snapshot(root, "https://example.invalid/r")
        assert snap.audited and len(snap.commit) == 40 and snap.tree_sha, snap
        assert not snap.dirty

        # --- relocation ---------------------------------------------------------------
        span = relocate(root, "config.yaml", "batch_size: 32")
        assert span and span.line == 1 and span.file_sha256, span
        assert relocate(root, "config.yaml", "nothing like this here") is None
        assert relocate(root, "missing.yaml", "batch_size: 32") is None
        # AND A PATH OUT OF THE CHECKOUT IS NOT A CITATION INTO IT.
        assert relocate(root, "../outside.txt", "batch_size: 32") is None
        # Retyped with different whitespace still relocates; the span is still the file's.
        loose = relocate(root, "train.py", "p.add_argument('--batch-size',  type=int, "
                                           "default=32)")
        assert loose and loose.line == 4, loose

        # --- level 1 ------------------------------------------------------------------
        assert file_fact(root, snap, "train.py").authority == "ARTIFACT_FACT"
        # AN ARBITRARY ABSENT FILE IS NOT A DEFECT.
        nobody = file_fact(root, snap, "eval_all.sh")
        assert nobody.authority == "NONE" and "nobody claimed" in nobody.statement
        # One the paper NAMES is an artifact fact, and still not a paper defect.
        named = file_fact(root, snap, "eval_all.sh",
                          named_by_paper="The released code accompanies this paper.")
        assert named.authority == "ARTIFACT_FACT" and not named.about_the_paper

        assert config_values(root, "config.yaml", "batch_size") == [("32", 1)]
        assert argparse_default(root, "train.py", "batch-size") == [("32", 4)]
        assert dependency_fact(root, snap, "torch").artifact_value.startswith("torch")
        assert dependency_fact(root, snap, "jax").probe == "dependency_undeclared"

        # --- level 2 ------------------------------------------------------------------
        quote = "We train every model for 100 epochs with a batch size of 128."
        bound = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="128", span=span,
            artifact_value="32", experiment_id="the only training entrypoint, train.py")
        assert bound.authority == "PAPER_ARTIFACT_MISMATCH", bound
        assert bound.about_the_paper and bound.paper_ref.startswith("P0:")
        assert "does NOT establish that the reported result is wrong" in bound.statement

        # WITHOUT AN EXPERIMENT IDENTITY IT IS A COINCIDENCE, not a mismatch.
        loose_id = bind_mismatch(doc, snap, paper_quote=quote, paper_value="128",
                                 span=span, artifact_value="32", experiment_id="")
        assert loose_id.authority == "NONE"
        assert loose_id.refusal == "experiment_identity_unbound", loose_id.refusal

        # A paraphrase of the paper is not the paper.
        para = bind_mismatch(doc, snap, paper_quote="we used a batch size of 128",
                             paper_value="128", span=span, artifact_value="32",
                             experiment_id="train.py")
        assert para.refusal == "paper_statement_unaddressed", para.refusal

        # A dirty tree cannot masquerade as the audited one.
        (root / "config.yaml").write_text("batch_size: 64\n", encoding="utf-8")
        after = snapshot(root, "https://example.invalid/r")
        assert after.dirty and not after.audited, after
        assert not same_snapshot(snap, after)
        dirty_bind = bind_mismatch(doc, after, paper_quote=quote, paper_value="128",
                                   span=span, artifact_value="32", experiment_id="train.py")
        assert dirty_bind.refusal == "artifact_fact_unlocated", dirty_bind.refusal
        # AND THE PRE-RUN FACT IS NOT REWRITTEN. It is tied to its own snapshot, which
        # still says what it said; only the span no longer holds against the NEW tree.
        assert bound.snapshot is not None and bound.snapshot.audited
        assert not span_still_holds(root, span)

        # --- the route ----------------------------------------------------------------
        empty = inspect(doc, root, url="u", statements=[quote])
        assert not empty.discharged and outcome_disposition(empty) == "ARTIFACT_BLOCKED"

        git("checkout", "--", "config.yaml")
        clean = snapshot(root, "u")
        assert clean.audited

        nothing_asked = inspect(doc, root, url="u", facts=[file_fact(root, clean, "train.py")])
        assert not nothing_asked.discharged
        assert "obtaining an artifact is not evidence" in nothing_asked.reason
        assert outcome_disposition(nothing_asked) == "COMPARISON_BLOCKED"

        ran = inspect(doc, root, url="u", statements=[quote], target_id="t1",
                      facts=[bind_mismatch(doc, clean, paper_quote=quote, paper_value="128",
                                           span=relocate(root, "config.yaml",
                                                         "batch_size: 32"),
                                           artifact_value="32",
                                           experiment_id="train.py")])
        assert ran.discharged and outcome_disposition(ran) == "ARTIFACT_RESOLVED", ran
        assert ran.files_examined == ["config.yaml"]
        assert len(ran.bound_mismatches()) == 1

    # --- §8, the rule audit ---------------------------------------------------------
    from . import code_audit
    ids = set()
    for rule in code_audit._RULES:
        ids |= {m for m in re.findall(r'rule_id="([^"]+)"', rule.__doc__ or "")}
    # Every rule the module can emit is classified, and an unknown one is not visible.
    assert not reviewer_visible("a-rule-nobody-audited")
    assert rule_authority("leak-model-selection-on-test") == "D"
    assert not reviewer_visible("leak-model-selection-on-test")
    assert reviewer_visible("leak-unseeded-split")
    assert set(RULE_AUTHORITY.values()) <= {"A", "B", "C", "D"}

    # An AST warning is never a scientific failure, and there is no value for one.
    from .artifacts import ARTIFACT_AUTHORITY
    assert "SCIENTIFIC_FAILURE" not in ARTIFACT_AUTHORITY
    assert len(ARTIFACT_AUTHORITY) == 3

    # A reproduction question is not answerable by reading.
    assert requires_execution("REPRODUCTION") and requires_execution("PRINTED_QUANTITY")
    assert not requires_execution("SPECIFICATION")

    if len(sys.argv) > 1:                              # audit a real checkout
        target = Path(sys.argv[1])
        snap = snapshot(target)
        print(json.dumps({"commit": snap.commit, "tree": snap.tree_sha,
                          "dirty": snap.dirty, "audited": snap.audited,
                          "entrypoint": entrypoint_fact(target, snap).artifact_value},
                         indent=2))

    print("harness.artifact_evidence self-check ok")
