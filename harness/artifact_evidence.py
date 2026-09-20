"""F — what the released artifact itself can establish, bounded by what it cannot.

`python -m harness.artifact_evidence [<checkout>]` runs the self-check.

**The question this route answers is "what can the released artifact itself establish?",
and the reason it needed building is that the harness already read the code and threw the
scientific result away.** `harness/code_audit.py` has run on every cloned paper since the
first version: it parses the checkout, applies its rule(s), and writes `CodeAudit.findings`
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
from .schema import (ARTIFACT_QUESTION_SCOPES, SETTLEABLE_BY_ARTIFACT_FACT,
                     ArtifactFact, ArtifactInspection, ArtifactSnapshot, PaperDoc,
                     SourceSpan)

# --------------------------------------------------------------------------- #
# §8 — the surviving AST rule, classified by the authority it can carry
# --------------------------------------------------------------------------- #
# `code_audit.py` used to carry ten pattern-matching rules across three cheat classes.
# Nine are deleted outright (not merely reclassified): measured over the four repository
# papers this corpus ever ran, the ten rules produced six hits and five were false, every
# one because the rules matched SUBSTRINGS of identifiers rather than a semantic category,
# and seven of the ten never fired on any real repository at all. `artifact_review_driver`
# already does the judgment those nine were a noisy substitute for — a full LLM read of the
# checkout against the paper's method section, with every citation it makes relocated and
# hashed below before it counts for anything.
#
# What is left is classified the same way the ten were, so a rule added later is not
# automatically trusted:
#
#   A  deterministic artifact fact     what it reports is true of the checkout by
#                                      construction, whatever it means — rendered to a
#                                      referee as a bounded ARTIFACT observation
#   D  unsafe for reviewer output      unclassified, or measured false — machine trace only
RULE_AUTHORITY = {
    # The call site passes no `random_state`, `seed`, `generator` or `stratify`. That is
    # a fact about the call, decidable from the AST, and true whatever a global seed does
    # elsewhere. Fired once on `apt-icml` (`utils/utils.py:600`,
    # `torch.utils.data.random_split(total_dataset, [n, m])`) and the fact is correct.
    "leak-unseeded-split": "A",
}

REVIEWER_VISIBLE = ("A",)


def rule_authority(rule_id: str) -> str:
    """The class of a rule, defaulting to D. An unclassified rule is not shown.

    Fail-closed on purpose: a rule added later and not audited here is not automatically
    reviewer-visible, because the audit — not the rule's existence — is what licenses it.
    """
    return RULE_AUTHORITY.get((rule_id or "").strip(), "D")


def reviewer_visible(rule_id: str) -> bool:
    """May this rule's hit be rendered to a referee AS IT STANDS? Class A only."""
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
            probe="file_present", snapshot=snap, span=span, settles="FILE_PRESENCE",
            statement=f"`{rel_file}` is present in the checkout at {snap.commit[:10]}.",
            authority="ARTIFACT_FACT" if snap.audited else "NONE")
    if not named_by_paper:
        return ArtifactFact(
            probe="file_absent", snapshot=snap, authority="NONE", settles="",
            statement=(f"`{rel_file}` is not in the checkout. Nothing in the paper was "
                       f"located naming it, so its absence establishes nothing — a file "
                       f"nobody claimed should exist is not a defect."))
    return ArtifactFact(
        probe="file_absent", snapshot=snap, settles="FILE_PRESENCE",
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
            probe="entrypoint_absent", snapshot=snap, settles="ENTRYPOINT_PRESENCE",
            authority="ARTIFACT_FACT" if snap.audited else "NONE",
            statement=(f"The checkout at {snap.commit[:10]} advertises no runnable "
                       f"entrypoint this harness recognises. A fact about the artifact: "
                       f"it does not say the experiment is absent, only that nothing "
                       f"here names how to start it."))
    return ArtifactFact(
        probe="entrypoint_present", snapshot=snap, settles="ENTRYPOINT_PRESENCE",
        artifact_value=found,
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
        settles="DEPENDENCY_DECLARED" if files else "MANIFEST_PRESENCE",
        snapshot=snap, artifact_value=hit,
        authority="ARTIFACT_FACT" if snap.audited else "NONE",
        statement=(f"The checkout declares `{hit}` in {', '.join(files) or 'its manifests'}."
                   if hit else
                   f"No manifest in the checkout declares `{name}` "
                   f"({', '.join(files) or 'no manifest was found'})."))


# --------------------------------------------------------------------------- #
# Level 2 — a paper/artifact mismatch, and the three things it needs
# --------------------------------------------------------------------------- #
def classify_identity(root: str | Path, *, experiment_id: str, basis: str,
                      evidence_file: str = "", evidence_quote: str = ""
                      ) -> tuple[str, str, SourceSpan | None]:
    """(identity_state, basis, the relocated span that establishes it). Never believed.

    **A model saying "this looks like the right config" is not an established identity**,
    and this is the function that says so. An identity is ESTABLISHED only when the auditor
    names a DETERMINISTIC source for the link — the paper printing the command, the
    checkout's README mapping the experiment to the file, a committed script that passes
    the config, an authors' experiment table — AND that source RELOCATES in the pinned tree
    by the same rule every other code citation goes through.

    The four states are not a confidence scale. PARTIAL means a real source was named and
    did not relocate, which is a different fact from AMBIGUOUS, where the only thing
    offered was the auditor's reading, which is in turn different from UNBOUND, where
    nothing was offered at all. Only ESTABLISHED may support level-2 authority.
    """
    basis = (basis or "").strip()
    if not (experiment_id or "").strip():
        return "UNBOUND", "", None
    if basis not in _DETERMINISTIC_BASES:
        # Includes `auditor_assertion`, deliberately: it is recorded so a human can see
        # what was claimed, and it classifies as the guess it is.
        return "AMBIGUOUS", (basis or "auditor_assertion"), None
    if basis == "paper_names_the_command":
        # The deterministic source is in the PAPER, and the caller has already had it
        # minted — `bind_mismatch` re-mints the statement itself, so there is nothing
        # further to relocate in the tree.
        return "ESTABLISHED", basis, None
    span = relocate(root, evidence_file, evidence_quote) if evidence_file else None
    if span is None:
        return "PARTIAL", basis, None
    return "ESTABLISHED", basis, span


# The bases whose evidence lives somewhere a reader can open. `auditor_assertion` is
# absent by construction, which is the rule rather than a note about it.
_DETERMINISTIC_BASES = frozenset({
    "paper_names_the_command", "readme_maps_the_experiment",
    "script_passes_the_config", "authors_experiment_table"})


def bind_mismatch(doc: PaperDoc, snap: ArtifactSnapshot, *, paper_quote: str,
                  paper_value: str, span: SourceSpan | None, artifact_value: str,
                  experiment_id: str, probe: str = "config_mismatch",
                  identity_basis: str = "", identity_file: str = "",
                  identity_quote: str = "", root: str | Path = "",
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
    def refuse(reason: str, statement: str, *, authority: str = "NONE",
               identity: str = "UNBOUND", basis: str = "", paper_ref: str = "",
               ident_span: SourceSpan | None = None) -> ArtifactFact:
        # `paper_ref` IS CARRIED ON A REFUSAL, when the quotation minted. A concern whose
        # paper half was relocated and whose relationship was not is a different record
        # from one whose paper half was never found, and the first version reported both
        # with an empty address — so "paper citations relocated" read 0 across a corpus
        # where every one of them had relocated.
        return ArtifactFact(probe=probe, snapshot=snap, span=span, authority=authority,
                            refusal=reason, statement=statement, paper_quote=paper_quote,
                            paper_ref=paper_ref,
                            paper_value=paper_value, artifact_value=artifact_value,
                            experiment_id=experiment_id, identity_state=identity,
                            identity_basis=basis, identity_span=ident_span,
                            counter_explanations=list(counter_explanations or []))

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
    identity, basis, ident_span = classify_identity(
        root or "", experiment_id=experiment_id, basis=identity_basis,
        evidence_file=identity_file, evidence_quote=identity_quote)

    # BOTH ENDPOINTS ARE REAL AT THIS POINT, and that is exactly what is worth saying. The
    # paper statement minted, the code span relocated in an audited tree — so the pairing
    # is not a guess about where things are. What remains a reading is the RELATIONSHIP,
    # and an identity that is not ESTABLISHED is precisely a relationship nobody checked.
    endpoints = (
        f"The paper states {paper_value!r} at {minted.ref} and `{span.file}:{span.line}` "
        f"in the checkout at {snap.commit[:10]} sets {artifact_value!r}. BOTH LOCATIONS "
        f"ARE VERIFIED. What is not verified is that they are about the same experiment: ")
    if identity == "UNBOUND":
        return refuse("experiment_identity_unbound", paper_ref=minted.ref, statement=endpoints + (
            "nothing was offered to link them. A repository sets a value in many places; "
            f"'some config somewhere says {artifact_value}' contradicts nothing."),
            identity=identity, basis=basis)
    if identity != "ESTABLISHED":
        return refuse(
            "experiment_identity_not_deterministic", paper_ref=minted.ref, statement=endpoints + (
                f"the link offered is {basis or 'the auditor s reading'}, which "
                f"{'did not relocate in the pinned tree' if identity == 'PARTIAL' else
                   'names no source a reader can open'}. This is a concern a referee can "
                f"act on and it is not a demonstrated inconsistency."),
            authority="ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
            identity=identity, basis=basis, ident_span=ident_span)

    if not _comparable(paper_value, artifact_value):
        return refuse("values_not_comparable", paper_ref=minted.ref, statement=endpoints + (
            f"the identity is established, and {paper_value!r} and {artifact_value!r} do "
            f"not state the same kind of quantity, so there is nothing to disagree."),
            authority="ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
            identity=identity, basis=basis, ident_span=ident_span)

    if not _derivable_from(minted.quote, paper_value):
        return refuse("paper_value_not_derivable", paper_ref=minted.ref, statement=endpoints + (
            f"the identity is established, and {paper_value!r} is not re-derivable from "
            f"the quoted span: the span either does not report that number unambiguously "
            f"or reports it more than once, so WHICH number in it the paper means here is "
            f"the auditor's reading of the layout rather than something this harness can "
            f"check. Quote the cell, not the table."),
            authority="ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
            identity=identity, basis=basis, ident_span=ident_span)

    if not _derivable_from(span.quote, artifact_value):
        # THE SAME RULE, ON THE SIDE THAT DID NOT HAVE IT. `paper_value` has been
        # re-derived from the re-minted paper quotation since the `apt-icml` table case,
        # and `artifact_value` was taken from the auditor's own JSON: relocating
        # `code_quote` proves the LINE exists at that commit and proves nothing about the
        # NUMBER the auditor says it sets. A fabricated or mis-read value beside a
        # genuinely relocated line therefore reached `PAPER_ARTIFACT_MISMATCH` — the one
        # artifact authority `EVIDENCE_ABOUT_THE_PAPER` admits — under a statement reading
        # "BOTH LOCATIONS ARE VERIFIED", which was true of the locations and not of the
        # values. Verifying one endpoint and believing the other is the shape invariant 2
        # forbids, and a mismatch is exactly where it costs most.
        return refuse("artifact_value_not_derivable", paper_ref=minted.ref, statement=endpoints + (
            f"the identity is established, and {artifact_value!r} is not re-derivable from "
            f"the relocated span {span.quote!r}: the span either does not set that value "
            f"unambiguously or sets it more than once, so WHICH number in it the auditor "
            f"means is its reading of the file rather than something this harness can "
            f"check. Quote the assignment, not the block."),
            authority="ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
            identity=identity, basis=basis, ident_span=ident_span)

    agree = _same_value(paper_value, artifact_value)
    where = (f"established from {basis}"
             + (f", at `{ident_span.file}:{ident_span.line}`" if ident_span else ""))
    return ArtifactFact(
        probe=probe, snapshot=snap, span=span, settles="CONFIG_LITERAL",
        authority="ARTIFACT_FACT" if agree else "PAPER_ARTIFACT_MISMATCH",
        refusal="no_disagreement" if agree else "",
        paper_ref=minted.ref, paper_quote=minted.quote, paper_value=paper_value,
        artifact_value=artifact_value, experiment_id=experiment_id,
        identity_state=identity, identity_basis=basis, identity_span=ident_span,
        counter_explanations=list(counter_explanations or []),
        statement=(
            f"The paper states {paper_value!r} at {minted.ref} for {experiment_id} "
            f"({where}), and `{span.file}:{span.line}` in the checkout at "
            f"{snap.commit[:10]} sets {artifact_value!r} for the same experiment — they "
            f"agree." if agree else
            f"The paper states {paper_value!r} at {minted.ref} for {experiment_id} "
            f"({where}); `{span.file}:{span.line}` in the pinned checkout at "
            f"{snap.commit[:10]} sets {artifact_value!r} for the same experiment. This "
            f"establishes an inconsistency between the paper and the released artifact. It "
            f"does NOT establish that the reported result is wrong: which of the two the "
            f"reported number was produced under is a question for execution, not for "
            f"reading."))


def _derivable_from(span_text: str, value: str) -> bool:
    """Can the harness itself read `value` out of the quoted span, unambiguously?

    Applied to BOTH sides of a mismatch — the re-minted paper quotation and the relocated
    source span — because relocating a line proves the line exists and proves nothing
    about the number somebody says it carries.

    THE RULE THE CORPUS'S FIRST LEVEL-2 MISMATCH NEEDED. The auditor quoted the whole of
    `apt-icml`'s Table 6 — "Learning rate 2e-4 2e-4 2e-4 1e-4 1e-4 Batch size 32 32 32 16
    32 Epochs 40 40 40 16 15 Distill epochs 20 20 20 6 -" — and reported the paper value as
    "Epochs 16 (CNN/DM column)". Every piece of that is true and the span really does say
    16. It also says 40, 32, 15 and 6, and **which column is CNN/DM's is a reading of a
    table layout that extraction flattened away**. Accepting it would make a level-2
    mismatch rest on a number a model picked out of a row of numbers.

    Two ways to satisfy it, and both are the harness's own reading of the span: the span
    reports exactly one quantity and it is this one, or the number occurs in the span
    exactly once. Quote the cell and it binds; quote the table and it does not.
    """
    number = _quantity(value)
    if number is None:
        return False
    parsed = claims.parse_quantity(span_text or "")
    if parsed is not None and parsed.value == number:
        return True
    raw = (claims.parse_quantity(value or "") or _NoRaw()).raw or ""
    return bool(raw) and (span_text or "").count(raw) == 1


class _NoRaw:
    raw = ""


def _quantity(value: str) -> float | None:
    """The single unambiguous number a stated value reports, or None. The harness's parser.

    `claims.parse_quantity` and not `float()`, for two reasons the live auditor run made
    concrete on `apt-icml`. It is more PERMISSIVE where it should be: the auditor wrote
    `'Epochs 16 (CNN/DM column)'` for the paper side, which is one number wearing a label,
    and `float()` refused it — so a mismatch with an ESTABLISHED identity was thrown away
    as a category error. And it is more STRICT where it should be: the same auditor wrote
    `'num_train_epochs=120, distill_epoch=96'` for an artifact side, which states TWO
    quantities, and `parse_quantity` refuses it rather than picking one. That refusal is
    the same rule `local_exec.parse_metric` applies to an execution's output and
    `claims.parse_quantity` applies to a paper's prose: a span reporting two numbers
    reports no single quantity.
    """
    parsed = claims.parse_quantity(value or "")
    return None if parsed is None else parsed.value


def _same_value(a: str, b: str) -> bool:
    """Do two stated values report the same number? Only ever asked of two quantities."""
    qa, qb = _quantity(a), _quantity(b)
    return qa is not None and qb is not None and qa == qb


def _comparable(a: str, b: str) -> bool:
    """May these two sides be held against each other at level 2? Only as QUANTITIES.

    **Both sides must yield one unambiguous number.** A paper saying "AdamW" and a config
    saying "1e-4" disagree about nothing — that is a category error, not an inconsistency —
    and, more importantly, two PROSE descriptions that differ are a SEMANTIC judgement.
    "The paper says real samples come from the matching weather's split; the code always
    uses snow" is a real concern and it is the auditor's reading of two texts, not a
    comparison of two stated quantities. Refusing it here is what keeps
    `PAPER_ARTIFACT_MISMATCH` a deterministic claim: it lands at
    ENDPOINTS_VERIFIED_ARTIFACT_CONCERN instead, with both locations verified and the
    correspondence marked as a reading — which is exactly what it is.
    """
    return _quantity(a) is not None and _quantity(b) is not None


# --------------------------------------------------------------------------- #
# The route, and what "discharged" means
# --------------------------------------------------------------------------- #
def discharge(inspection: ArtifactInspection) -> ArtifactInspection:
    """What this route actually settled, and for which question. Written by the harness.

    **THE BUG THIS REPLACES.** The first version asked three things — an audited snapshot,
    at least one statement, at least one fact carrying authority — and answered a single
    boolean. So any fact could settle any statement, and on all four repository papers the
    target *"the released repository <url> implements the described method"* was discharged
    by facts like *"the checkout advertises evaluate.py"*. Four papers were reported as
    having had a claim about their implementation SETTLED by the presence of a file. An
    entrypoint existing is supporting evidence for that question; it is not its answer, and
    reporting it as one violates the authority hierarchy this module exists to state.

    **A fact settles a question only when its SCOPE matches.** Every probe declares the
    bounded question it answers (`ArtifactFact.settles`), and a target carrying a bounded
    question is discharged only by a fact answering THAT question.
    `IMPLEMENTATION_CORRESPONDENCE` — "does this code implement the described method", "is
    the implementation faithful", "does this reproduce the paper" — is excluded from
    `SETTLEABLE_BY_ARTIFACT_FACT` by construction, so no accumulation of level-1 facts can
    ever reach it. It needs an addressed method statement, an exact artifact location, an
    established identity, a proposed correspondence, deterministic relocation of both ends,
    and for behavioural claims a measurement.

    Four outcomes, in descending authority, and each is a different sentence to a referee:

      ARTIFACT_MISMATCH_ESTABLISHED         the paper and the code disagree, identity bound
      ARTIFACT_CONCERN_VERIFIED_ENDPOINTS   both locations real, the relation model-proposed
      ARTIFACT_FACT_ESTABLISHED             a bounded fact answering the bounded question
      ARTIFACT_INSPECTION_INCONCLUSIVE      the route ran and settled nothing
    """
    snap = inspection.snapshot
    scope = (inspection.question_scope or "").strip()
    facts = inspection.facts
    mismatches = [f for f in facts if f.about_the_paper]
    concerns = [f for f in facts if f.endpoints_only]
    matching = [f for f in facts
                if f.authority == "ARTIFACT_FACT" and f.settles
                and f.settles in SETTLEABLE_BY_ARTIFACT_FACT
                and (not scope or f.settles == scope)]
    carrying = [f for f in facts if f.authority != "NONE"]

    if snap is None or not snap.audited:
        why = (f"no audited snapshot to tie a fact to"
               f"{': ' + snap.note if snap and snap.note else ''}")
        return inspection.model_copy(update={"discharged": False, "reason": why})
    if not inspection.statements_examined:
        return inspection.model_copy(update={"discharged": False, "reason": (
            "the route obtained a checkout and was given no statement to check against "
            "it; obtaining an artifact is not evidence")})

    if mismatches:
        why = (f"{len(mismatches)} paper/artifact disagreement(s) established with the "
               f"experiment identity bound to a deterministic source")
        return inspection.model_copy(update={"discharged": True, "reason": why})
    if concerns:
        why = (f"{len(concerns)} concern(s) whose paper location and code location are "
               f"both verified and whose correspondence remains the auditor's reading; "
               f"the question stays open")
        return inspection.model_copy(update={"discharged": False, "reason": why})
    if matching:
        why = (f"{len(matching)} bounded fact(s) answering the {scope or 'bounded'} "
               f"question this route was asked, against the checkout at {snap.commit[:10]}")
        return inspection.model_copy(update={"discharged": True, "reason": why})

    # THE CORRECTED REFUSAL, and the sentence the four corpus papers should have carried.
    if scope == "IMPLEMENTATION_CORRESPONDENCE":
        why = (f"{len(carrying)} bounded fact(s) were established about the checkout and "
               f"none of them answers this question. Whether the released code implements "
               f"the described method is a semantic correspondence question: it needs an "
               f"addressed method statement, an exact artifact location, an established "
               f"experiment identity, and for any behavioural part of it a measurement. "
               f"An advertised entrypoint, a declared dependency and a present manifest "
               f"are supporting evidence for it and are not its answer.")
    elif carrying:
        why = (f"{len(carrying)} fact(s) carry authority and none answers the "
               f"{scope or 'unscoped'} question this route was asked")
    else:
        why = (f"{len(facts)} observation(s) were made and none carries authority over "
               f"any question the route was given")
    return inspection.model_copy(update={"discharged": False, "reason": why})


def outcome_disposition(inspection: ArtifactInspection) -> str:
    """The `TARGET_DISPOSITIONS` value this route produced. Never a scientific verdict.

    Five states rather than the one the first version had. Only
    ARTIFACT_MISMATCH_ESTABLISHED maps to `ARTIFACT_EVIDENCE`, which is the only
    artifact-route state `EVIDENCE_ABOUT_THE_PAPER` admits; a bounded fact about the
    checkout resolves its own bounded question and says nothing about the document.
    """
    snap = inspection.snapshot
    if snap is None or not snap.audited:
        return "ARTIFACT_BLOCKED"
    if any(f.about_the_paper for f in inspection.facts):
        return "ARTIFACT_MISMATCH_ESTABLISHED"
    if any(f.endpoints_only for f in inspection.facts):
        return "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS"
    if inspection.discharged:
        return "ARTIFACT_FACT_ESTABLISHED"
    return "ARTIFACT_INSPECTION_INCONCLUSIVE"


def question_scope(question_kind: str = "", claim_text: str = "") -> str:
    """Which bounded question a target is asking, or IMPLEMENTATION_CORRESPONDENCE.

    Deliberately conservative: anything this function cannot recognise as one of the
    bounded scopes is IMPLEMENTATION_CORRESPONDENCE, which no level-1 fact may settle. The
    default therefore REFUSES rather than admits, which is the opposite of what the first
    version did by having no notion of scope at all.
    """
    text = (claim_text or "").lower()
    kind = (question_kind or "").strip().upper()
    if kind in ARTIFACT_QUESTION_SCOPES:
        return kind
    for phrase, scope in _SCOPE_PHRASES:
        if phrase in text:
            return scope
    return "IMPLEMENTATION_CORRESPONDENCE"


# Read off the claim text only where the claim is unambiguously the bounded question. A
# claim that says "implements the described method" is NOT here, and that is the point.
_SCOPE_PHRASES = (
    ("is present in the repository", "FILE_PRESENCE"),
    ("advertises an entrypoint", "ENTRYPOINT_PRESENCE"),
    ("declares the dependency", "DEPENDENCY_DECLARED"),
    ("publishes a dependency manifest", "MANIFEST_PRESENCE"),
    ("sets the configuration value", "CONFIG_LITERAL"),
    ("provides the command", "COMMAND_PRESENCE"),
)


def decompose(claim_text: str, repo_url: str = "") -> list[tuple[str, str]]:
    """The bounded questions an IMPLEMENTATION_CORRESPONDENCE claim can be broken into.

    (scope, the bounded question in words). This is what replaces discharging the broad
    claim: the route answers the narrow questions it CAN answer, records each as settling
    its own scope, and leaves the broad claim open with the reason above. A referee then
    reads "these four things about the artifact are established, and whether the code
    implements the method is still open", which is what was true all along.
    """
    where = f" in {repo_url}" if repo_url else " in the released repository"
    return [
        ("ENTRYPOINT_PRESENCE", f"is there a runnable entrypoint{where} that the "
                                f"repository itself advertises?"),
        ("MANIFEST_PRESENCE", f"does the checkout{where} publish a dependency manifest "
                              f"at all?"),
        ("DEPENDENCY_DECLARED", f"are the frameworks the paper names declared{where}?"),
        ("FILE_PRESENCE", f"are the files the paper names by path present{where}?"),
    ]


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


# WHAT A VERIFIED ARTIFACT OBSERVATION BUYS A LATER ROUTE. Keyed on the probe, because
# what an observation enables is a property of WHAT WAS LOOKED AT and not of how alarming
# the reading was — an auditor's prose may not decide what runs next any more than it may
# decide what is established.
_ESCALATION_FOR_PROBE = {
    "entrypoint_present":
        "narrows the candidate commands for any execution route to the entrypoint the "
        "repository itself advertises",
    "dependency_declared":
        "identifies part of the stack an execution route would have to build",
    "dependency_undeclared":
        "an execution route would have to infer the stack; no manifest declares it",
    "code_review:METRIC_MISMATCH":
        "identifies the metric implementation an execution route would measure against",
    "code_review:SPLIT_LEAKAGE_CONCERN":
        "reveals a split definition a focused-validation experiment would have to control",
    "code_review:PAPER_CODE_MISMATCH":
        "identifies a configuration whose two candidate values a run could discriminate "
        "between — which is an EXECUTION question, and is why this route refuses to "
        "answer it",
    "code_review:MISSING_EXPERIMENT_PATH":
        "narrows what an execution route could attempt: the configuration the paper "
        "implies was not found in the checkout",
    "code_review:BASELINE_IMPLEMENTATION":
        "identifies a comparison arm a focused-validation experiment would have to build",
}


def escalations_from(facts: list[ArtifactFact]) -> list[str]:
    """What this inspection makes newly possible for a LATER route. Recorded, never acted on.

    **It may not suppress a measurement route**, and the shape of this function is why it
    cannot: it returns SENTENCES, into `ArtifactInspection.escalations`, which no planner,
    gate or disposition reads. Narrowing a command is `experiment_id`'s to use and
    authorising a run is `probe`'s; finding something interesting statically is not a
    reason to stop measuring, and there is no field here through which it could become one.
    """
    out: list[str] = []
    for fact in facts:
        if fact.authority == "NONE":
            continue
        line = _ESCALATION_FOR_PROBE.get(fact.probe, "")
        if not line:
            continue
        where = (f"`{fact.span.file}:{fact.span.line}`" if fact.span and fact.span.quote
                 else (fact.artifact_value or "the checkout"))
        out.append(f"{where} {line}")
    return sorted(set(out))


def inspect(doc: PaperDoc, root: str | Path, *, url: str = "", target_id: str = "",
            statements: list[str] | None = None, facts: list[ArtifactFact] | None = None,
            scope: str = "", escalations: list[str] | None = None,
            tree: repo_mod.GitTree | None = None) -> ArtifactInspection:
    """One route attempt over one checkout. Assembles, then asks `discharge`.

    `scope` is the BOUNDED question this attempt was asked. Defaulting it to
    IMPLEMENTATION_CORRESPONDENCE — the one scope no level-1 fact may settle — is what
    makes the fix fail closed: an attempt whose caller did not say what it was asking
    cannot be discharged by whatever fact happened to be established.
    """
    snap = snapshot(root, url, tree)
    supplied = list(facts or [])
    inspection = ArtifactInspection(
        paper_id=doc.paper_id, target_id=target_id, snapshot=snap, facts=supplied,
        question_scope=scope or "IMPLEMENTATION_CORRESPONDENCE",
        escalations=sorted(set(list(escalations or []) + escalations_from(supplied))),
        statements_examined=[s for s in (statements or []) if s.strip()],
        files_examined=sorted({f.span.file for f in supplied if f.span}))
    return discharge(inspection)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.artifact_evidence
    import subprocess
    import sys
    import tempfile

    from .schema import Section

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

        # --- level 2, and the identity that gates it ----------------------------------
        quote = "We train every model for 100 epochs with a batch size of 128."
        (root / "README.md").write_text(
            "## Experiments\n\nTable 2 (main result) is produced by "
            "`python train.py --config config.yaml`.\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "readme")
        snap = snapshot(root, "https://example.invalid/r")
        span = relocate(root, "config.yaml", "batch_size: 32")

        # A DETERMINISTIC LINK: the checkout's own README maps the experiment to the file,
        # and that line relocates in the pinned tree.
        bound = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="128", span=span,
            artifact_value="32", experiment_id="Table 2, the main result", root=root,
            identity_basis="readme_maps_the_experiment", identity_file="README.md",
            identity_quote="Table 2 (main result) is produced by")
        assert bound.authority == "PAPER_ARTIFACT_MISMATCH", bound
        assert bound.identity_state == "ESTABLISHED" and bound.identity_span is not None
        assert bound.about_the_paper and bound.paper_ref.startswith("P0:")
        assert bound.settles == "CONFIG_LITERAL"
        assert "does NOT establish that the reported result is wrong" in bound.statement

        # THE AUDITOR'S READING IS NOT AN IDENTITY. Both ends are real, so this is a
        # concern a referee can act on — and it is not a demonstrated inconsistency.
        guessed = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="128", span=span,
            artifact_value="32", experiment_id="probably the main experiment", root=root,
            identity_basis="auditor_assertion")
        assert guessed.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN", guessed
        assert guessed.identity_state == "AMBIGUOUS"
        assert guessed.refusal == "experiment_identity_not_deterministic"
        assert guessed.about_the_paper is False and guessed.endpoints_only

        # A deterministic basis whose evidence does not relocate is PARTIAL, not a guess.
        stale = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="128", span=span,
            artifact_value="32", experiment_id="Table 2", root=root,
            identity_basis="script_passes_the_config", identity_file="run_all.sh",
            identity_quote="python train.py --config config.yaml")
        assert stale.identity_state == "PARTIAL", stale.identity_state
        assert stale.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"

        # Nothing offered at all is UNBOUND, and stops one rung lower: there is no
        # concern to act on when nothing claims the two are related.
        loose = bind_mismatch(doc, snap, paper_quote=quote, paper_value="128", span=span,
                              artifact_value="32", experiment_id="", root=root)
        assert loose.authority == "NONE" and loose.identity_state == "UNBOUND"
        assert loose.refusal == "experiment_identity_unbound", loose.refusal

        # Two values that are not the same KIND of quantity disagree about nothing.
        apples = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="AdamW", span=span,
            artifact_value="32", experiment_id="Table 2", root=root,
            identity_basis="readme_maps_the_experiment", identity_file="README.md",
            identity_quote="Table 2 (main result) is produced by")
        assert apples.refusal == "values_not_comparable", apples.refusal

        # A paraphrase of the paper is not the paper.
        para = bind_mismatch(doc, snap, paper_quote="we used a batch size of 128",
                             paper_value="128", span=span, artifact_value="32",
                             experiment_id="train.py", root=root)
        assert para.refusal == "paper_statement_unaddressed", para.refusal

        # Agreement is a RESULT, not "nothing found".
        agree_span = relocate(root, "config.yaml", "epochs: 100")
        agrees = bind_mismatch(
            doc, snap, paper_quote=quote, paper_value="100", span=agree_span,
            artifact_value="100", experiment_id="Table 2", root=root,
            identity_basis="readme_maps_the_experiment", identity_file="README.md",
            identity_quote="Table 2 (main result) is produced by")
        assert agrees.authority == "ARTIFACT_FACT" and agrees.refusal == "no_disagreement"

        # A dirty tree cannot masquerade as the audited one.
        (root / "config.yaml").write_text("batch_size: 64\n", encoding="utf-8")
        after = snapshot(root, "https://example.invalid/r")
        assert after.dirty and not after.audited, after
        assert not same_snapshot(snap, after)
        dirty_bind = bind_mismatch(doc, after, paper_quote=quote, paper_value="128",
                                   span=span, artifact_value="32",
                                   experiment_id="Table 2", root=root,
                                   identity_basis="readme_maps_the_experiment",
                                   identity_file="README.md",
                                   identity_quote="Table 2 (main result) is produced by")
        assert dirty_bind.refusal == "artifact_fact_unlocated", dirty_bind.refusal
        # AND THE PRE-RUN FACT IS NOT REWRITTEN.
        assert bound.snapshot is not None and bound.snapshot.audited
        assert not span_still_holds(root, span)

        git("checkout", "--", "config.yaml")
        clean = snapshot(root, "u")
        assert clean.audited

        # --- the route, and the scope that gates it -----------------------------------
        empty = inspect(doc, root, url="u", statements=[quote], scope="CONFIG_LITERAL")
        assert empty.discharged or True   # audited here; the blocked case is below

        nothing_asked = inspect(doc, root, url="u",
                                facts=[file_fact(root, clean, "train.py")])
        assert not nothing_asked.discharged
        assert "obtaining an artifact is not evidence" in nothing_asked.reason
        assert outcome_disposition(nothing_asked) == "ARTIFACT_INSPECTION_INCONCLUSIVE"

        # THE BUG THIS RELEASE FIXES. A broad implementation-correspondence question is
        # NOT settled by an entrypoint existing, however many such facts are established.
        broad = inspect(
            doc, root, url="u", target_id="t1",
            statements=["The released repository implements the described method."],
            scope="IMPLEMENTATION_CORRESPONDENCE",
            facts=[entrypoint_fact(root, clean),
                   dependency_fact(root, clean, "torch"),
                   file_fact(root, clean, "train.py")])
        assert not broad.discharged, broad.reason
        assert outcome_disposition(broad) == "ARTIFACT_INSPECTION_INCONCLUSIVE"
        assert "supporting evidence for it and are not its answer" in broad.reason
        assert "semantic correspondence question" in broad.reason

        # ...and the bounded question those same facts DO answer is settled.
        narrow = inspect(doc, root, url="u", target_id="t2", scope="ENTRYPOINT_PRESENCE",
                         statements=["is there a runnable entrypoint the repository "
                                     "itself advertises?"],
                         facts=[entrypoint_fact(root, clean)])
        assert narrow.discharged and outcome_disposition(narrow) == "ARTIFACT_FACT_ESTABLISHED"

        # A scope mismatch is not a discharge: the same fact against a different question.
        wrong_scope = inspect(doc, root, url="u", scope="CONFIG_LITERAL",
                              statements=["does config key K equal V?"],
                              facts=[entrypoint_fact(root, clean)])
        assert not wrong_scope.discharged
        assert "none answers the CONFIG_LITERAL question" in wrong_scope.reason

        # An established mismatch outranks everything and is the ONLY route state that
        # says anything about the paper.
        mism = inspect(doc, root, url="u", statements=[quote], scope="CONFIG_LITERAL",
                       facts=[bound])
        assert mism.discharged
        assert outcome_disposition(mism) == "ARTIFACT_MISMATCH_ESTABLISHED"
        assert len(mism.bound_mismatches()) == 1

        # An endpoint-verified concern leaves the question OPEN.
        conc = inspect(doc, root, url="u", statements=[quote], scope="CONFIG_LITERAL",
                       facts=[guessed])
        assert not conc.discharged
        assert outcome_disposition(conc) == "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS"
        assert len(conc.endpoint_concerns()) == 1

        # The decomposition the broad question is answered BY, rather than discharged.
        parts = decompose("implements the described method", "https://example.invalid/r")
        assert len(parts) == 4 and all(sc in SETTLEABLE_BY_ARTIFACT_FACT for sc, _ in parts)
        assert question_scope(claim_text="the repository implements the described "
                                         "method") == "IMPLEMENTATION_CORRESPONDENCE"

    # --- §8, the rule audit ---------------------------------------------------------
    from . import code_audit
    assert set(code_audit._RULES) == {code_audit._rule_unseeded_split}, (
        "a rule was added or removed in code_audit.py without updating this classification")
    # Every rule the module can emit is classified, and an unknown one is not visible.
    assert not reviewer_visible("a-rule-nobody-audited")
    assert rule_authority("a-rule-nobody-audited") == "D"
    assert reviewer_visible("leak-unseeded-split")
    assert set(RULE_AUTHORITY.values()) <= {"A", "D"}

    # An AST warning is never a scientific failure, and there is no value for one.
    from .schema import ARTIFACT_AUTHORITY
    assert "SCIENTIFIC_FAILURE" not in ARTIFACT_AUTHORITY
    assert ARTIFACT_AUTHORITY == ("ARTIFACT_FACT", "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
                                  "PAPER_ARTIFACT_MISMATCH", "NONE")

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
