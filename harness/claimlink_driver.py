"""Delegating the claim-link reading, and sealing what comes back.

`python -m harness.claimlink_driver` runs the self-check.

The lightest of the reviewer drivers after `verdict_driver`, and for the same reason: one
best-effort call per paper, never retried, and its failure changes nothing. With no links
the harness behaves exactly as it did before this channel existed — `materiality` and
`discovery` are untouched, and `claimgraph` simply has no `SUPPORTED_BY` edges. That is
the property that makes a new model channel on the always-on path acceptable: it can only
ADD a dependency the harness could not otherwise establish, and never remove or alter one.

What is sealed is the reader's PROPOSALS, verified. `harness/claimlink.py` re-mints every
claim quotation, requires it to land in the Abstract or the Conclusion, re-resolves every
evidence address against the quotation given for it, and re-derives the arithmetic — so
the file on disk carries the harness's verdict on each pairing, not the reader's.
Refusals are kept beside the acceptances, because a reader that proposed twelve pairings
of which four did not resolve is a different reader from one that proposed eight.
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

from . import claimlink, delegation, reviewer_cli, sealing, state
from .artifacts import ClaimLink, ClaimLinkSet, PaperDoc
from .config import Config
from .prompts import claimlink as CP
from .reviewer_cli import (Confinement, _kill_tree, denied_tools, envelope_provenance,
                           operator_confinement, unwrap_envelope, write_pinned_settings)

# Same reasoning as `verdict_driver.WRITERS`: read off the delegation vocabulary so a mode
# cannot be sealed by one path and refused by another, which presents as "no links exist"
# for a paper that has them.
WRITERS = ("claimlink_driver",) + tuple(delegation.WRITTEN_BY.values())


class ClaimLinkDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return delegation.resolve_model(cfg.claimlink_model, CP.ROLE_SPEC.get("model", ""))


def link_confinement(cfg: Config, *, settings_sha256: str = "",
                     pdf_dir: str = "") -> Confinement:
    """Read-only over the papers directory, nothing else.

    `Read` is granted for one reason: extraction garbles tables, and a reader asked to
    quote a cell verbatim needs to be able to check the printed page when the recovered
    text is mangled. It cannot reach the network, this harness's source, or any other
    paper — and whatever it sees in the PDF still has to be quoted at an address the
    harness re-resolves, so looking is never itself evidence.
    """
    if cfg.claimlink_cmd.strip():
        return operator_confinement("claim-link reader")
    allowed = ("Read",)
    return Confinement(
        role="claimlink", allowed_tools=allowed, disallowed_tools=denied_tools(allowed),
        add_dir=((pdf_dir,) if pdf_dir else ()), model=role_model(cfg),
        restricted=True, strict_mcp=True, bare=False,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "",
                pdf_dir: str = "") -> str:
    cfg = cfg or Config()
    exe = delegation.resolve_reviewer_exe(exe, cfg.reviewer_exe)
    if not exe:
        return ""
    extra = link_confinement(Config(claimlink_cmd="",
                                    claimlink_model=cfg.claimlink_model),
                             pdf_dir=pdf_dir).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, *, settings: str = "", pdf_dir: str = "") -> str:
    return cfg.claimlink_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe,
                                                    settings=settings, pdf_dir=pdf_dir)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_claim_links:
        return False, ("claim-link gate is closed: SH_ALLOW_CLAIM_LINKS is not set. With "
                       "it closed the harness behaves exactly as it did before this "
                       "channel existed — no link is established and materiality falls "
                       "back to its own structural rule")
    return delegation.check_command(resolve_cmd(cfg), "SH_CLAIMLINK_CMD")


def parse_links(text: str) -> tuple[list[ClaimLink], str, dict]:
    """(proposals, notes, meta). Proposals only — nothing here is accepted yet.

    The reader's own `accepted`, `refusal`, `numeric_relation` and `verified_observation`
    are dropped rather than carried, exactly as `audit_driver.strip_harness_keys` drops
    the harness-owned half of a finding. A proposal that arrived asserting it had been
    verified would be a proposal signing the harness's name.
    """
    outer = (text or "").strip()
    if not outer:
        raise ClaimLinkDriverError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                  "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise ClaimLinkDriverError(f"the reader reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ClaimLinkDriverError(
            f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise ClaimLinkDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        raise ClaimLinkDriverError("output JSON has no 'links' list")
    out: list[ClaimLink] = []
    for i, row in enumerate(data["links"]):
        if not isinstance(row, dict):
            meta["unknown_keys_dropped"] += 1
            continue
        for key in HARNESS_OWNED_LINK_KEYS:
            if key in row:
                meta["harness_keys_stripped"] += 1
        meta["unknown_keys_dropped"] += sum(
            1 for k in row if k not in _PROPOSAL_KEYS and k not in HARNESS_OWNED_LINK_KEYS)
        out.append(ClaimLink(
            link_id=f"L{i + 1:02d}",
            claim_quote=str(row.get("claim_quote") or "").strip(),
            evidence_ref=str(row.get("evidence_ref") or "").strip(),
            evidence_quote=str(row.get("evidence_quote") or "").strip(),
            rationale=str(row.get("rationale") or "").strip()[:600]))
    return out, str(data.get("notes") or "").strip()[:2000], meta


# The reader supplies two quotations, an address and a reason. Everything else on the
# type is the harness's (invariant 2), and a value arriving under one of these names is
# counted as an attempted forgery rather than merged in.
_PROPOSAL_KEYS = ("claim_quote", "evidence_ref", "evidence_quote", "rationale")
HARNESS_OWNED_LINK_KEYS = ("accepted", "refusal", "numeric_relation", "claim_ref",
                           "claim_section_idx", "claim_value", "evidence_value",
                           "verified_observation", "link_id",
                           # A reader asserting its own link's AUTHORITY is the forgery
                           # that matters most: `STRUCTURALLY_BOUND_LINK` is the only
                           # class that could ever inform a decision, and it is the
                           # harness's to award from the document.
                           "link_authority", "binding_basis", "binding_checks")


def _paths(cfg: Config, pid: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "links" / f"{pid}.claimlinks.json"
    return out, out.with_suffix(".driver.json")


def seal(cfg: Config, pid: str, linkset: ClaimLinkSet, record: dict) -> dict:
    """Write the verified link set and its sidecar. One writer for both channels.

    `record` already carries whatever `delegation.provenance_record` produced —
    `accept` builds it explicitly, and `run()`'s own inline record states its mode
    directly, since it IS a `CLI_SUBPROCESS` call. See `verdict_driver._seal` for why
    `mode`/`reviewer`/`tool_policy` are re-derived FROM `record` rather than recomputed a
    second, independent time.
    """
    out, _sidecar = _paths(cfg, pid)
    return sealing.seal(
        out, linkset.model_dump(),
        mode=record.get("delegation_mode", "MANUAL"),
        reviewer=record.get("reviewer") or record.get("reader", ""),
        tool_policy=record.get("tool_policy", "unrecorded"),
        extra={**record, "paper_id": pid, "proposed": linkset.proposed,
              "accepted": linkset.accepted, "refusals": linkset.refusals},
    )


def accept(cfg: Config, pid: str, doc: PaperDoc, raw: str, *, reader: str = "",
           tool_policy: str = "unrecorded", mode: str = "MANUAL") -> ClaimLinkSet:
    """Verify and seal a reading produced OUTSIDE this module's subprocess.

    The channel `accept_lens`, `accept_grade` and `accept_verdict` each already provide,
    for the same reason: on a host where the CLI is unreachable, a reading a person or an
    isolated subagent produced must be able to enter through the SAME verification as one
    this module ran, and be distinguishable from it afterwards.
    """
    proposals, notes, meta = parse_links(raw)
    linkset = claimlink.verify_all(doc, proposals)
    linkset = linkset.model_copy(update={"notes": notes})
    prov = delegation.provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    seal(cfg, pid, linkset, {
        "written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
        "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
        "isolation_claim": prov["isolation_claim"],
        "tool_policy_provable": prov["tool_policy_provable"],
        "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
        "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
    })
    return linkset


def load(cfg: Config, pid: str, *, prompt_sha256: str = "") -> ClaimLinkSet | None:
    """A sealed link set for `pid`, or None. Verifies the seal before trusting it.

    `prompt_sha256`, when given, must match what the sidecar recorded. The prompt is a
    function of the paper's own Abstract, Conclusion and recovered evidence, so a
    different hash means the document was re-parsed — and links minted against a previous
    parse point at addresses that may have moved. Refusing them is the same rule
    `extraction_version` states and does not enforce, enforced here because this artifact
    is new and has no archive to invalidate.
    """
    out, sidecar = _paths(cfg, pid)
    ok, _why = sealing.verify_seal(out, accepted_writers=WRITERS)
    if not ok:
        return None
    try:
        rec = state.read_json(sidecar)
        recorded = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded and recorded != prompt_sha256:
            return None
        return ClaimLinkSet(**state.read_json(out))
    except Exception:
        return None


def run(cfg: Config, pid: str, doc: PaperDoc, prompt_text: str, *,
        pdf_dir: str = "") -> ClaimLinkSet | None:
    """One best-effort call. Returns None on ANY failure, and never raises.

    Never raising is the contract that makes this channel safe to put on the always-on
    path. An unreachable CLI, a timeout, a malformed response or an unwritable policy file
    all degrade to NO LINKS, which is exactly the state the harness was in before this
    module existed — not to a placeholder link, and not to a blocked review.
    """
    ok, _why = available(cfg)
    if not ok:
        return None
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="sh-claimlink-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-claimlink-policy-"))
        conf = link_confinement(cfg, pdf_dir=pdf_dir)
        settings_path = ""
        try:
            if conf.enforced:
                sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
                settings_path = str(sp)
                conf = dataclasses.replace(conf, settings_sha256=sha)
        except OSError:
            shutil.rmtree(policy_dir, ignore_errors=True)
            return None
        cmd = (resolve_cmd(cfg, settings=settings_path, pdf_dir=pdf_dir)
               .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
        group_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True})
        sandbox = Path(tempfile.mkdtemp(prefix="sh-claimlink-sandbox-"))
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
                proc.communicate(timeout=cfg.claimlink_timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.communicate()
                return None
            if not out.exists():
                return None
            try:
                proposals, notes, meta = parse_links(out.read_text(encoding="utf-8"))
            except ClaimLinkDriverError:
                return None
            linkset = claimlink.verify_all(doc, proposals).model_copy(
                update={"notes": notes})
            try:
                seal(cfg, pid, linkset, {
                    "written_by": "claimlink_driver", "reader": "",
                    "delegation_mode": "CLI_SUBPROCESS",
                    "command": cmd, "returncode": proc.returncode,
                    "seconds": round(time.time() - started, 1),
                    "tool_policy": conf.summary(), "tool_policy_detail": conf.policy(),
                    "prompt_sha256": prompt_sha,
                    "raw_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                    "envelope": meta.get("envelope") or {},
                    "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
                    "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
                })
            except OSError:
                pass
            return linkset
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.claimlink_driver
    from .artifacts import Section, Table

    assert available(Config(allow_claim_links=False))[0] is False
    assert "SH_ALLOW_CLAIM_LINKS" in available(Config(allow_claim_links=False))[1]
    bad = Config(allow_claim_links=True, claimlink_cmd="reader --in {prompt}")
    assert available(bad)[0] is False, "a template with no {out} must be refused"
    good = Config(allow_claim_links=True, claimlink_cmd="cp {prompt} {out}")
    assert available(good) == (True, "")

    # The confinement: read-only, no network, and the model the role declares.
    conf = link_confinement(Config(allow_claim_links=True))
    assert set(conf.allowed_tools) == {"Read"}, conf.allowed_tools
    for net in ("WebSearch", "WebFetch", "Bash"):
        assert net in conf.disallowed_tools, net
    assert conf.model == "sonnet" and conf.enforced is True
    assert operator_confinement("claim-link reader").enforced is False

    # A reader's own verdict on its own pairing is stripped before anything is verified.
    proposals, notes, meta = parse_links(json.dumps({
        "links": [{"claim_quote": "q", "evidence_ref": "T1:r0:c1", "evidence_quote": "40.2",
                   "rationale": "r", "accepted": True, "numeric_relation": "EQUAL",
                   "verified_observation": "THE HARNESS CONFIRMED THIS",
                   "made_up_key": 1}],
        "notes": "n"}))
    assert len(proposals) == 1 and notes == "n"
    assert proposals[0].accepted is False and proposals[0].verified_observation == ""
    assert proposals[0].numeric_relation == ""
    assert meta["harness_keys_stripped"] == 3, meta
    assert meta["unknown_keys_dropped"] == 1, meta

    for junk in ("", "no json here", '{"nope": 1}', '{"links": "not a list"}'):
        try:
            parse_links(junk)
            raise AssertionError(f"should have refused {junk!r}")
        except ClaimLinkDriverError:
            pass

    # The whole path, sealed and loaded back, through a fake reader.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects", allow_claim_links=True)
        doc = PaperDoc(
            paper_id="p", title="T", n_pages=2,
            sections=[Section(section_idx=0, title="Abstract", page_start=1,
                              text="We reduce peak memory by 40%."),
                      Section(section_idx=1, title="Results", page_start=2,
                              text="Memory falls.")],
            tables=[Table(table_idx=1, page=2, label="1", rows=[["ours", "40.2"]])])
        state.create_project(cfg, "", "T", pid="p")
        payload = json.dumps({"links": [
            {"claim_quote": "We reduce peak memory by 40%.", "evidence_ref": "T1:r0:c1",
             "evidence_quote": "40.2", "rationale": "the headline memory number"},
            {"claim_quote": "Memory falls.", "evidence_ref": "T1:r0:c1",
             "evidence_quote": "40.2", "rationale": "a body sentence"}], "notes": "n"})
        sealed = accept(cfg, "p", doc, payload, reader="self-check")
        assert sealed.proposed == 2 and sealed.accepted == 1, sealed
        assert sealed.refusals == {"claim_not_headline": 1}, sealed.refusals
        back = load(cfg, "p")
        assert back is not None and back.accepted == 1
        assert back.links[0].numeric_relation == "ROUNDS_TO"
        # A file edited after sealing is not the file that was sealed.
        out, _sidecar = _paths(cfg, "p")
        out.write_text(out.read_text(encoding="utf-8").replace("ROUNDS_TO", "EQUAL"),
                       encoding="utf-8")
        assert load(cfg, "p") is None, "the seal must refuse an edited artifact"

    print("harness.claimlink_driver self-check ok")
