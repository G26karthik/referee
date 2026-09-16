"""Delegating the authors'-code reading, and relocating every citation it returns.

`python -m harness.artifact_review_driver` runs the self-check.

One best-effort call per paper with a checkout, never retried, and its failure changes
nothing: with the gate closed — or with the CLI unreachable, or the response malformed —
the artifact route still runs on `artifact_evidence`'s deterministic probes alone, and a
reviewer sees exactly what the harness could establish without a model. That is the same
property `allow_grading` and `allow_claim_links` have, and it is what makes a model channel
on the always-on path assessable: a channel nobody can turn off cannot be measured.

**What the reader gets is read-only, and what it says is relocated.** The confinement
grants `Read` and `Grep` over the checkout and nothing else — no `Bash`, no `Write`, no
network — so it can read the authors' code and cannot run it. Then every concern it returns
is put through `artifact_evidence.relocate`: the file must be in the pinned checkout, the
quoted text must be in that file, and it must occur exactly once. What does not relocate is
DROPPED and counted, exactly as `stages.audit.load_reports` drops a finding whose quotation
is not in the paper.

**A relocated citation is still not a mismatch.** Relocation establishes that the code says
what the reader said it says. Whether that disagrees with the PAPER needs the paper half
addressed and the experiment identity bound, which is `artifact_evidence.bind_mismatch`'s
three requirements and not this module's.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import artifact_evidence, delegation, state
from .artifacts import ArtifactFact, ArtifactInspection, ArtifactSnapshot, PaperDoc
from .audit_driver import (Confinement, _kill_tree, denied_tools, envelope_provenance,
                           operator_confinement, unwrap_envelope, write_pinned_settings)
from .config import Config
from .prompts import artifact_review as AP

WRITERS = ("artifact_review_driver",) + tuple(delegation.WRITTEN_BY.values())

# The reader supplies a location, a quotation, and prose. Everything that decides what a
# concern ESTABLISHES is the harness's, and a value arriving under one of these names is
# counted as an attempted forgery rather than merged in — invariant 2, on a third channel.
_PROPOSAL_KEYS = ("kind", "title", "statement", "file", "code_quote", "paper_quote",
                  "paper_value", "artifact_value", "experiment_id", "counter_explanations")
HARNESS_OWNED_FACT_KEYS = ("authority", "refusal", "span", "snapshot", "paper_ref",
                           "fact_id", "probe")


class ArtifactReviewDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return (cfg.artifact_review_model or "").strip() or str(AP.ROLE_SPEC.get("model", ""))


def review_confinement(cfg: Config, *, settings_sha256: str = "",
                       repo_dir: str = "") -> Confinement:
    """Read-only over the CHECKOUT, and nothing else. No execution, by construction.

    `Read` and `Grep` are the minimum for reading a repository nobody has mapped, and they
    are the maximum this role may ever have: an auditor that could run the code would be
    an execution path with none of `authorize()`'s preconditions in front of it — no
    verified commit, no experiment identity, no resource check, no provenance. The
    denied-tool list is written to a pinned settings file and enforced by the CLI, which
    is what lets this mode report a tool policy at all.
    """
    if cfg.artifact_review_cmd.strip():
        return operator_confinement("authors'-code auditor")
    allowed = ("Read", "Grep")
    return Confinement(
        role="artifact_review", allowed_tools=allowed,
        disallowed_tools=denied_tools(allowed),
        add_dir=((repo_dir,) if repo_dir else ()), model=role_model(cfg),
        restricted=True, strict_mcp=True, bare=False,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "",
                repo_dir: str = "") -> str:
    cfg = cfg or Config()
    exe = ((exe or "").strip() or (cfg.reviewer_exe or "").strip()
           or (shutil.which("claude") or ""))
    if not exe:
        return ""
    reader = "type" if os.name == "nt" else "cat"
    extra = review_confinement(
        Config(artifact_review_cmd="", artifact_review_model=cfg.artifact_review_model),
        repo_dir=repo_dir).flags(settings)
    return f'{reader} "{{prompt}}" | "{exe}" -p{extra} > "{{out}}"'


def resolve_cmd(cfg: Config, *, settings: str = "", repo_dir: str = "") -> str:
    return cfg.artifact_review_cmd.strip() or default_cmd(
        cfg, exe=cfg.reviewer_exe, settings=settings, repo_dir=repo_dir)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_artifact_review:
        return False, ("authors'-code audit gate is closed: SH_ALLOW_ARTIFACT_REVIEW is "
                       "not set. With it closed the artifact route still runs on "
                       "deterministic probes alone")
    cmd = resolve_cmd(cfg)
    if not cmd:
        return False, "no reader available for the authors'-code pass"
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, "SH_ARTIFACT_REVIEW_CMD must contain both {prompt} and {out}"
    return True, ""


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
            "counter_explanations": [str(x).strip()[:300] for x in
                                     (row.get("counter_explanations") or [])
                                     if str(x).strip()][:4],
        })
    return out, str(data.get("notes") or "").strip()[:2000], meta


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
            "bound_mismatches": 0}
    for i, row in enumerate(proposals):
        span = artifact_evidence.relocate(root, row["file"], row["code_quote"])
        if span is None:
            meta["dropped_unlocatable"] += 1
            continue
        meta["relocated"] += 1
        fid = f"AF{i + 1:02d}"
        if row["paper_quote"] and row["paper_value"] and row["artifact_value"]:
            fact = artifact_evidence.bind_mismatch(
                doc, snap, paper_quote=row["paper_quote"], paper_value=row["paper_value"],
                span=span, artifact_value=row["artifact_value"],
                experiment_id=row["experiment_id"], probe=f"code_review:{row['kind']}",
                counter_explanations=row["counter_explanations"])
            if fact.authority == "PAPER_ARTIFACT_MISMATCH":
                meta["bound_mismatches"] += 1
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
    out, sidecar = _paths(cfg, pid)
    state.write_json(out, inspection.model_dump())
    record = dict(record)
    record.update({"paper_id": pid, "proposed": inspection.proposed,
                   "relocated": inspection.relocated,
                   "discharged": inspection.discharged,
                   "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                   "ts": state.now()})
    state.write_json(sidecar, record)
    return record


def accept(cfg: Config, pid: str, doc: PaperDoc, root: str | Path, raw: str, *,
           url: str = "", statements: list[str] | None = None, reader: str = "",
           tool_policy: str = "unrecorded", mode: str = "MANUAL",
           facts: list[ArtifactFact] | None = None) -> ArtifactInspection:
    """Relocate and seal a reading produced OUTSIDE this module's subprocess."""
    proposals, notes, meta = parse_concerns(raw)
    snap = artifact_evidence.snapshot(root, url)
    located, lmeta = locate_all(doc, root, snap, proposals)
    inspection = artifact_evidence.inspect(
        doc, root, url=url, statements=statements,
        facts=list(facts or []) + located)
    inspection = inspection.model_copy(update={
        "proposed": lmeta["proposed"], "relocated": lmeta["relocated"]})
    prov = delegation.provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    seal(cfg, pid, inspection, {
        "written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
        "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
        "isolation_claim": prov["isolation_claim"],
        "tool_policy_provable": prov["tool_policy_provable"],
        "notes": notes, **{k: v for k, v in lmeta.items()},
        "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
        "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
    })
    return inspection


def load(cfg: Config, pid: str, *, commit: str = "") -> ArtifactInspection | None:
    """A sealed inspection for `pid`, or None. Verifies the seal, and the COMMIT.

    `commit`, when given, must match the snapshot's. An inspection is a set of statements
    about one tree; served for a different checkout it would be citations into a tree
    nobody looked at, which is the failure `extraction_version` describes for the paper
    side and this one enforces.
    """
    out, sidecar = _paths(cfg, pid)
    if not (out.exists() and sidecar.exists()):
        return None
    try:
        rec = state.read_json(sidecar)
        if not isinstance(rec, dict) or rec.get("written_by") not in WRITERS:
            return None
        if hashlib.sha256(out.read_bytes()).hexdigest() != rec.get("content_sha256"):
            return None
        inspection = ArtifactInspection(**state.read_json(out))
        if commit and (inspection.snapshot is None
                       or inspection.snapshot.commit != commit):
            return None
        return inspection
    except Exception:
        return None


def run(cfg: Config, pid: str, doc: PaperDoc, root: str | Path, prompt_text: str, *,
        url: str = "", statements: list[str] | None = None,
        facts: list[ArtifactFact] | None = None) -> ArtifactInspection | None:
    """One best-effort call. Returns None on ANY failure, and never raises."""
    ok, _why = available(cfg)
    if not ok:
        return None
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="sh-artreview-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-artreview-policy-"))
        conf = review_confinement(cfg, repo_dir=str(root))
        settings_path = ""
        try:
            if conf.enforced:
                sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
                settings_path = str(sp)
                conf = dataclasses.replace(conf, settings_sha256=sha)
        except OSError:
            shutil.rmtree(policy_dir, ignore_errors=True)
            return None
        cmd = (resolve_cmd(cfg, settings=settings_path, repo_dir=str(root))
               .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
        group_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})
        sandbox = Path(tempfile.mkdtemp(prefix="sh-artreview-sandbox-"))
        try:
            try:
                proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        **group_kwargs)
            except OSError:
                return None
            try:
                proc.communicate(timeout=cfg.artifact_review_timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.communicate()
                return None
            if not out.exists():
                return None
            try:
                raw = out.read_text(encoding="utf-8")
                proposals, notes, meta = parse_concerns(raw)
            except (OSError, ArtifactReviewDriverError):
                return None
            snap = artifact_evidence.snapshot(root, url)
            located, lmeta = locate_all(doc, root, snap, proposals)
            inspection = artifact_evidence.inspect(
                doc, root, url=url, statements=statements,
                facts=list(facts or []) + located).model_copy(
                    update={"proposed": lmeta["proposed"],
                            "relocated": lmeta["relocated"]})
            try:
                seal(cfg, pid, inspection, {
                    "written_by": "artifact_review_driver", "reader": "",
                    "command": cmd, "returncode": proc.returncode,
                    "seconds": round(time.time() - started, 1),
                    "tool_policy": conf.summary(), "tool_policy_detail": conf.policy(),
                    "prompt_sha256": prompt_sha, "notes": notes,
                    "raw_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                    "envelope": meta.get("envelope") or {}, **lmeta,
                    "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
                    "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
                })
            except OSError:
                pass
            return inspection
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.artifact_review_driver
    from .artifacts import Section

    assert available(Config(allow_artifact_review=False))[0] is False
    assert "SH_ALLOW_ARTIFACT_REVIEW" in available(Config(allow_artifact_review=False))[1]
    bad = Config(allow_artifact_review=True, artifact_review_cmd="reader --in {prompt}")
    assert available(bad)[0] is False, "a template with no {out} must be refused"
    assert available(Config(allow_artifact_review=True,
                            artifact_review_cmd="cp {prompt} {out}")) == (True, "")

    # READ-ONLY, AND NO EXECUTION. The confinement is the enforcement, not the prompt.
    conf = review_confinement(Config(allow_artifact_review=True))
    assert set(conf.allowed_tools) == {"Read", "Grep"}, conf.allowed_tools
    for forbidden in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
        assert forbidden in conf.disallowed_tools, forbidden
    assert conf.model == "sonnet" and conf.enforced is True

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
        (root / "config.yaml").write_text("batch_size: 32\n", encoding="utf-8")

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

        cfg = Config(projects_dir=Path(td) / "projects", allow_artifact_review=True)
        state.create_project(cfg, "", "T", pid="p")
        doc = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
            Section(section_idx=0, title="Method", page_start=1,
                    text="We train with a batch size of 128 on every benchmark.")])
        payload = json.dumps({"concerns": [
            # bound: paper half mints, identity given, values disagree
            {"kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
             "file": "config.yaml", "code_quote": "batch_size: 32",
             "paper_quote": "We train with a batch size of 128 on every benchmark.",
             "paper_value": "128", "artifact_value": "32",
             "experiment_id": "the only config the entrypoint reads",
             "counter_explanations": ["a launcher may override it"]},
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
        assert got.proposed == 3 and got.relocated == 2, (got.proposed, got.relocated)
        assert len(got.facts) == 2, "the unlocatable concern is dropped whole"
        bound = got.bound_mismatches()
        assert len(bound) == 1 and bound[0].paper_ref.startswith("P0:"), bound
        assert got.discharged and artifact_evidence.outcome_disposition(got) \
            == "ARTIFACT_RESOLVED"
        # The code-only fact is about the ARTIFACT and says its reading is unverified.
        code_only = [f for f in got.facts if not f.about_the_paper]
        assert len(code_only) == 1 and code_only[0].authority == "ARTIFACT_FACT"
        assert "UNVERIFIED" in code_only[0].statement

        back = load(cfg, "p", commit=artifact_evidence.snapshot(root).commit)
        assert back is not None and back.relocated == 2
        assert load(cfg, "p", commit="0" * 40) is None, \
            "an inspection is about ONE tree and may not be served for another"

        # A file edited after sealing is not the file that was sealed.
        out, _sidecar = _paths(cfg, "p")
        out.write_text(out.read_text(encoding="utf-8").replace('"relocated": 2',
                                                               '"relocated": 9'),
                       encoding="utf-8")
        assert load(cfg, "p") is None

    print("harness.artifact_review_driver self-check ok")
