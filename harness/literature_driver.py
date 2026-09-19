"""Delegating the two literature readings, and verifying every endpoint they return.

`python -m harness.literature_driver` runs the self-check.

Two best-effort calls per paper, never retried, and their failure changes nothing: with
the gate closed — or the CLI unreachable, or the response malformed — the prior-art route
still runs, on the paper's own novelty sentences and the deterministic queries built from
them, and a reviewer sees exactly what the harness could establish without a model. That
is the property `allow_grading`, `allow_claim_links` and `allow_artifact_review` all have,
and it is what makes a model channel measurable: a channel nobody can turn off cannot be
compared against one that never ran.

**Neither reader touches anything.** The confinement grants NO tools at all — not `Read`,
not `Grep`, and certainly not `WebFetch`. **The reader must not do its own searching**:
every candidate it sees was retrieved by `literature_providers`, cached, hashed and
printed into the prompt, so that a quotation can be checked against the text this harness
actually holds. A reader that fetched its own page would be quoting something nobody else
can re-read.

**And what comes back is stripped at the boundary.** `authority`, `refusal`, `chronology`,
`citation_state`, `target_ref`, `statement` and `candidate_quote_located` are the
harness's to write, and so is anything spelled `accepted`, `prior_art_established`,
`novelty_failure`, `severity` or `paper_decision` — a proposal arriving with one of those
is counted as an attempted forgery rather than merged in. Invariant 2, on a fourth channel.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import delegation, literature, reviewer_cli, state
from .artifacts import (LITERATURE_RELATIONS, LiteratureSearch, PaperDoc, PriorArtFact,
                        SearchClaim, SearchCutoff, WorkIdentity)
from .config import Config
from .prompts import literature as LP
from .reviewer_cli import (Confinement, _kill_tree, denied_tools, operator_confinement,
                           unwrap_envelope, write_pinned_settings)

WRITERS = ("literature_driver",) + tuple(delegation.WRITTEN_BY.values())

# What a reader may supply. Everything else that decides what a pairing ESTABLISHES is
# the harness's, and `binding_basis` is in this list on purpose: the reader may NAME a
# basis, and whether that basis is deterministic — and whether the claim it is offered for
# actually asserts priority — is `literature.bind_prior_art`'s to say.
_MATCH_KEYS = ("candidate_id", "relation", "candidate_quote", "overlap", "binding_basis")
HARNESS_OWNED_MATCH_KEYS = (
    "authority", "refusal", "statement", "chronology", "chronology_basis",
    "citation_state", "target_ref", "target_quote", "fact_id", "claim_id",
    "candidate", "candidate_quote_located",
    # THE FOUR THAT WOULD MAKE THIS CHANNEL A VERDICT. None of them is a field on
    # `PriorArtFact` at all, which is the point: a reader writing one is not overriding a
    # value, it is inventing a vocabulary this system does not have.
    "accepted", "prior_art_established", "novelty_failure", "severity", "paper_decision",
)
_QUERY_KEYS = ("family", "query")


class LiteratureDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return delegation.resolve_model(cfg.literature_model, LP.REVIEW_ROLE_SPEC.get("model", ""))


def review_confinement(cfg: Config, *, settings_sha256: str = "") -> Confinement:
    """NO TOOLS. The reader is given two texts and asked to compare them.

    Zero rather than `Read`, and the zero is load-bearing. A reader with `WebFetch` would
    search the literature itself, and then the passage it quoted would be from a page this
    harness never retrieved, never hashed and cannot re-read — which is exactly the
    "a snippet is not a work" failure `bind_prior_art` refuses. A reader with `Read` could
    reach this harness's own artifacts, including the findings it must not see.
    """
    if cfg.literature_cmd.strip():
        return operator_confinement("literature reviewer")
    return Confinement(
        role="literature", allowed_tools=(), disallowed_tools=denied_tools(()),
        add_dir=(), model=role_model(cfg), restricted=True, strict_mcp=True, bare=False,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "") -> str:
    cfg = cfg or Config()
    exe = delegation.resolve_reviewer_exe(exe, cfg.reviewer_exe)
    if not exe:
        return ""
    extra = review_confinement(
        Config(literature_cmd="", literature_model=cfg.literature_model)).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, *, settings: str = "") -> str:
    return cfg.literature_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe,
                                                     settings=settings)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_literature_review:
        return False, ("literature-review gate is closed: SH_ALLOW_LITERATURE_REVIEW is "
                       "not set. With it closed the prior-art route still runs, on the "
                       "paper's own novelty sentences and deterministic queries, and "
                       "adjudicates nothing semantically")
    return delegation.check_command(resolve_cmd(cfg), "SH_LITERATURE_CMD")


# --------------------------------------------------------------------------------------
# PARSING — proposals only. Nothing here is verified, and nothing here is a PriorArtFact.
# --------------------------------------------------------------------------------------
def _outer(text: str) -> dict:
    """The JSON object inside the CLI's envelope, however the reader wrapped it.

    The outermost braces are taken rather than the whole string parsed, exactly as
    `claimlink_driver.parse_links` does: a reader that prefixes a sentence or wraps its
    answer in a markdown fence has still answered, and failing the call for that would
    throw away a real reading over a formatting habit. What is NOT tolerated is anything
    that changes the content — no key renaming, no repair, no partial parse.
    """
    raw = (text or "").strip()
    if not raw:
        raise LiteratureDriverError("the command wrote an empty file")
    try:
        body, envelope = unwrap_envelope(raw)
    except Exception as exc:                       # noqa: BLE001 — any shape but ours
        raise LiteratureDriverError(f"unparseable response: {exc}") from exc
    if envelope and envelope.get("is_error"):
        raise LiteratureDriverError(f"the reader reported an error: {str(body)[:200]}")
    if isinstance(body, dict):
        return body
    inner = str(body or "")
    start, end = inner.find("{"), inner.rfind("}")
    if start < 0 or end <= start:
        raise LiteratureDriverError(
            f"no JSON object in the output (first 120 chars: {inner[:120]!r})")
    try:
        parsed = json.loads(inner[start:end + 1])
    except ValueError as exc:
        raise LiteratureDriverError(f"response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LiteratureDriverError(f"response was {type(parsed).__name__}, not an object")
    return parsed


def parse_queries(text: str) -> tuple[list[tuple[str, str]], str]:
    """[(family, query)] the proposer offered. Unknown families are DROPPED, not renamed.

    A family this harness does not know is a query outside the declared protocol, and the
    protocol is the only thing that makes "completed" mean anything.
    """
    body = _outer(text)
    out: list[tuple[str, str]] = []
    for item in (body.get("queries") or []):
        if not isinstance(item, dict):
            continue
        family = str(item.get("family") or "").strip().upper()
        query = " ".join(str(item.get("query") or "").split())
        if family in LP.QUERY_FAMILIES and 3 <= len(query) <= 200:
            out.append((family, query))
    return out, str(body.get("notes") or "")


def parse_matches(text: str) -> tuple[list[dict], str, dict]:
    """(proposals, notes, meta). Plain dicts, deliberately — see `parse_concerns`.

    A `PriorArtFact` carries an `authority` and a `chronology`, and both are the harness's
    to write. Returning dicts means there is no moment at which an unverified proposal
    exists wearing the type that means "established".
    """
    body = _outer(text)
    stripped = dropped = 0
    out: list[dict] = []
    for item in (body.get("matches") or []):
        if not isinstance(item, dict):
            continue
        clean: dict = {}
        for key, value in item.items():
            k = str(key).strip()
            if k in HARNESS_OWNED_MATCH_KEYS:
                stripped += 1
                continue
            if k not in _MATCH_KEYS:
                dropped += 1
                continue
            clean[k] = value
        relation = str(clean.get("relation") or "").strip().upper()
        clean["relation"] = relation if relation in LITERATURE_RELATIONS \
            else "INSUFFICIENT_EVIDENCE"
        clean["candidate_id"] = str(clean.get("candidate_id") or "").strip()
        clean["candidate_quote"] = str(clean.get("candidate_quote") or "")
        clean["binding_basis"] = str(clean.get("binding_basis") or "").strip()
        clean["overlap"] = str(clean.get("overlap") or "")
        if clean["candidate_id"]:
            out.append(clean)
    meta = {"harness_keys_stripped": stripped, "unknown_keys_dropped": dropped,
            "candidates_read": int(body.get("candidates_read") or 0)}
    return out, str(body.get("notes") or ""), meta


# --------------------------------------------------------------------------------------
# ADJUDICATION — proposals in, verified facts out
# --------------------------------------------------------------------------------------
def candidates_block(works: list[WorkIdentity]) -> tuple[str, dict[str, WorkIdentity]]:
    """The candidate list as the reader sees it, and the id -> work map used to check it.

    The reader quotes from THIS text and nothing else, so what is printed here is exactly
    what `literature._passage_present` will search. Truncating an abstract would silently
    make a correct quotation unverifiable, so it is printed whole.
    """
    lines: list[str] = []
    by_id: dict[str, WorkIdentity] = {}
    for i, work in enumerate(works, start=1):
        cid = f"c{i}"
        by_id[cid] = work
        when = work.date or (str(work.year) if work.year else "date unknown")
        ident = work.canonical_id or literature.canonical_id(work)
        lines.append(f"[{cid}] {work.title or '(untitled)'} ({when}) {ident}\n"
                     f"      venue: {work.venue or '(unknown)'}\n"
                     f"      abstract: {work.abstract or '(no abstract retrieved)'}")
    return "\n".join(lines), by_id


def adjudicate(doc: PaperDoc, claim: SearchClaim, proposals: list[dict],
               by_id: dict[str, WorkIdentity], *, cutoff: SearchCutoff | None = None,
               bibliography: str | None = None) -> list[PriorArtFact]:
    """Put every proposal through `bind_prior_art`. A proposal naming no known candidate
    is dropped whole — it is a work this harness never retrieved, and a passage from it
    could not be checked against anything.
    """
    out: list[PriorArtFact] = []
    for proposal in proposals:
        work = by_id.get(proposal.get("candidate_id", ""))
        if work is None:
            continue
        out.append(literature.bind_prior_art(
            doc, claim, work,
            relation=proposal.get("relation", "INSUFFICIENT_EVIDENCE"),
            candidate_quote=proposal.get("candidate_quote", ""),
            cutoff=cutoff, binding_basis=proposal.get("binding_basis", ""),
            reviewer_note=proposal.get("overlap", ""), bibliography=bibliography))
    return out


# --------------------------------------------------------------------------------------
# SEALING
# --------------------------------------------------------------------------------------
def _paths(cfg: Config, pid: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "literature" / f"{pid}.search.json"
    return out, out.with_suffix(".driver.json")


def seal(cfg: Config, pid: str, search: LiteratureSearch, record: dict) -> dict:
    out, sidecar = _paths(cfg, pid)
    state.write_json(out, search.model_dump())
    record = dict(record)
    record.update({"paper_id": pid, "proposed": search.proposed,
                   "concerns": len(search.concerns()),
                   "structurally_bound": len(search.bound_relations()),
                   "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                   "ts": state.now()})
    state.write_json(sidecar, record)
    return record


def load(cfg: Config, pid: str) -> LiteratureSearch | None:
    """A sealed search for `pid`, or None. Verifies the seal before trusting it."""
    out, sidecar = _paths(cfg, pid)
    if not (out.exists() and sidecar.exists()):
        return None
    try:
        rec = state.read_json(sidecar)
        if not isinstance(rec, dict) or rec.get("written_by") not in WRITERS:
            return None
        if hashlib.sha256(out.read_bytes()).hexdigest() != rec.get("content_sha256"):
            return None
        return LiteratureSearch(**state.read_json(out))
    except Exception:                              # noqa: BLE001 — a bad seal is no seal
        return None


# --------------------------------------------------------------------------------------
# THE CALL
# --------------------------------------------------------------------------------------
def call(cfg: Config, prompt_text: str, *, tag: str = "literature"
         ) -> tuple[str, dict]:
    """One confined call. ('', record) on ANY failure — and the record says which failure.

    A silent None makes "the reader found nothing" and "the reader never answered"
    indistinguishable, which is the defect the artifact-review channel had on its first
    live run: two of four calls returned nothing with no trace, and the histogram counted
    them as clean. Every early return here records the command, the return code and the
    output tails.
    """
    started = time.time()
    record: dict = {"tag": tag, "seconds": 0.0}
    ok, why = available(cfg)
    if not ok:
        record.update({"failure": "gate_or_command", "detail": why})
        return "", record
    with tempfile.TemporaryDirectory(prefix="sh-lit-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-lit-policy-"))
        conf = review_confinement(cfg)
        settings_path = ""
        try:
            if conf.enforced:
                sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
                settings_path = str(sp)
                conf = dataclasses.replace(conf, settings_sha256=sha)
        except OSError as exc:
            shutil.rmtree(policy_dir, ignore_errors=True)
            record.update({"failure": "policy_unwritable", "detail": str(exc)})
            return "", record
        cmd = (resolve_cmd(cfg, settings=settings_path)
               .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
        record.update({"command": cmd, "tool_policy": conf.summary(),
                       "tool_policy_detail": conf.policy(), "model": conf.model,
                       "prompt_sha256": hashlib.sha256(
                           prompt_text.encode("utf-8")).hexdigest()})
        group_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})
        sandbox = Path(tempfile.mkdtemp(prefix="sh-lit-sandbox-"))
        try:
            try:
                proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        **group_kwargs)
            except OSError as exc:
                record.update({"failure": "process_unstartable", "detail": str(exc)})
                return "", record
            try:
                stdout, stderr = proc.communicate(timeout=cfg.literature_timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.communicate()
                record.update({"failure": "timeout",
                               "seconds": round(time.time() - started, 1)})
                return "", record
            record.update({"returncode": proc.returncode,
                           "stdout_tail": (stdout or "")[-400:],
                           "stderr_tail": (stderr or "")[-400:],
                           "seconds": round(time.time() - started, 1)})
            if not out.exists():
                record["failure"] = "no_output_file"
                return "", record
            body = out.read_text(encoding="utf-8")
            record["raw_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
            if not body.strip():
                record["failure"] = "empty_output"
                return "", record
            return body, record
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.literature_driver
    from .artifacts import Section

    assert available(Config(allow_literature_review=False))[0] is False
    assert "SH_ALLOW_LITERATURE_REVIEW" in available(Config(allow_literature_review=False))[1]
    bad = Config(allow_literature_review=True, literature_cmd="reader --in {prompt}")
    assert available(bad)[0] is False, "a template with no {out} must be refused"
    assert available(Config(allow_literature_review=True,
                            literature_cmd="cp {prompt} {out}")) == (True, "")

    # NO TOOLS AT ALL, and the network ones are denied by name as well.
    conf = review_confinement(Config(allow_literature_review=True))
    assert conf.allowed_tools == (), conf.allowed_tools
    for tool in ("WebSearch", "WebFetch", "Bash", "Read", "Grep", "Write"):
        assert tool in conf.disallowed_tools, tool
    assert conf.model == "sonnet" and conf.enforced is True

    # A READER MAY NOT SIGN THE HARNESS'S NAME.
    proposals, notes, meta = parse_matches(json.dumps({"candidates_read": 3, "matches": [
        {"candidate_id": "c1", "relation": "LIKELY_DIRECT_PREDECESSOR",
         "candidate_quote": "We learn optical flow end to end with a network.",
         "overlap": "same contribution", "binding_basis": "reviewer_reading",
         "authority": "STRUCTURALLY_BOUND_PRIOR_ART", "prior_art_established": True,
         "novelty_failure": True, "severity": "FATAL", "chronology": "PREDATES_CUTOFF",
         "made_up_key": 1}]}))
    assert meta["harness_keys_stripped"] == 5 and meta["unknown_keys_dropped"] == 1
    assert set(proposals[0]) == set(_MATCH_KEYS), proposals[0]
    assert "authority" not in proposals[0] and "severity" not in proposals[0]

    # AN UNKNOWN RELATION IS NOT RENAMED INTO A CONCERNING ONE.
    weird, _, _ = parse_matches(json.dumps({"matches": [
        {"candidate_id": "c1", "relation": "DEFINITELY_STOLEN"}]}))
    assert weird[0]["relation"] == "INSUFFICIENT_EVIDENCE"

    # AN UNKNOWN QUERY FAMILY IS OUTSIDE THE PROTOCOL AND IS DROPPED.
    qs, _ = parse_queries(json.dumps({"queries": [
        {"family": "EXACT_METHOD_NAME", "query": "flownet optical flow"},
        {"family": "VIBES", "query": "anything"}]}))
    assert qs == [("EXACT_METHOD_NAME", "flownet optical flow")], qs

    # END TO END, WITH NO SUBPROCESS: a proposal naming a real candidate binds, and one
    # naming a candidate this harness never retrieved is dropped whole.
    doc = PaperDoc(paper_id="p", title="FlowNet", body_end_section_idx=1, sections=[
        Section(section_idx=0, title="Abstract", text=(
            "We introduce FlowNet for optical flow. "
            "This is the first method to learn optical flow end to end.")),
        Section(section_idx=1, title="References", text="[1] Someone. Something. 2011."),
    ])
    claim = next(c for c in literature.novelty_claims(doc)
                 if c.claim_kind == "NOVELTY_MARKER")
    work = WorkIdentity(doi="10.9/earlier", title="End-to-end flow", date="2013-03-01",
                        date_source="arxiv_submission_v1",
                        abstract="We learn optical flow end to end with a network.")
    work.canonical_id = literature.canonical_id(work)
    block, by_id = candidates_block([work])
    assert "[c1]" in block and work.abstract in block
    cutoff = literature.derive_cutoff({"arxiv_submission_v1": "2015-04-26"})
    facts = adjudicate(doc, claim, proposals, by_id, cutoff=cutoff)
    assert len(facts) == 1 and facts[0].authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN"
    assert facts[0].binding_basis == "reviewer_reading" and not facts[0].establishes_novelty
    assert adjudicate(doc, claim, [{"candidate_id": "c99", "relation":
                                    "LIKELY_DIRECT_PREDECESSOR"}], by_id) == []

    # A CLOSED GATE RECORDS THE FAILURE RATHER THAN VANISHING.
    body, rec = call(Config(allow_literature_review=False), "prompt")
    assert body == "" and rec["failure"] == "gate_or_command"
    print("harness.literature_driver self-check ok")
