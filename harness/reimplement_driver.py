"""The fourth driver — writes a GOVERNED RECONSTRUCTION from a paper's own specification,
when `harness.reimplement.assess` has already established that the paper says enough.

WHY THIS EXISTS, and what it replaces. Before this module, the only channel for PATH B
was `stages.probe.write_reimplementation_prompt`: a brief written to disk, an implementer
(human or model) fills `runs/<pid>/spec.json` by hand, and `run.py accept` seals it under
the existing `driver` provenance — the SAME provenance a human-authored mechanism script
for a `FOCUSED_VALIDATION_EXPERIMENT` uses, with no check that a REIMPLEMENTATION
specifically stayed inside what the paper specified. The brief asked the implementer
nicely not to invent anything; nothing verified that they hadn't.

Decision 1 closes that gap with a NEW provenance, `reimpl_exec` (see `harness.provenance`),
admissible only when every REQUIRED ingredient the paper supplies is bound to both a paper
locator and a VERIFIED implementation locator — `conformance()` below computes that
binding, and it does not trust the delegate's own say-so: `impl_quote` is re-checked
against the script text this call actually returned, the same discipline
`claims.verify_evidence` applies to a lens's citation. A binding the delegate claimed but
that is not literally present in the returned script is not bound.

The manual channel (`write_reimplementation_prompt` / `run.py accept`, routing through
`driver`) is UNCHANGED and still exists for an operator who wants to review and hand-seal a
reconstruction themselves — this module adds an AUTOMATED, machine-verified path
alongside it, exactly as `audit_driver`/`grade_driver`/`verdict_driver` sit alongside the
manual `audit/prompts/<lens>.md` channel for their own phases. It does not touch `driver`,
`repo_exec`, `backends.authorize`'s repo-execution branch, or `experiment_id` at all.

WRITING is not RUNNING. This module's subprocess only produces TEXT — a script as a JSON
string, never executed by this module — mirroring `verdict_driver`'s zero-tool, bare
confinement. Whether the SEALED result may ever actually run is a completely separate
question, decided by `backends.authorize`'s `reimpl_exec` branch and gated by
`SH_ALLOW_REIMPLEMENTATION_EXEC` plus the same isolation floor `SH_ALLOW_REPO_EXEC`
requires (`harness.isolation.sufficient_for_repo_exec`) — decision 10: a model-authored
script nobody reviewed is third-party code, and no capability that increases third-party
code execution lands without that boundary enforced.

`python -m harness.reimplement_driver` runs the self-check.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import delegation, reviewer_cli, sealing, state
from .artifacts import ReimplementationBinding, ReimplementationConformance, ReimplementationReadiness
from .config import Config
from .prompts import reimplement as RP
from .reviewer_cli import (KNOWN_TOOLS, Confinement, _kill_tree, denied_tools,
                           envelope_provenance, operator_confinement, unwrap_envelope,
                           write_pinned_settings)

# Every writer a validated reconstruction can carry: this module's own subprocess, a human
# `accept_reimplementation` call, plus every mode `harness.delegation` admits — the same
# vocabulary `verdict_driver.WRITERS` reads off, for the same reason: a mode sealed here
# must not be refused by `load_accepted`.
WRITERS = ("reimplement_driver",) + tuple(
    __import__("harness.delegation", fromlist=["WRITTEN_BY"]).WRITTEN_BY.values())

# Only these five carry a REQUIRED paper locator (`hyperparameters`/`architecture`/
# `preprocessing` are optional — see `harness.reimplement.INGREDIENTS`) and only these
# five are what decision 1 requires bound before a reconstruction may run.
REQUIRED_KINDS = ("method", "training", "dataset", "metric", "comparison_target")

_LINE_REF = re.compile(r"^lines?\s+(\d+)(?:\s*[-:]\s*(\d+))?$", re.I)
_FUNCTION_REF = re.compile(r"^(?:function|def)\s+([A-Za-z_]\w*)$", re.I)


def _implementation_locator_matches(script: str, impl_ref: str, impl_quote: str) -> bool:
    """Verify the quote at the claimed implementation locator, not anywhere in the file."""
    if not impl_ref or not impl_quote:
        return False
    lines = script.splitlines()
    m = _LINE_REF.fullmatch(impl_ref.strip())
    if m:
        start = int(m.group(1))
        end = int(m.group(2) or start)
        if start < 1 or end < start or end > len(lines):
            return False
        return impl_quote in "\n".join(lines[start - 1:end])
    m = _FUNCTION_REF.fullmatch(impl_ref.strip())
    if m:
        try:
            import ast
            tree = ast.parse(script)
            node = next((n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name == m.group(1)), None)
            if node is None:
                return False
            end = int(getattr(node, "end_lineno", node.lineno))
            return impl_quote in "\n".join(lines[node.lineno - 1:end])
        except (SyntaxError, StopIteration):
            return False
    return False


class ReimplementationDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return delegation.resolve_model(cfg.reimplementation_model, RP.ROLE_SPEC.get("model", ""))


def reimplementation_confinement(cfg: Config, *, settings_sha256: str = "") -> Confinement:
    """Zero tools, no directories, `--bare`. This call only produces TEXT — see the module
    docstring — so there is nothing for a tool to do and nothing for a directory to leak;
    the paper text it needs is entirely inside the prompt, on disk, checkable by a human."""
    if cfg.reimplementation_cmd.strip():
        return operator_confinement("reimplementer")
    return Confinement(
        role="reimplementer", allowed_tools=(), disallowed_tools=denied_tools(()),
        add_dir=(), model=role_model(cfg), restricted=True, strict_mcp=True, bare=True,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "") -> str:
    cfg = cfg or Config()
    exe = delegation.resolve_reviewer_exe(exe, cfg.reviewer_exe)
    if not exe:
        return ""
    extra = reimplementation_confinement(Config(
        reimplementation_cmd="", reimplementation_model=cfg.reimplementation_model)).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, *, settings: str = "") -> str:
    return cfg.reimplementation_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe, settings=settings)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_reimplementation_driver:
        return False, "reimplementation-driver gate is closed; set SH_ALLOW_REIMPLEMENTATION_DRIVER=1"
    return delegation.check_command(resolve_cmd(cfg), "SH_REIMPLEMENTATION_CMD")


def _invoke_fresh(cfg: Config, prompt_text: str, timeout_s: int) -> tuple[str, dict] | None:
    """Run one text-only delegate in a fresh process and empty working directory."""
    with tempfile.TemporaryDirectory(prefix="sh-reimpl-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-reimpl-policy-"))
        sandbox = Path(tempfile.mkdtemp(prefix="sh-reimpl-sandbox-"))
        conf_conn = reimplementation_confinement(cfg)
        settings_path = ""
        try:
            if conf_conn.enforced:
                sp, sha = write_pinned_settings(policy_dir, conf_conn.disallowed_tools)
                settings_path = str(sp)
                conf_conn = dataclasses.replace(conf_conn, settings_sha256=sha)
            cmd = (resolve_cmd(cfg, settings=settings_path)
                   .replace("{prompt}", str(prompt)).replace("{out}", str(out)))
            group_kwargs: dict = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if sys.platform == "win32" else {"start_new_session": True})
            try:
                proc = subprocess.Popen(
                    cmd, shell=True, cwd=str(sandbox), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace", **group_kwargs)
            except OSError:
                return None
            try:
                proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.communicate()
                return None
            if not out.exists():
                return None
            raw = out.read_text(encoding="utf-8")
            return raw, {
                "command": cmd, "returncode": proc.returncode,
                "tool_policy": conf_conn.summary(),
                "tool_policy_detail": conf_conn.policy(),
                "raw_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
            }
        except OSError:
            return None
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            shutil.rmtree(policy_dir, ignore_errors=True)


def _verification_brief(readiness: ReimplementationReadiness, script: str,
                        bindings: list[dict]) -> str:
    return f"""{RP.SECURITY}

You are the independent verifier, not the generator. Check the proposed reconstruction
against every paper-owned required ingredient below. Reject any invented method,
training, dataset, metric, or comparison detail, and reject a locator whose quoted text
is not present at that exact implementation location.

=== PAPER INGREDIENTS ===
{ingredients_table(readiness)}

=== PROPOSED SCRIPT ===
```python
{script}
```

=== PROPOSED BINDINGS ===
{json.dumps(bindings, ensure_ascii=False)}

Print only JSON:
{{"approved": true_or_false,
  "approved_kinds": ["method","training","dataset","metric","comparison_target"],
  "notes": "short reason"}}
"""


def _parse_verification(text: str) -> tuple[bool, str]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return False, "verifier returned no JSON object"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return False, f"verifier JSON invalid: {e}"
    kinds = {str(x) for x in (data.get("approved_kinds") or [])}
    approved = bool(data.get("approved")) and kinds == set(REQUIRED_KINDS)
    return approved, str(data.get("notes") or "")


def ingredients_table(readiness: ReimplementationReadiness) -> str:
    """The paper's own words for every REQUIRED ingredient, as a markdown table — the same
    shape `stages.probe.write_reimplementation_prompt` already builds for the manual brief,
    factored out so both channels show the delegate identical evidence."""
    rows = ["| ingredient | found at | the paper's own words |", "|---|---|---|"]
    for i in readiness.ingredients:
        if not i.required:
            continue
        quote = (i.quote or "").replace("|", "\\|")[:200]
        rows.append(f"| {i.kind} | {('`' + i.ref + '`') if i.ref else '**MISSING**'} | {quote} |")
    return "\n".join(rows)


def build_brief(readiness: ReimplementationReadiness, *, paper_title: str = "", claim: str = "",
                table_ref: str = "", claimed_cell_value: str = "", paper_text: str = "") -> str:
    return RP.build(paper_title, claim, table_ref, claimed_cell_value,
                    ingredients_table(readiness), paper_text)


def parse_reimplementation_report(text: str) -> tuple[str, list[dict], str, dict]:
    """(script, bindings, notes, meta) — the raw JSON, unwrapped and validated.

    Unknown keys are dropped, exactly as `grade_driver`/`verdict_driver` do: a field this
    schema does not name is not carried into anything sealed."""
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {}}
    if envelope and envelope.get("is_error"):
        raise ReimplementationDriverError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise ReimplementationDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise ReimplementationDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("script") or "").strip():
        raise ReimplementationDriverError("output JSON has no non-empty 'script'")
    bindings = data.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
    clean_bindings = [b for b in bindings if isinstance(b, dict) and b.get("kind")]
    return str(data["script"]), clean_bindings, str(data.get("notes") or ""), meta


def conformance(readiness: ReimplementationReadiness, script: str,
                bindings: list[dict], *, generated_by: str = "",
                verified_by: str = "") -> ReimplementationConformance:
    """Pure. Pairs each REQUIRED ingredient's PAPER locator (from `readiness`, already
    established) with the driver's IMPLEMENTATION locator, and — the harness's own check,
    never the delegate's — verifies `impl_quote` occurs verbatim in `script`.

    `readiness.established` must already be True before this is called (eligibility is a
    precondition, not something this function re-derives); a caller that skips that check
    is asking whether an implementation stayed inside a brief that was never checked
    complete, which is a different and weaker question.
    """
    by_kind = {i.kind: i for i in readiness.ingredients}
    driver_by_kind = {b.get("kind"): b for b in bindings}
    out: list[ReimplementationBinding] = []
    unbound: list[str] = []
    for kind in REQUIRED_KINDS:
        paper = by_kind.get(kind)
        paper_ref = paper.ref if paper else ""
        paper_quote = paper.quote if paper else ""
        d = driver_by_kind.get(kind) or {}
        impl_ref = str(d.get("impl_ref") or "").strip()
        impl_quote = str(d.get("impl_quote") or "").strip()
        verified = _implementation_locator_matches(script, impl_ref, impl_quote)
        bound = bool(paper_ref) and bool(impl_ref) and verified
        if not bound:
            unbound.append(kind)
        out.append(ReimplementationBinding(
            kind=kind, paper_ref=paper_ref, paper_quote=paper_quote,
            impl_ref=impl_ref, impl_quote=impl_quote, verified=verified, bound=bound))
    generator = (generated_by or "").strip()
    verifier = (verified_by or "").strip()
    independent = bool(generator and verifier and generator != verifier)
    established = not unbound and independent
    if established:
        reason = ("every required ingredient is bound to both a paper locator and a "
                  "verified implementation locator, and a separately attributed verifier "
                  "approved the proposed bindings; this reconstruction may reconcile "
                  "against the cited cell")
    elif not unbound and not independent:
        reason = ("all proposed snippets are present, but generated code cannot certify "
                  "its own scientific conformance. A separately attributed verifier must "
                  "approve the method, training, dataset, metric and comparison bindings")
    else:
        reason = ("not bound: " + ", ".join(unbound) + " — the returned script does not "
                  "verifiably realize " + ("this ingredient" if len(unbound) == 1 else
                                            "these ingredients") + ", so no verdict may be "
                  "drawn from running it")
    return ReimplementationConformance(
        established=established, bindings=out, unbound=unbound, reason=reason,
        generated_by=generator, verified_by=verifier,
        independently_verified=independent)


def _paths(cfg: Config, pid: str, target_id: str) -> tuple[Path, Path]:
    out = (state.project_dir(cfg, pid) / "runs" / pid / "reimplementation" /
           f"{target_id or 'default'}.json")
    return out, out.with_suffix(".driver.json")


def _seal(cfg: Config, pid: str, target_id: str, script: str,
         conf: ReimplementationConformance, record: dict) -> dict:
    """`record` already carries whatever `delegation.provenance_record` produced —
    `accept_reimplementation` builds it explicitly, and `run()`'s own inline record states
    its mode directly, since it IS a `CLI_SUBPROCESS` call (two of them: a generator and a
    separately attributed verifier). See `verdict_driver._seal` for why
    `mode`/`reviewer`/`tool_policy` are re-derived FROM `record` rather than recomputed a
    second, independent time. The payload is a plain wrapper dict, never a pydantic
    `model_dump()` — `sealing.seal` takes `payload: dict` for exactly this reason.
    """
    out, _sidecar = _paths(cfg, pid, target_id)
    return sealing.seal(
        out, {"script": script, "conformance": conf.model_dump()},
        mode=record.get("delegation_mode", "MANUAL"),
        reviewer=record.get("reviewer") or record.get("reader", ""),
        tool_policy=record.get("tool_policy", "unrecorded"),
        extra={**record, "paper_id": pid, "target_id": target_id,
              "established": conf.established},
    )


def accept_reimplementation(cfg: Config, pid: str, target_id: str, raw: str,
                            readiness: ReimplementationReadiness, *, reviewer: str = "",
                            generated_by: str = "",
                            tool_policy: str = "unrecorded", mode: str = "MANUAL") -> dict:
    """Seal a reconstruction produced OUTSIDE this module's own subprocess — the same
    manual-acceptance channel `stages.audit.accept_lens` / `grade_stage.accept_grade` /
    `verdict_driver.accept_verdict` already give their phases, completing the set for
    this one."""
    script, bindings, _notes, _meta = parse_reimplementation_report(raw)
    conf = conformance(readiness, script, bindings, generated_by=generated_by,
                       verified_by=reviewer)
    from . import delegation
    prov = delegation.provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy)
    return _seal(cfg, pid, target_id, script, conf, {
        "written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
        "reviewer": prov["reviewer"], "generated_by": generated_by,
        "independent_verification": conf.independently_verified,
        "tool_policy": prov["tool_policy"],
        "isolation_claim": prov["isolation_claim"],
        "tool_policy_provable": prov["tool_policy_provable"]})


def load_accepted(cfg: Config, pid: str, target_id: str, *,
                  prompt_sha256: str = "") -> tuple[str, ReimplementationConformance] | None:
    """A sealed (script, conformance) for `pid`/`target_id`, or None. Verifies the seal
    before trusting it, exactly as `verdict_driver.load_accepted` does."""
    out, sidecar = _paths(cfg, pid, target_id)
    ok, _why = sealing.verify_seal(out, accepted_writers=WRITERS)
    if not ok:
        return None
    try:
        rec = state.read_json(sidecar)
        recorded_prompt = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        data = state.read_json(out)
        conf = ReimplementationConformance(**data.get("conformance") or {})
        if conf.established and not (rec.get("independent_verification")
                                     and conf.independently_verified
                                     and conf.generated_by and conf.verified_by
                                     and conf.generated_by != conf.verified_by):
            return None
        return str(data.get("script") or ""), conf
    except Exception:
        return None


def run(cfg: Config, prompt_text: str, readiness: ReimplementationReadiness, *,
       timeout_s: int = 0, pid: str = "", target_id: str = "") -> tuple[str, ReimplementationConformance] | None:
    """Best-effort. Returns None on ANY failure, exactly like `verdict_driver.run` — this
    driver never blocks a review and its absence never produces a placeholder result.

    Given `pid`, the result is SEALED and attributable, and the next call finds it via
    `load_accepted` instead of paying for a fresh one.
    """
    ok, _why = available(cfg)
    if not ok:
        return None
    timeout_s = timeout_s or cfg.reimplementation_timeout_s
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    started = time.time()
    generated = _invoke_fresh(cfg, prompt_text, timeout_s)
    if generated is None:
        return None
    raw, gen_record = generated
    try:
        script, bindings, notes, meta = parse_reimplementation_report(raw)
    except ReimplementationDriverError:
        return None

    checked = _invoke_fresh(
        cfg, _verification_brief(readiness, script, bindings), timeout_s)
    approved = False
    verifier_note = "independent verifier did not complete"
    verify_record: dict = {}
    if checked is not None:
        verify_raw, verify_record = checked
        approved, verifier_note = _parse_verification(verify_raw)
    conf = conformance(
        readiness, script, bindings, generated_by="reimplementation_generator",
        verified_by=("reimplementation_verifier" if approved else ""))
    if pid:
        try:
            _seal(cfg, pid, target_id, script, conf, {
                "written_by": "reimplement_driver",
                "delegation_mode": "CLI_SUBPROCESS",
                "reviewer": "reimplementation_verifier" if approved else "",
                "generated_by": "reimplementation_generator",
                "independent_verification": conf.independently_verified,
                "generator": gen_record, "verifier": verify_record,
                "seconds": round(time.time() - started, 1),
                "prompt_sha256": prompt_sha, "notes": notes,
                "verifier_notes": verifier_note,
                "envelope": meta.get("envelope") or {},
            })
        except OSError:
            pass
    return script, conf


if __name__ == "__main__":       # self-check: python -m harness.reimplement_driver
    import inspect as _inspect
    import tempfile as _tf

    from .artifacts import ReimplementationIngredient

    cfg = Config.load()
    closed = Config(allow_reimplementation_driver=False)
    assert available(closed)[0] is False
    assert run(closed, "prompt", ReimplementationReadiness()) is None, (
        "gate closed must degrade to None, never raise")

    def _readiness(established=True) -> ReimplementationReadiness:
        ings = [
            ReimplementationIngredient(kind="method", required=True, present=True,
                                       ref="s0", quote="we define the objective as a sum"),
            ReimplementationIngredient(kind="training", required=True, present=True,
                                       ref="s0", quote="Adam for 30 epochs at lr=1e-3"),
            ReimplementationIngredient(kind="dataset", required=True, present=True,
                                       ref="s0", quote="the CIFAR-100 dataset"),
            ReimplementationIngredient(kind="metric", required=True, present=True,
                                       ref="s0", quote="report accuracy"),
            ReimplementationIngredient(kind="comparison_target", required=True, present=True,
                                       ref="T0", quote="Table 1"),
        ]
        return ReimplementationReadiness(established=established, ingredients=ings)

    # --- parsing --------------------------------------------------------------------
    good = json.dumps({
        "script": "import argparse\n# method: sum objective\ndef train():\n    pass\n"
                  "print('SH_METRIC arm=a seed=0 value=0.9')",
        "bindings": [
            {"kind": "method", "impl_ref": "line 2", "impl_quote": "# method: sum objective"},
            {"kind": "training", "impl_ref": "line 3", "impl_quote": "def train():"},
            {"kind": "dataset", "impl_ref": "line 1", "impl_quote": "import argparse"},
            {"kind": "metric", "impl_ref": "line 5", "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
            {"kind": "comparison_target", "impl_ref": "line 5", "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
        ],
        "notes": "",
    })
    script, bindings, notes, _meta = parse_reimplementation_report(good)
    assert "SH_METRIC" in script and len(bindings) == 5 and notes == ""
    for bad in ("", "no json", '{"bindings":[]}', '{"script":""}'):
        try:
            parse_reimplementation_report(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ReimplementationDriverError:
            pass

    # --- conformance: THE REGRESSION this module exists to fix -----------------------
    # A binding the delegate CLAIMS but that does not literally appear in the returned
    # script must never be trusted — that is the whole difference between this path and
    # the old unchecked `driver` seal.
    r = _readiness()
    fully_bound = conformance(r, script, bindings,
                              generated_by="generator", verified_by="verifier")
    assert fully_bound.established, fully_bound.unbound
    assert fully_bound.unbound == []
    assert all(b.verified and b.bound for b in fully_bound.bindings)

    lying = json.loads(good)
    lying["bindings"][0]["impl_quote"] = "this text is not anywhere in the script"
    _, lying_bindings, _, _ = parse_reimplementation_report(json.dumps(lying))
    forged = conformance(r, script, lying_bindings,
                         generated_by="generator", verified_by="verifier")
    assert not forged.established
    assert "method" in forged.unbound
    method_binding = next(b for b in forged.bindings if b.kind == "method")
    assert method_binding.impl_ref == "line 2"          # the delegate's claim is RECORDED
    assert not method_binding.verified and not method_binding.bound  # but not TRUSTED

    # a binding entirely absent from the delegate's report is unbound, not a crash
    missing = conformance(r, script, [b for b in bindings if b["kind"] != "dataset"],
                          generated_by="generator", verified_by="verifier")
    assert "dataset" in missing.unbound and not missing.established

    # a paper ingredient that was never established (readiness refused it) can never
    # become bound no matter what the delegate claims about it
    partial_readiness = ReimplementationReadiness(established=False, ingredients=[
        ReimplementationIngredient(kind="method", required=True, present=False, ref="", quote="")])
    no_paper_ref = conformance(partial_readiness, script, bindings,
                               generated_by="generator", verified_by="verifier")
    method_b = next(b for b in no_paper_ref.bindings if b.kind == "method")
    assert method_b.paper_ref == "" and not method_b.bound, (
        "no paper locator means no binding, regardless of the implementation side")

    # --- ingredients_table / build_brief ----------------------------------------------
    table = ingredients_table(r)
    assert "method" in table and "comparison_target" in table and "MISSING" not in table
    incomplete = _readiness(established=False)
    incomplete.ingredients[0].present = False
    incomplete.ingredients[0].ref = ""
    assert "**MISSING**" in ingredients_table(incomplete)
    brief = build_brief(r, paper_title="T", claim="c", table_ref="T0:r0:c0",
                        claimed_cell_value="91.4", paper_text="body")
    assert "T0:r0:c0" in brief and "91.4" in brief and "INDEPENDENT_REIMPLEMENTATION" in brief

    # --- seal / load roundtrip, and attribution ---------------------------------------
    with _tf.TemporaryDirectory() as _td:
        _cfg = Config(projects_dir=Path(_td), allow_reimplementation_driver=True)
        assert load_accepted(_cfg, "p", "t1") is None
        _rec = accept_reimplementation(
            _cfg, "p", "t1", good, r, reviewer="alice", generated_by="builder")
        assert _rec["written_by"] in WRITERS and _rec["established"] is True
        got = load_accepted(_cfg, "p", "t1")
        assert got is not None
        got_script, got_conf = got
        assert got_script == script and got_conf.established
        # a different target_id is a different seal
        assert load_accepted(_cfg, "p", "t2") is None
        # a nonconformant submission seals HONESTLY as not established, never silently
        # promoted
        lying_rec = accept_reimplementation(
            _cfg, "p", "t3", json.dumps(lying), r,
            reviewer="alice", generated_by="builder")
        assert lying_rec["established"] is False
        lying_loaded = load_accepted(_cfg, "p", "t3")
        assert lying_loaded is not None
        assert not lying_loaded[1].established
        # an edited artifact is not the artifact that was sealed
        out_path, _ = _paths(_cfg, "p", "t1")
        state.write_json(out_path, {"script": "tampered", "conformance": {}})
        assert load_accepted(_cfg, "p", "t1") is None

    # --- confinement -------------------------------------------------------------------
    _EXE = "/nonexistent/claude"
    _built = default_cmd(Config(), exe=_EXE, settings="/policy/s.json")
    for _flag in ("--restricted", "--strict-mcp-config", "--settings", "--bare",
                  "--disallowedTools", "--model", "--output-format json"):
        assert _flag in _built, (_flag, _built)
    assert '--allowedTools ""' in _built and "--add-dir" not in _built
    _c = reimplementation_confinement(Config())
    assert _c.allowed_tools == () and set(_c.disallowed_tools) == set(KNOWN_TOOLS)
    assert _c.bare is True and _c.model and _c.enforced is True
    assert reimplementation_confinement(
        Config(reimplementation_cmd="x {prompt} {out}")).enforced is False
    assert role_model(Config(reimplementation_model="haiku")) == "haiku"
    assert role_model(Config(reimplementation_model="")) == RP.ROLE_SPEC["model"]

    # --- this module WRITES and never RUNS what it writes; never touches the modules
    # that decide whether a sealed reconstruction may execute ---------------------------
    import ast as _ast

    _tree = _ast.parse(_inspect.getsource(sys.modules[__name__]))
    _imported: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.ImportFrom):
            _imported.add(_node.module or "")
            _imported.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
    for _forbidden in ("backends", "local_exec", "stages", "planner", "grading", "taxonomy"):
        assert _forbidden not in _imported, _forbidden
    assert "stdin=subprocess.DEVNULL" in _inspect.getsource(_invoke_fresh)

    print(json.dumps({"self_check": "ok",
                      "gate_default": cfg.allow_reimplementation_driver}, indent=2))
