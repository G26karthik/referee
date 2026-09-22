"""Validating and relocating the authors'-code reading a session subagent returns.

`python -m harness.artifact_review_driver` runs the self-check.

**One best-effort task per paper with a checkout, never retried, and its absence changes
nothing.** With no reading sealed yet — the gate closed, or the response malformed — the
artifact route still runs on `artifact_evidence`'s deterministic probes alone, and a
reviewer sees exactly what the harness could establish without a model. `stages/artifact.py`
persists the prompt (`harness.stages.artifact._reviewer_facts`) that `harness/tasks.py`
lists as an `artifact_review` task for the controlling session's own subagent to answer;
this module never spawns anything to answer it itself any more.

**What the reader is asked for is read-only, and what it says is relocated.** The task is
built to ask for `Read`/`Grep` over the checkout only — no `Bash`, no `Write`, no network —
so it can read the authors' code and cannot run it. Then every concern it returns is put
through `artifact_evidence.relocate`: the file must be in the pinned checkout, the quoted
text must be in that file, and it must occur exactly once. What does not relocate is
DROPPED and counted, exactly as `stages.audit.load_reports` drops a finding whose quotation
is not in the paper.

**A relocated citation is still not a mismatch.** Relocation establishes that the code says
what the reader said it says. Whether that disagrees with the PAPER needs the paper half
addressed and the experiment identity bound, which is `artifact_evidence.bind_mismatch`'s
three requirements and not this module's.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import artifact_evidence, delegation, sealing, state
from .reviewer_cli import envelope_provenance, unwrap_envelope
from .schema import (ARTIFACT_IDENTITY_BASES, ARTIFACT_IDENTITY_STATES,
                     ArtifactFact,
                     ArtifactInspection, ArtifactSnapshot, PaperDoc)
from .config import Config
from .prompts import artifact_review as AP

WRITERS = ("artifact_review_driver",) + tuple(delegation.WRITTEN_BY.values())

# The reader supplies a location, a quotation, and prose. Everything that decides what a
# concern ESTABLISHES is the harness's, and a value arriving under one of these names is
# counted as an attempted forgery rather than merged in — invariant 2, on a third channel.
_PROPOSAL_KEYS = ("kind", "title", "statement", "file", "code_quote", "paper_quote",
                  "paper_value", "artifact_value", "experiment_id", "identity_basis",
                  "identity_file", "identity_quote", "config_key", "counter_explanations")
HARNESS_OWNED_FACT_KEYS = ("authority", "refusal", "span", "snapshot", "paper_ref",
                           "fact_id", "probe", "settles",
                           # THE TWO THAT DECIDE WHETHER A CONCERN BECOMES A MISMATCH.
                           # The auditor supplies a basis and its evidence; whether that
                           # basis is DETERMINISTIC and whether its evidence relocates is
                           # `artifact_evidence.classify_identity`'s to say, and an
                           # auditor writing `identity_state: ESTABLISHED` would be
                           # signing the harness's name to the only judgement that
                           # separates level 2 from level 1.5.
                           "identity_state", "identity_span")


class ArtifactReviewDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    """WHICH model tier an `artifact_review` task should request — metadata
    `harness.tasks.pending` attaches to the task dict. No gate any more: there is no
    external process to spawn, so the only precondition for dispatching this task is
    `harness.stages.artifact._reviewer_facts`'s own (an audited checkout exists and no
    valid sealed reading covers it yet)."""
    return delegation.resolve_model(cfg.artifact_review_model, AP.ROLE_SPEC.get("model", ""))


def parse_concerns(text: str) -> tuple[list[dict], str, dict]:
    """(proposals, notes, meta). Proposals only — nothing here is located or accepted yet.

    Returns plain dicts rather than `ArtifactFact`s on purpose: an `ArtifactFact` carries
    an `authority` and a `span`, and both are the harness's to write. A proposal has
    neither until `locate_all` has been through it, so there is no moment at which an
    unlocated proposal exists wearing the type that means "established".
    """
    outer = (text or "").strip()
    if not outer:
        raise ArtifactReviewDriverError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                  "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise ArtifactReviewDriverError(f"the reader reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ArtifactReviewDriverError(
            f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise ArtifactReviewDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("concerns"), list):
        raise ArtifactReviewDriverError("output JSON has no 'concerns' list")
    out: list[dict] = []
    for row in data["concerns"]:
        if not isinstance(row, dict):
            meta["unknown_keys_dropped"] += 1
            continue
        for key in HARNESS_OWNED_FACT_KEYS:
            if key in row:
                meta["harness_keys_stripped"] += 1
        meta["unknown_keys_dropped"] += sum(
            1 for k in row if k not in _PROPOSAL_KEYS and k not in HARNESS_OWNED_FACT_KEYS)
        kind = str(row.get("kind") or "").strip().upper()
        out.append({
            "kind": kind if kind in AP.CONCERN_KINDS else "SUSPICIOUS_IMPLEMENTATION",
            "title": str(row.get("title") or "").strip()[:120],
            "statement": str(row.get("statement") or "").strip()[:1200],
            "file": str(row.get("file") or "").strip(),
            "code_quote": str(row.get("code_quote") or "").strip(),
            "paper_quote": str(row.get("paper_quote") or "").strip(),
            "paper_value": str(row.get("paper_value") or "").strip()[:80],
            "artifact_value": str(row.get("artifact_value") or "").strip()[:80],
            "experiment_id": str(row.get("experiment_id") or "").strip()[:300],
            "identity_basis": _basis(row.get("identity_basis")),
            "identity_file": str(row.get("identity_file") or "").strip(),
            "identity_quote": str(row.get("identity_quote") or "").strip(),
            "config_key": str(row.get("config_key") or "").strip()[:120],
            "counter_explanations": [str(x).strip()[:300] for x in
                                     (row.get("counter_explanations") or [])
                                     if str(x).strip()][:4],
        })
    return out, str(data.get("notes") or "").strip()[:2000], meta


def _basis(value) -> str:
    """The auditor's claimed identity basis, or `auditor_assertion`. Never a sixth value.

    An unrecognised basis falls back to the weakest one rather than being dropped: the
    auditor said SOMETHING about why it paired these, and recording that as a reading is
    more honest than recording nothing. It reaches AMBIGUOUS either way.
    """
    got = str(value or "").strip().lower()
    return got if got in ARTIFACT_IDENTITY_BASES else "auditor_assertion"


def _reread_config(root: str | Path, rel_file: str, key: str,
                   claimed: str) -> tuple[str, str]:
    """(the value the FILE states for `key`, how it was read). Never the auditor's copy.

    Once the auditor names a key, the harness can stop trusting the value it was handed
    and read the file itself. Two readers, tried in order, because a default in an
    `argparse` call is not a `key: value` line and a YAML entry is not an `add_argument`.

    AMBIGUITY REFUSES rather than picks. A key set in three places has three answers and
    choosing one would be the positional coincidence `parse_metric` exists to refuse; the
    auditor's own value is then kept and the disagreement is visible in `how`.
    """
    if not (rel_file and key):
        return claimed, "as the auditor reported it; no key was named to re-read"
    for reader, how in ((artifact_evidence.config_values, "re-read from the file as a "
                                                          "key/value line"),
                        (artifact_evidence.argparse_default, "re-read from the file as an "
                                                             "argparse default")):
        hits = reader(root, rel_file, key)
        if len(hits) == 1:
            return hits[0][0], how
        if len(hits) > 1:
            return claimed, (f"NOT re-read: `{key}` is set in {len(hits)} places in "
                             f"`{rel_file}`, so which one the paper means is not decidable "
                             f"from the file; the auditor's value is kept and the ambiguity "
                             f"is recorded")
    return claimed, f"NOT re-read: `{key}` was not found in `{rel_file}`"


def locate_all(doc: PaperDoc, root: str | Path, snap: ArtifactSnapshot,
               proposals: list[dict]) -> tuple[list[ArtifactFact], dict]:
    """Relocate every proposal's code citation, then try to bind the ones claiming a mismatch.

    Two gates, in this order, because they answer different questions and the second is
    meaningless without the first:

      1. **Does the code say what the reader said it says?** `relocate` searches the
         pinned file for the quotation and refuses an absent or a non-unique one. A
         proposal that fails here is DROPPED — not softened, not reported.
      2. **Does that disagree with the paper?** Only for a proposal that supplies a paper
         quotation and two values; `bind_mismatch` additionally requires the paper
         quotation to mint an address and the experiment identity to be non-empty, and
         records a NAMED refusal when either is missing. A proposal with no paper half is
         an ARTIFACT_FACT about the code and never a statement about the paper.
    """
    facts: list[ArtifactFact] = []
    meta = {"proposed": len(proposals), "relocated": 0, "dropped_unlocatable": 0,
            "paper_citations_relocated": 0, "bound_mismatches": 0,
            "endpoint_concerns": 0, "reread_from_file": 0,
            "identity": {k: 0 for k in ARTIFACT_IDENTITY_STATES}}
    for i, row in enumerate(proposals):
        span = artifact_evidence.relocate(root, row["file"], row["code_quote"])
        if span is None:
            meta["dropped_unlocatable"] += 1
            continue
        meta["relocated"] += 1
        fid = f"AF{i + 1:02d}"
        if row["paper_quote"] and row["paper_value"] and row["artifact_value"]:
            # THE VALUE IS RE-READ FROM THE FILE where the auditor named a key. What the
            # auditor says the code sets is a claim about the code; what the file says is
            # the fact, and the two are only the same when the harness has checked.
            value, how = _reread_config(root, row["file"], row.get("config_key", ""),
                                        row["artifact_value"])
            if value != row["artifact_value"] or "re-read from" in how:
                meta["reread_from_file"] += 1
            fact = artifact_evidence.bind_mismatch(
                doc, snap, paper_quote=row["paper_quote"], paper_value=row["paper_value"],
                span=span, artifact_value=value, root=root,
                experiment_id=row["experiment_id"], probe=f"code_review:{row['kind']}",
                identity_basis=row.get("identity_basis", ""),
                identity_file=row.get("identity_file", ""),
                identity_quote=row.get("identity_quote", ""),
                counter_explanations=row["counter_explanations"] + [how])
            meta["identity"][fact.identity_state] = \
                meta["identity"].get(fact.identity_state, 0) + 1
            if fact.paper_ref:
                meta["paper_citations_relocated"] += 1
            if fact.authority == "PAPER_ARTIFACT_MISMATCH":
                meta["bound_mismatches"] += 1
            elif fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN":
                meta["endpoint_concerns"] += 1
            facts.append(fact.model_copy(update={"fact_id": fid}))
            continue
        # A CODE-ONLY OBSERVATION. The quotation is located in the pinned tree, which is
        # all that is claimed: what it MEANS is the reader's prose and is carried as the
        # reader's, not as the harness's attestation.
        facts.append(ArtifactFact(
            fact_id=fid, probe=f"code_review:{row['kind']}", snapshot=snap, span=span,
            authority="ARTIFACT_FACT" if snap.audited else "NONE",
            artifact_value=row["artifact_value"],
            counter_explanations=row["counter_explanations"],
            statement=(f"`{span.file}:{span.line}` in the checkout at "
                       f"{snap.commit[:10] or '(unpinned)'} contains the quoted code. A "
                       f"reader's account of what that means: {row['statement'][:400]} "
                       f"— UNVERIFIED; the harness established the location, not the "
                       f"reading.")))
    return facts, meta


def _paths(cfg: Config, pid: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "artifact" / f"{pid}.inspection.json"
    return out, out.with_suffix(".driver.json")


def seal(cfg: Config, pid: str, inspection: ArtifactInspection, record: dict) -> dict:
    """`record` already carries whatever `delegation.provenance_record` produced --
    `accept` builds it explicitly. `mode`/`reviewer`/`tool_policy` are re-derived FROM
    `record` rather than recomputed a second, independent time (see `agent._seal_role`
    for the same shape shared by every other role)."""
    out, _sidecar = _paths(cfg, pid)
    return sealing.seal(
        out, inspection.model_dump(),
        mode=record.get("delegation_mode", "MANUAL"),
        reviewer=record.get("reviewer") or record.get("reader", ""),
        tool_policy=record.get("tool_policy", "unrecorded"),
        extra={**record, "paper_id": pid, "proposed": inspection.proposed,
              "relocated": inspection.relocated, "discharged": inspection.discharged},
    )


def accept(cfg: Config, pid: str, doc: PaperDoc, root: str | Path, raw: str, *,
           url: str = "", statements: list[str] | None = None, reader: str = "",
           tool_policy: str = "unrecorded", mode: str = "MANUAL",
           facts: list[ArtifactFact] | None = None, prompt_sha256: str = "") -> ArtifactInspection:
    """Relocate and seal a reading produced by a delegate -- a human, or (the live path,
    via `harness.tasks.seal`) a session subagent answering the `artifact_review` task
    `harness.stages.artifact._reviewer_facts` persisted.

    `prompt_sha256`, when given, is recorded on the sidecar so a LATER call to `load()`
    (or `harness.stages.artifact._reviewer_facts`'s own cache check) can tell whether this
    reading still answers the prompt currently on disk -- the same pinning
    `agent.accept_verdict` and every lens/grade seal already do.
    """
    proposals, notes, meta = parse_concerns(raw)
    snap = artifact_evidence.snapshot(root, url)
    located, lmeta = locate_all(doc, root, snap, proposals)
    inspection = artifact_evidence.inspect(
        doc, root, url=url, statements=statements,
        facts=list(facts or []) + located)
    inspection = inspection.model_copy(update={
        "proposed": lmeta["proposed"], "relocated": lmeta["relocated"]})
    prov = delegation.provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    record = {
        "written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
        "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
        "isolation_claim": prov["isolation_claim"],
        "tool_policy_provable": prov["tool_policy_provable"],
        "notes": notes, **{k: v for k, v in lmeta.items()},
        "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
        "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
    }
    if prompt_sha256:
        record["prompt_sha256"] = prompt_sha256
    seal(cfg, pid, inspection, record)
    return inspection


def load(cfg: Config, pid: str, *, commit: str = "",
        prompt_sha256: str = "") -> ArtifactInspection | None:
    """A sealed inspection for `pid`, or None. Verifies the seal, the COMMIT, and — when
    given — the PROMPT.

    `commit`, when given, must match the snapshot's. An inspection is a set of statements
    about one tree; served for a different checkout it would be citations into a tree
    nobody looked at, which is the failure `extraction_version` describes for the paper
    side and this one enforces.

    `prompt_sha256`, when given, must match what the sidecar recorded: an absent value on
    EITHER side is never treated as a mismatch, so a sidecar sealed before this parameter
    existed (or a caller not yet passing one) is not refused over a field that did not
    exist then. The prompt is a function of the paper's title, its method text, the
    checkout's file tree, the pinned commit, the advertised URL and the paper's own
    reported quantities (`stages.artifact._reviewer_facts`, via `prompts.artifact_review.
    build`); a changed hash means one of those moved, and a cached reading against the OLD
    inputs is not a cheaper answer to a question that has since changed.
    """
    out, sidecar = _paths(cfg, pid)
    ok, _why = sealing.verify_seal(out, accepted_writers=WRITERS)
    if not ok:
        return None
    try:
        rec = state.read_json(sidecar)
        recorded_prompt = str(rec.get("prompt_sha256") or "") if isinstance(rec, dict) else ""
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        inspection = ArtifactInspection(**state.read_json(out))
        if commit and (inspection.snapshot is None
                       or inspection.snapshot.commit != commit):
            return None
        return inspection
    except Exception:
        return None


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.artifact_review_driver
    import subprocess
    import sys
    import tempfile

    from .schema import Section

    # Model tier metadata, no gate, no command -- there is nothing left to resolve.
    assert role_model(Config(artifact_review_model="haiku")) == "haiku"
    assert role_model(Config()) == AP.ROLE_SPEC["model"]

    # A reader's own verdict on its own concern is stripped before anything is located.
    proposals, notes, meta = parse_concerns(json.dumps({
        "concerns": [{"kind": "PAPER_CODE_MISMATCH", "title": "t", "statement": "s",
                      "file": "a.py", "code_quote": "x = 1", "paper_quote": "q",
                      "authority": "PAPER_ARTIFACT_MISMATCH", "span": {"file": "a.py"},
                      "invented": 1}],
        "notes": "n"}))
    assert len(proposals) == 1 and notes == "n"
    assert "authority" not in proposals[0] and "span" not in proposals[0]
    assert meta["harness_keys_stripped"] == 2, meta
    assert meta["unknown_keys_dropped"] == 1, meta
    # An unknown concern kind falls back rather than passing through.
    assert parse_concerns(json.dumps({"concerns": [{"kind": "TOTALLY_FATAL"}]}))[0][0][
        "kind"] == "SUSPICIOUS_IMPLEMENTATION"

    for junk in ("", "no json", '{"nope": 1}', '{"concerns": "not a list"}'):
        try:
            parse_concerns(junk)
            raise AssertionError(f"should have refused {junk!r}")
        except ArtifactReviewDriverError:
            pass

    # The whole path, through a fake reader, against a real git checkout.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        (root / "config.yaml").write_text("batch_size: 32\nepochs: 100\n", encoding="utf-8")

        def git(*args):
            subprocess.run(["git", *args], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            git("init", "-q")
            git("config", "user.email", "s@e")
            git("config", "user.name", "s")
            git("add", "-A")
            git("commit", "-qm", "c")
        except (OSError, subprocess.CalledProcessError):   # pragma: no cover
            print("harness.artifact_review_driver self-check skipped: git unavailable")
            sys.exit(0)

        cfg = Config(projects_dir=Path(td) / "projects")
        state.create_project(cfg, "", "T", pid="p")
        doc = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
            Section(section_idx=0, title="Method", page_start=1,
                    text="We train with a batch size of 128 on every benchmark.")])
        (root / "README.md").write_text(
            "Table 2 of the paper is produced by `python train.py --config config.yaml`.\n",
            encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "readme")

        payload = json.dumps({"concerns": [
            # BOUND: the paper half mints, the README states the experiment-to-config
            # link and relocates, and the two values disagree.
            {"kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
             "file": "config.yaml", "code_quote": "batch_size: 32",
             "paper_quote": "We train with a batch size of 128 on every benchmark.",
             "paper_value": "128", "artifact_value": "32", "config_key": "batch_size",
             "experiment_id": "Table 2",
             "identity_basis": "readme_maps_the_experiment",
             "identity_file": "README.md",
             "identity_quote": "Table 2 of the paper is produced by",
             "counter_explanations": ["a launcher may override it"]},
            # A READING IS NOT AN IDENTITY: both ends verified, the relation model-proposed.
            {"kind": "PAPER_CODE_MISMATCH", "title": "batch size again", "statement": "s",
             "file": "config.yaml", "code_quote": "epochs: 100",
             "paper_quote": "We train with a batch size of 128 on every benchmark.",
             "paper_value": "128", "artifact_value": "100",
             "experiment_id": "probably the ablation", "identity_basis": "auditor_assertion"},
            # unlocatable: DROPPED WHOLE
            {"kind": "HARD_CODED_RESULT", "title": "x", "statement": "s",
             "file": "config.yaml", "code_quote": "accuracy = 0.914  # hard-coded"},
            # code-only: an ARTIFACT_FACT, never a statement about the paper
            {"kind": "SUSPICIOUS_IMPLEMENTATION", "title": "y", "statement": "odd",
             "file": "config.yaml", "code_quote": "batch_size: 32"},
        ], "notes": "n"})
        got = accept(cfg, "p", doc, root, payload,
                     statements=["We train with a batch size of 128 on every benchmark."],
                     reader="self-check")
        assert got.proposed == 4 and got.relocated == 3, (got.proposed, got.relocated)
        assert len(got.facts) == 3, "the unlocatable concern is dropped whole"

        bound = got.bound_mismatches()
        assert len(bound) == 1 and bound[0].paper_ref.startswith("P0:"), bound
        assert bound[0].identity_state == "ESTABLISHED"
        assert bound[0].identity_basis == "readme_maps_the_experiment"
        assert bound[0].identity_span is not None
        # The value was RE-READ from the file rather than taken from the auditor.
        assert any("re-read from the file" in c for c in bound[0].counter_explanations)

        concerns = got.endpoint_concerns()
        assert len(concerns) == 1 and concerns[0].identity_state == "AMBIGUOUS", concerns
        assert concerns[0].refusal == "experiment_identity_not_deterministic"
        assert concerns[0].about_the_paper is False

        # AN ESTABLISHED MISMATCH DISCHARGES; a concern alone would not.
        assert got.discharged and "identity bound to a deterministic source" in got.reason
        assert artifact_evidence.outcome_disposition(got) == "ARTIFACT_MISMATCH_ESTABLISHED"
        concern_only = artifact_evidence.inspect(
            doc, root, statements=["We train with a batch size of 128 on every benchmark."],
            scope="CONFIG_LITERAL", facts=[concerns[0]])
        assert not concern_only.discharged, "a model-proposed relation settles nothing"
        assert artifact_evidence.outcome_disposition(concern_only)             == "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS"

        # The code-only fact is about the ARTIFACT and says its reading is unverified.
        code_only = [f for f in got.facts
                     if f.authority == "ARTIFACT_FACT" and not f.paper_ref]
        assert len(code_only) == 1 and "UNVERIFIED" in code_only[0].statement

        back = load(cfg, "p", commit=artifact_evidence.snapshot(root).commit)
        assert back is not None and back.relocated == 3
        assert load(cfg, "p", commit="0" * 40) is None, \
            "an inspection is about ONE tree and may not be served for another"

        # `prompt_sha256`: an ABSENT value on either side is never a mismatch. This
        # fixture was sealed through `accept()`, whose sidecar never records one — so a
        # caller supplying one here must still get the inspection back.
        assert load(cfg, "p", prompt_sha256="c" * 64) is not None

        # A REAL prompt-hash mismatch — the sidecar recorded one, the caller asks for a
        # different one — is refused; the same hash back is served; and asking with none
        # at all skips the check entirely, the identical comparison shape those two
        # siblings already use.
        seal(cfg, "p", back, {"written_by": "artifact_review_driver", "reader": "",
                              "delegation_mode": "CLI_SUBPROCESS", "tool_policy": "unrecorded",
                              "prompt_sha256": "a" * 64})
        assert load(cfg, "p", prompt_sha256="a" * 64) is not None
        assert load(cfg, "p", prompt_sha256="b" * 64) is None
        assert load(cfg, "p") is not None

        # A file edited after sealing is not the file that was sealed.
        out, _sidecar = _paths(cfg, "p")
        out.write_text(out.read_text(encoding="utf-8").replace('"relocated": 3',
                                                               '"relocated": 9'),
                       encoding="utf-8")
        assert load(cfg, "p") is None

    print("harness.artifact_review_driver self-check ok")
