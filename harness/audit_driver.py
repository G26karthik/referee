"""Optional S2 autonomy: fill lens JSON by shelling out to a configured reviewer.

This exists because "run the whole pipeline without stopping" is a reasonable thing to
want, and the honest way to provide it is to make the delegation explicit rather than to
pretend S2 was never a judgement stage.

What this module does NOT do is invent an auditor. The harness has no model of its own —
no API key, no cloud SDK, by design — so autonomy here means running whatever command the
operator puts in `SH_AUDIT_CMD` and treating its output as a candidate lens report. Three
consequences follow, and all three are enforced below rather than documented and hoped for:

  1. The gate is off by default (`SH_ALLOW_AUTO_AUDIT`). Running an arbitrary configured
     command as part of `review` is a per-invocation decision, like the S3 exec gates.
  2. Every lens filled this way leaves `audit/<lens>.driver.json` recording the command,
     the exit code and the timestamp. A finding that reaches a report should always be
     traceable to who wrote it.
  3. Output is parsed and structurally validated here, and then re-verified downstream by
     `stages/audit.load_reports` exactly like a human-written file — quotes checked against
     the parsed PDF, cell citations checked against the cell. An auto-filled lens gets no
     benefit of the doubt that a hand-written one would not get.

If the command fails, writes nothing, or writes something that is not a lens report, that
lens stays pending. A failed auto-audit degrades to the normal manual pause; it never
writes a placeholder, because an empty findings list is indistinguishable from "this
paper is clean" once it reaches the report.

WHAT THE FORENSIC AUDIT OF THIS PATH FOUND, and what each addition below exists to stop.
None of it was a bug in the code as written; all of it was a guarantee described in a
docstring and enforced by nothing:

  1. `--allowedTools` is an ALLOW list and denies nothing on its own. What a granted
     `Read` may reach was a property of the CLI's permission mode, which this module
     neither set nor recorded — so "a lens sees only the prompt and one PDF" rested on a
     CLI promise. `confinement()` now emits the four flags that deny (`--restricted`,
     `--strict-mcp-config`, `--settings`, `--disallowedTools`) and names every ungranted
     tool explicitly.
  2. The ambient operator config was inherited wholesale: a user-level `CLAUDE.md`
     reaches every session regardless of cwd, and on the development host the settings
     file carried a permission mode, a model, a `PreToolUse` hook and ten plugins, plus
     four MCP servers including a filesystem one. For a LENS that is noise; for the
     supposedly blinded GRADER it is a blinding leak. `--settings` replaces it with bytes
     this module owns and hashes.
  3. `tool_policy` was two-valued and derived from WHICH TEMPLATE was chosen, not from
     anything observed — an unfalsifiable label. It is now a structured record of the
     flags that were actually passed, and `enforced` is False whenever the operator
     supplied the command line, because this module cannot characterise an argv it did
     not build.
  4. Nothing recorded which model answered. `--output-format text` discards exactly the
     envelope that carries the model, the session id and the cost, so declared panel
     diversity, grader independence and any replay claim were unverifiable after the
     fact. The template now asks for `json`, `unwrap_envelope` reads it, and
     `envelope_provenance` records `envelope_keys` so a missing model is distinguishable
     from a model we read out of the wrong key.
  5. The prompt is regenerated unconditionally every run and was never hashed, so a
     sealed lens file plus an edited `prompts/audit.py` still passed its own provenance
     check: the seal certified an output against a prompt that no longer existed.
     `prompt_sha256` is recorded and a content-addressed copy is kept beside it.
  6. The raw response was kept only on FAILURE. On success only the parsed model was
     written, so any prose the reviewer wrapped its JSON in — a hedge, a note that it
     could not open the PDF — was lost, and the failure path was better instrumented
     than the success path.
  7. `_Base` is `extra="allow"` with every `Finding` field optional, so a lens could
     write `verified_observation="THE HARNESS CONFIRMED THIS"` and `counted_severity`
     into the file this module then content-hashes as authentic. The VERDICT was never at
     risk (`stages/audit._coerce` rebuilds every finding with explicit keyword arguments)
     but the sealed artifact carried a forged machine attestation in the harness's own
     field names. `strip_harness_keys` removes them and the count is recorded.
  8. `stdin` was inherited. A delegated reviewer that decided to ask a question would
     have blocked on the operator's terminal until the timeout — a human checkpoint
     nobody put there on purpose. Every child now gets `stdin=DEVNULL`.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import delegation, failures, reviewer_cli, sealing, state
from .artifacts import Finding, LensReport
from .config import Config
from .prompts import audit as P
from .reviewer_cli import (CODE_RUNNING_TOOLS, KNOWN_TOOLS, READING_TOOLS, Confinement,
                           NonRetryable, RateLimited, _kill_tree, classify_delegated_failure,
                           denied_tools, driver_error, envelope_provenance, keep_prompt_copy,
                           operator_confinement, pinned_settings_json, prompt_fingerprint,
                           prompt_is_unchanged, unwrap_envelope, write_pinned_settings)

# Back-compat re-export: `AuditDriverError` used to be DEFINED here. It now lives in
# `harness/reviewer_cli.py` (as `ReviewerCLIError`, with `AuditDriverError` kept as a
# module-level alias there for the same reason) — this line keeps
# `from harness.audit_driver import AuditDriverError` and `audit_driver.AuditDriverError`
# working for every call site that predates the move
# (`tests/test_dossier_and_suite.py`, `tests/test_caption_and_evidence_ref.py`,
# `tests/test_audit_driver.py`, `tests/test_part_audit_stage.py`).
AuditDriverError = reviewer_cli.AuditDriverError


def lens_confinement(lens: str | None, pdf_dir: str = "", *,
                     settings_sha256: str = "") -> Confinement:
    """The confinement one audit lens runs under, from its own declaration in `prompts.audit`.

    `--add-dir` grants read access to the paper's own directory ONLY, so
    `SOURCE_FIDELITY` in the prompt (open the PDF to settle an extraction ambiguity) is
    possible without opening the rest of the filesystem. A lens with no `pdf_dir` gets no
    directory at all rather than a default one.
    """
    spec = P.LENSES.get(lens or "", {})
    allowed = tuple(spec.get("tools", ("Read",))) if lens else ("Read",)
    return Confinement(
        role=f"lens:{lens}" if lens else "lens:unnamed",
        allowed_tools=allowed, disallowed_tools=denied_tools(allowed),
        add_dir=(pdf_dir,) if pdf_dir else (),
        # PANEL DIVERSITY, actually applied. `prompts.audit.LENSES` has declared a per-lens
        # model since the panel was written and nothing read it for a long time, so all
        # four lenses ran on whatever the CLI defaulted to — four readings from one model,
        # a weaker panel than the design claims, invisible because the key existed.
        model=str(spec.get("model", "")) if lens else "",
        restricted=True, strict_mcp=True,
        # NOT `--bare` for a lens. `--bare` also drops the CLI's own tool-permission
        # scaffolding, and a lens legitimately needs `Read` to work; the ambient
        # `CLAUDE.md` is closed off by `--restricted` + `--settings` instead. The grader
        # and the assessor DO take it, because for them the operator's notes are a
        # blinding leak rather than uncontrolled noise, and they need no tools at all.
        bare=False,
        settings_sha256=settings_sha256, output_format="json",
    )


# --------------------------------------------------------------------------- #
# REPLAY — the fingerprint of the prompt that actually ran
# --------------------------------------------------------------------------- #
# The procedure, written down, because "replayable" is a claim and a claim needs a
# method. Every field it names is written by `run_lens` / `run_candidate` /
# `verdict_driver.run`, and `tests/test_delegation_path.py` asserts that correspondence,
# so this text cannot drift away from the record it describes.
#
# The one thing NOT recoverable from disk is the pinned settings file: it lives in a
# scratch directory that is deleted with the run, deliberately, so that the reviewer's own
# cwd stays empty. Step 3 regenerates it from the module constant and checks the hash,
# which is why `pinned_settings_json` is a pure function of this module plus the deny
# list rather than a checked-in config file that could drift from `denied_tools`.
REPLAY_RECIPE: str = """\
To re-derive one delegated reading by hand, from `<artifact>.driver.json`:

  1. `prompt_sha256` — check the prompt on disk still hashes to it. If it does not, use
     the content-addressed copy at `prompt_copy`; that is the prompt that ran.
  2. `envelope.model_reported` / `envelope.cli_version` / `envelope.session_id` — what
     answered, on which CLI, in which session. `envelope.envelope_keys` says what the CLI
     actually reported, so a blank field is distinguishable from a key read wrongly.
  3. `tool_policy_detail.settings_sha256` — regenerate the pinned settings with
     `audit_driver.pinned_settings_json(tool_policy_detail["disallowed_tools"])` and
     confirm the hash. The file itself is not kept; the bytes are a function of this
     module plus that deny list.
  4. `command` — the fully substituted argv, with `--settings` repointed at the file from
     step 3 and `{prompt}`/`{out}` at paths of your choosing.
  5. `raw_response` / `raw_sha256` — the reviewer's answer exactly as it arrived, before
     validation, including any prose it wrapped the JSON in.
  6. `content_sha256` — the seal over the validated artifact this run promoted.
  7. `harness_keys_stripped` — non-zero means the reviewer wrote into a harness-owned
     field name and the value was removed before the artifact was sealed.

What is still NOT pinned, and cannot be from here: the model's own weights behind a
version alias, and sampling temperature, which this CLI does not expose. So a replay
re-derives the CALL, not the answer.
"""
# Which sidecar fields the recipe above depends on. Named once so the documentation and
# the record are checked against each other rather than both against a reader's memory.
REPLAY_FIELDS: tuple[str, ...] = (
    "prompt_sha256", "prompt_copy", "envelope", "tool_policy_detail", "command",
    "raw_response", "raw_sha256", "content_sha256", "harness_keys_stripped",
)


# --------------------------------------------------------------------------- #
# INVARIANT 2, AT THE ARTIFACT BOUNDARY
# --------------------------------------------------------------------------- #
# Fields whose author is the HARNESS. A lens supplying any of them is not adding
# information, it is signing the harness's name. `stages/audit._coerce` already rebuilds
# every finding with explicit keyword arguments, so the verdict was never reachable this
# way — but `run_lens` writes the parsed model to `audit/<lens>.json` and then
# content-hashes it as authentic, so a forged `verified_observation="THE HARNESS
# CONFIRMED THIS"` and `counted_severity="FATAL"` sat inside a sealed artifact. Anything
# that later trusts that file inherits the forgery.
HARNESS_OWNED_FINDING_KEYS: tuple[str, ...] = (
    "verified_observation", "evidence_class", "scientific_class", "verification_state",
    "calc_class", "origin_consistency", "finding_class", "counted_severity",
    "grade_state", "binding_cap", "derivation", "grader_evidence_class",
    "grader_verified_observation", "evidence_ceiling", "evidence_sources", "grade",
    # Reading-provenance and identity, added with the part/synthesis split. `source_part`
    # says which reading produced a concern and `merged_from` which concerns were folded
    # into it — both facts about how the review was conducted, and both would be a reading
    # describing its own conduct if they came from the file. `cross_section` is derived
    # from whether `additional_evidence` actually verified, so a reading asserting it
    # would be asserting that a second location was checked.
    "source_part", "merged_from", "cross_section",
)
# The same rule one level down. A `Finding` carries `additional_evidence`, each entry of
# which has its own `evidence_class` and `verified_observation`, and those are the harness's
# words for the same reason the top-level pair is: the machine half of a citation must not
# be writable by the thing being checked. `strip_harness_keys` walks into the list, because
# a rule enforced only at the top level is a rule with a nested hole in it.
POINTER_OWNED_KEYS: tuple[str, ...] = ("evidence_class", "verified_observation")
# Report-level, and harness-written for the same reason: `merged_duplicates` counts what
# deduplication folded, which happens after a reading has finished.
HARNESS_OWNED_REPORT_KEYS: tuple[str, ...] = ("merged_duplicates",)
# `evidence_origin` is deliberately ABSENT from that tuple even though `Finding` describes
# it as harness-written. `_coerce` reads the lens's own claimed origin back and compares it
# with the origin derived from the reference's shape, which is the only way
# `origin_consistency == "corrected"` is ever produced. Stripping it would silently turn
# that check off — a mechanism that still runs, still passes, and can no longer fail.
_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)
_LENS_ALLOWED = frozenset(LensReport.model_fields) - frozenset(HARNESS_OWNED_REPORT_KEYS)
_POINTER_ALLOWED = frozenset(("role", "evidence_quote", "evidence_ref"))


def strip_harness_keys(data: dict) -> tuple[dict, int, int]:
    """(clean, n_harness_stripped, n_unknown_dropped) — a lens report reduced to its own words.

    Two counts, not one. A stripped harness key is an ATTEMPTED FORGERY and a dropped
    unknown key is a reviewer being chatty; reporting them as one number would let the
    first hide inside the second. Neither is an error: refusing the lens outright would
    let one stray key discard a whole reading, and the fields are removed rather than
    trusted, so nothing survives to mislead.
    """
    if not isinstance(data, dict):
        return {}, 0, 0
    stripped = unknown = 0
    clean: dict = {}
    for key, value in data.items():
        if key in _LENS_ALLOWED:
            clean[key] = value
        else:
            unknown += 1
    out_findings = []
    for f in (clean.get("findings") or []):
        if not isinstance(f, dict):
            out_findings.append(f)
            continue
        kept: dict = {}
        for key, value in f.items():
            if key in HARNESS_OWNED_FINDING_KEYS:
                stripped += 1
            elif key in _FINDING_ALLOWED:
                kept[key] = value
            else:
                unknown += 1
        sides = kept.get("additional_evidence")
        if isinstance(sides, list):
            clean_sides = []
            for side in sides:
                if not isinstance(side, dict):
                    unknown += 1
                    continue
                keep_side: dict = {}
                for key, value in side.items():
                    if key in POINTER_OWNED_KEYS:
                        stripped += 1
                    elif key in _POINTER_ALLOWED:
                        keep_side[key] = value
                    else:
                        unknown += 1
                clean_sides.append(keep_side)
            kept["additional_evidence"] = clean_sides
        out_findings.append(kept)
    if "findings" in clean:
        clean["findings"] = out_findings
    return clean, stripped, unknown


def default_cmd(lens: str | None = None, pdf_dir: str = "", *, exe: str = "",
                settings: str = "") -> str:
    """The built-in reviewer invocation, or '' when no reviewer can be found.

    The harness still has no model of its own and still holds no key. What it can do is
    notice that the Claude Code CLI is installed on this machine and hand it one lens
    prompt at a time. That is the same delegation `SH_AUDIT_CMD` always described, with
    the harness supplying the template instead of the operator — which is the difference
    between an orchestration loop that exists in code and one that exists in prose.

    One process per lens is not incidental. It is stronger isolation than the convention
    it replaces: four lenses read by one session share a context and can echo each other,
    whereas four subprocesses cannot. `stages/audit.py` names that weakness explicitly;
    this closes it. The confinement flags come from `lens_confinement` — see `Confinement`
    for why the deny side is emitted rather than only the allow side.

    `exe` overrides PATH discovery, and exists so every assertion about this argv can run
    on a host with no CLI installed. Before it, the only checks on `--allowedTools`,
    `--add-dir` and `--model` lived inside this module's `__main__` behind
    `if default_cmd():` — skipped on every CI box, and no pytest test executes a
    self-check, so the isolation and diversity guarantees were asserted nowhere at all.

    `settings` is the path to a pinned settings document (`write_pinned_settings`). It is
    supplied by the RUNNER, not manufactured here: a template builder that writes files
    would leak a temp file on every `available()` call, and `available()` is called on
    every phase transition. `--settings` is therefore absent from a template built without
    one, which is honest — and `run_lens` always supplies one, which
    `test_the_lens_that_actually_runs_is_the_confined_one` asserts over the recorded argv.

    Returns '' when no reviewer can be found, so `available()` refuses with a reason and
    the pipeline falls back to the normal manual pause rather than inventing a reviewer.
    """
    # `resolve_reviewer_exe`'s `cfg_exe` step is deliberately skipped here (passed "")
    # because THIS function's own caller (`resolve_cmd`) already supplies `exe=
    # cfg.reviewer_exe` — see `harness/delegation.py`'s own docstring on
    # `resolve_reviewer_exe` for why that is preserved at the call site rather than by
    # weakening the shared function.
    exe = delegation.resolve_reviewer_exe(exe, "")
    if not exe:
        return ""
    # `-p` is one-shot and non-interactive. The prompt is PIPED rather than interpolated
    # into the command line: a lens prompt carries the paper's own text, which is
    # untrusted data that may contain quotes, backticks or anything else, and a shell
    # that expands it is a shell that can be made to run it. `reviewer_cli.cli_pipe_command`
    # picks the platform-correct reader (`type` on Windows, `cat` elsewhere) — `run_lens`
    # uses shell=True, so cmd.exe has no `cat` and does not understand POSIX substitution.
    # Getting this wrong does not fail loudly; it produces an empty output file, which
    # `run_lens` reports as "the command exited without writing", so the pipeline degrades
    # to the manual pause and the cause is invisible.
    #
    # ponytail: no runtime `claude --help` preflight for these flags — all of
    # `--allowedTools`, `--disallowedTools`, `--add-dir`, `--model`, `--restricted`,
    # `--strict-mcp-config`, `--settings`, `--bare` and `--output-format json` were
    # verified present on the installed CLI at design time. If a future CLI drops one,
    # every lens fails identically with "exited without writing" (see `run_lens`), which
    # is loud, not silent — an acceptable ceiling rather than a subprocess call on every
    # single lens invocation to re-verify something that does not change between them.
    extra = lens_confinement(lens, pdf_dir).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, lens: str | None = None, pdf_dir: str = "", *,
                settings: str = "") -> str:
    """The command that would run: the operator's if set, otherwise the built-in one.

    An operator-supplied `SH_AUDIT_CMD` wins unconditionally and is NOT restricted by
    this module — `run_lens` records which happened (`tool_policy_detail.enforced` on the
    `.driver.json` sidecar) rather than silently pretending a guarantee it cannot enforce
    over an arbitrary command line.
    """
    return cfg.audit_cmd.strip() or default_cmd(lens, pdf_dir, exe=cfg.reviewer_exe,
                                                settings=settings)


def confinement_for(cfg: Config, lens: str | None = None, pdf_dir: str = "", *,
                    settings_sha256: str = "") -> Confinement:
    """The confinement record for whichever command `resolve_cmd` would return."""
    if cfg.audit_cmd.strip():
        return operator_confinement(f"lens:{lens}" if lens else "lens:unnamed")
    return lens_confinement(lens, pdf_dir, settings_sha256=settings_sha256)


def available(cfg: Config, lens: str | None = None, pdf_dir: str = "") -> tuple[bool, str]:
    """Whether auto-audit can run, and if not, the reason to show the operator.

    `lens` and `pdf_dir` exist because this used to validate a DIFFERENT command than the
    one that ran: `available(cfg)` resolved the lens-less template while `run_lens`
    executed the per-lens one with `--model` and `--add-dir`. Harmless while the
    placeholders were lens-independent, and exactly the shape of check that stops catching
    things the moment the per-lens branch grows.
    """
    if not cfg.allow_auto_audit:
        # NOT "(or pass --auto-audit)", which is what this message used to say. The
        # controller only reaches this call when the operator ALREADY passed that flag
        # (`controller._phase_audit` returns `waiting` before it otherwise), so the
        # sentence a human read was "pass the flag you just passed". The gate is an
        # environment value; a flag widens it only where an entrypoint calls
        # `Config.open_delegation_gates`, and this string must not promise wiring that
        # lives in another file. `tests/test_delegation_path.py` holds the two in step.
        return False, ("auto-audit gate is closed: SH_ALLOW_AUTO_AUDIT is not set. Set it "
                       "(in the environment or .env.sandbox) to let the controller fill "
                       "lenses by running a reviewer")
    cmd = resolve_cmd(cfg, lens, pdf_dir)
    if not cmd:
        return False, ("no reviewer is available: SH_AUDIT_CMD is empty, SH_REVIEWER_EXE is "
                       "empty and the `claude` CLI is not on PATH, so there is nothing to "
                       "delegate the audit to")
    if "{prompt}" not in cmd or "{out}" not in cmd:
        return False, ("SH_AUDIT_CMD must contain both {prompt} and {out} placeholders; "
                       f"got: {cfg.audit_cmd!r}")
    return True, ""


def parse_lens_json(text: str, lens: str) -> LensReport:
    """A LensReport from the command's output file, or AuditDriverError explaining why not.

    The thin public API; `parse_lens_report` returns the same report plus what the parse
    itself observed. `stages/audit.accept_lens` calls this one, because a hand-accepted
    lens has no CLI result envelope to read.
    """
    return parse_lens_report(text, lens)[0]


def parse_lens_report(text: str, lens: str) -> tuple[LensReport, dict]:
    """(report, meta) — the parsed lens plus what was observed while parsing it.

    `meta` carries the CLI's own result envelope (see `envelope_provenance`) and the two
    key-strip counts. Returned rather than logged, because it belongs on the provenance
    sidecar: "this lens tried to write three harness-owned fields" is a fact about how the
    reading was produced, and a fact nobody records is a fact nobody can act on.

    Tolerant of two things only: a JSON object wrapped in prose or a fenced code block,
    because a chat-shaped reviewer will often produce that; and the CLI's
    `--output-format json` envelope around it. Not tolerant of a missing `findings` key —
    a report with no findings list is not a report that found nothing, it is a report that
    did not happen.
    """
    outer = (text or "").strip()
    if not outer:
        raise AuditDriverError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                  "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        # The CLI reported its OWN failure inside a well-formed envelope, so the output
        # file exists, parses, and contains no report. Classifying the reviewer's words
        # here is what keeps a rate limit or a rejected `--model` out of the generic
        # bucket that consumes a retry attempt (invariant 14).
        raise driver_error(raw[:300] or "the reviewer reported an error and returned no result")
    if not raw:
        raise AuditDriverError("the reviewer returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AuditDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AuditDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AuditDriverError("output JSON is not an object")
    if "findings" not in data:
        raise AuditDriverError("output JSON has no 'findings' key")
    if not isinstance(data["findings"], list):
        raise AuditDriverError("'findings' is not a list")

    # `Finding.evidence_ref` defaults to "", so a finding that arrives without one is
    # accepted by the model and then written back out with an empty string — which reads
    # downstream as "a reference was recorded and it is blank" rather than "the reference
    # is missing". Catching it here keeps this module from laundering an unlocatable
    # finding into a well-formed lens file. Refusing the whole lens rather than dropping
    # the finding is deliberate: a reviewer that omits references is malfunctioning, and
    # silently keeping its other findings would hide that.
    for i, f in enumerate(data["findings"]):
        if not isinstance(f, dict):
            raise AuditDriverError(f"findings[{i}] is not an object")
        if (f.get("evidence_quote") or "").strip() and not (f.get("evidence_ref") or "").strip():
            raise AuditDriverError(
                f"findings[{i}] ({f.get('finding_id') or 'unnamed'}) carries an "
                f"evidence_quote but no evidence_ref; a quote with no location cannot be "
                f"verified and must not be persisted")
        # Same rule for every further side of a multi-location concern. Letting one
        # through would make the second half of a cross-section claim exactly the
        # unlocatable assertion the schema was extended to replace.
        for j, side in enumerate(f.get("additional_evidence") or []):
            if not isinstance(side, dict):
                raise AuditDriverError(f"findings[{i}].additional_evidence[{j}] is not an object")
            if not (side.get("evidence_quote") or "").strip():
                raise AuditDriverError(
                    f"findings[{i}].additional_evidence[{j}] carries no evidence_quote; a "
                    f"further location with nothing quoted from it names nothing")
            if not (side.get("evidence_ref") or "").strip():
                raise AuditDriverError(
                    f"findings[{i}].additional_evidence[{j}] carries an evidence_quote but "
                    f"no evidence_ref; the second half of a cross-section concern has to be "
                    f"as locatable as the first")

    # INVARIANT 2, at the boundary where the artifact is minted rather than only where the
    # verdict is computed. See `HARNESS_OWNED_FINDING_KEYS`.
    data, stripped, unknown = strip_harness_keys(data)
    meta["harness_keys_stripped"] = stripped
    meta["unknown_keys_dropped"] = unknown
    data["lens"] = lens
    try:
        return LensReport(**data), meta
    except Exception as e:                       # pydantic validation, shape errors
        raise AuditDriverError(f"output does not match the lens schema: {e}") from e


def run_lens(cfg: Config, pid: str, lens: str, prompt: Path, out: Path, *,
            pdf_dir: str = "", unit_id: str = "") -> dict:
    """Run the configured reviewer for one lens. Returns a record; raises on failure.

    The reviewer writes to a STAGING path, never to `audit/<lens>.json`, and its output
    is promoted to the lens path only after it validates. Redirecting the command
    straight at the lens path was a real defect with a silent and severe failure: when
    the reviewer emitted something that was not a lens report — a rate-limit notice, an
    error page, a partial response — the shell had already created the file before this
    module could reject it. `run_audit` then saw four files and reported the panel
    complete, `load_reports` could not parse them and returned four EMPTY reports, and a
    paper that had never been audited came out GREEN with "4 lenses run, 0 findings".

    The staged output of a rejected run is kept at `<lens>.rejected.txt` — diagnosable,
    and not a filename anything downstream mistakes for a result.

    Runs with `cwd` an EMPTY scratch directory, deleted after — a lens started in the
    repo root (the old behaviour) can `Read` its three sibling `audit/<lens>.json`
    files and this harness's own source, which contradicts the "sealed session" the
    module docstring in `harness/prompts/audit.py` already claims. The confinement flags
    (see `Confinement`) close the rest: the ambient settings, plugins, hooks, MCP servers
    and the operator's own `CLAUDE.md`, none of which the cwd affects.

    Everything needed to REPLAY this call by hand is on the sidecar: the substituted argv,
    the prompt's sha256 plus a content-addressed copy of the prompt itself, the flags as
    booleans, the CLI's reported model and session id, and the raw response beside the
    validated one. Before that, a delegated lens was re-runnable and not replayable —
    against a prompt that may have changed, on a model nobody recorded.
    """
    ok, why = available(cfg, lens, pdf_dir)
    if not ok:
        raise AuditDriverError(why)
    if not prompt.exists():
        raise AuditDriverError(f"prompt file is missing: {prompt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()                             # never let a stale file look like success
    staged = out.with_suffix(".staged")
    # Named off the OUTPUT rather than off the lens. A paper read in parts writes four
    # readings of one lens into one directory, and a rejection file named for the lens
    # would let part 3's unusable output overwrite part 2's — losing the evidence of the
    # first failure at the moment a second one makes it interesting. Identical for a
    # single-pass lens, whose output stem is the lens name.
    rejected = out.with_name(f"{out.stem}.rejected.txt")
    raw_kept = out.with_name(f"{out.stem}.raw.txt")
    for p_ in (staged, rejected, raw_kept):
        p_.unlink(missing_ok=True)

    prompt_sha = prompt_fingerprint(prompt)
    prompt_copy = keep_prompt_copy(prompt, prompt_sha)

    # The pinned settings live OUTSIDE the reviewer's cwd — see `write_pinned_settings`.
    policy_dir = Path(tempfile.mkdtemp(prefix=f"sh-policy-{lens}-"))
    conf = confinement_for(cfg, lens, pdf_dir)
    settings_path = ""
    try:
        if conf.enforced:
            sp, sha = write_pinned_settings(policy_dir, conf.disallowed_tools)
            settings_path = str(sp)
            conf = dataclasses.replace(conf, settings_sha256=sha)
    except OSError as e:
        # The scratch directory is created before the `try` that owns the teardown, so
        # its cleanup has to be here too. Refusing rather than running unconfined: a
        # reviewer launched without the settings it was supposed to be pinned to is a
        # reading whose recorded policy would not be the policy that applied.
        shutil.rmtree(policy_dir, ignore_errors=True)
        raise AuditDriverError(f"could not write the pinned tool policy: {e}") from e
    cmd = (resolve_cmd(cfg, lens, pdf_dir, settings=settings_path)
           .replace("{prompt}", str(prompt)).replace("{out}", str(staged)))
    started = time.time()
    # Started in its own process group/session so a timeout can kill the WHOLE tree, not
    # just the immediate shell. `shell=True` on either platform launches a shell that is
    # itself the parent of the real work — `cmd.exe` for a pipeline, `sh -c` for one — and
    # `Popen.kill()` alone only terminates that shell. The reviewer it launched keeps
    # running as an orphan: it can still be writing to `staged` after this function has
    # already declared the attempt timed out and unlinked that same path, and it can
    # still be running when a retry starts a second reviewer over the same prompt.
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True})
    sandbox = Path(tempfile.mkdtemp(prefix=f"sh-lens-{lens}-"))
    try:
        try:
            # `stdin=DEVNULL`, not inherited. A delegated reviewer that decides to ask a
            # question would otherwise block on the operator's own terminal until the
            # timeout — a human checkpoint inside an autonomous stage that nobody put
            # there deliberately, and one that looks like a hang rather than a refusal.
            # An EOF on stdin makes the reviewer answer or fail; it can never wait.
            proc = subprocess.Popen(cmd, shell=True, cwd=str(sandbox),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", **group_kwargs)
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise AuditDriverError(f"could not start the command: {e}") from e
        try:
            stdout, stderr = proc.communicate(timeout=cfg.audit_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()                   # reap the process now that it is dead
            staged.unlink(missing_ok=True)
            raise AuditDriverError(f"timed out after {cfg.audit_timeout_s}s") from None
        p = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

        if not staged.exists():
            # Classify the reviewer's OWN words first: a rate limit, a revoked
            # credential and a flag the CLI does not accept all arrive here as the same
            # non-zero exit, and they have three different right responses.
            tail = (p.stderr or p.stdout or "").strip()[-300:]
            err = driver_error(tail or f"the command exited {p.returncode} with no output")
            if err.kind != "unknown":
                raise err
            raise AuditDriverError(
                f"the command exited {p.returncode} without writing {out.name}"
                + (f" — {tail}" if tail else ""))

        raw = staged.read_text(encoding="utf-8")
        try:
            report, meta = parse_lens_report(raw, lens)
        except AuditDriverError:
            classified = driver_error(raw.strip()[:300])
            if classified.retry in ("later", "never"):
                # The reviewer's entire response WAS the refusal notice — `raw` is short
                # plain prose, not a lens report that merely failed to parse. Retrying a
                # parse failure can work; retrying this cannot.
                staged.replace(rejected)
                raise classified from None
            # Move the unusable output somewhere nothing reads as a lens result, and
            # keep it, because "the reviewer said something and it was not a report" is
            # worth seeing.
            staged.replace(rejected)
            raise
        # KEPT ON SUCCESS TOO, which it was not before: only the parsed model was written,
        # so any prose the reviewer wrapped its JSON in — a hedge, a note that it could not
        # open the PDF, a statement that it skimmed rather than read — was discarded, and
        # diagnosing a systematically degraded lens had nothing to work from. The failure
        # path was better instrumented than the success path.
        staged.replace(raw_kept)
        # Written normalised, and only now: downstream reads this file, so what is on
        # disk is exactly what was validated rather than whatever prose the command
        # wrapped it in. `sealing.seal` performs that write.
        #
        # This IS a `CLI_SUBPROCESS` call — the argv above was built and spawned by this
        # module — so `mode="CLI_SUBPROCESS"` is passed explicitly rather than derived
        # from anything, which is also what makes `written_by` come out "audit_driver"
        # (`delegation.WRITTEN_BY["CLI_SUBPROCESS"]`) without a literal override here.
        return sealing.seal(out, report.model_dump(), mode="CLI_SUBPROCESS",
                            tool_policy=conf.summary(), extra={
            "lens": lens, "unit_id": unit_id or lens,
            "paper_id": pid, "command": cmd, "returncode": p.returncode,
            "seconds": round(time.time() - started, 1), "findings": len(report.findings),
            # TWO REPRESENTATIONS OF ONE FACT, on purpose — see `Confinement.summary`.
            "tool_policy_detail": conf.policy(),
            # REPLAY. The prompt is regenerated on every run, so the hash is the only
            # thing that says whether the prompt on disk is the one that produced this.
            "prompt_sha256": prompt_sha,
            "prompt_copy": str(prompt_copy) if prompt_copy else "",
            "raw_sha256": hashlib.sha256(raw_kept.read_bytes()).hexdigest(),
            "raw_response": str(raw_kept),
            # What the CLI itself reported: the model that answered, the session, the cost.
            # `envelope_keys` inside says whether a blank field means the CLI reported
            # nothing or means this module read the wrong key.
            "envelope": meta.get("envelope") or {},
            # INVARIANT 2, counted. Non-zero means a lens wrote into a harness-owned field
            # name and the value was removed before the artifact was sealed.
            "harness_keys_stripped": meta.get("harness_keys_stripped", 0),
            "unknown_keys_dropped": meta.get("unknown_keys_dropped", 0),
        })
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(policy_dir, ignore_errors=True)


def _fill_one(cfg: Config, pid: str, unit_id: str, lens: str, prompt: Path,
              out_path: Path, pdf_dir: str) -> tuple[str, dict | None, "AuditDriverError | None"]:
    try:
        rec = run_lens(cfg, pid, lens, prompt, out_path, pdf_dir=pdf_dir, unit_id=unit_id)
        detail = rec.get("tool_policy_detail") or {}
        state.append_log(cfg, pid, artifact_type="audit_auto", phase="audit",
                         headers={"lens": lens, "unit": unit_id,
                                  "findings": rec["findings"],
                                  "seconds": rec["seconds"],
                                  # WHICH MODEL and WHETHER CONFINED, in the case
                                  # history itself. The history line used to carry
                                  # three numbers and nothing about how the reading
                                  # was produced, so a run that silently collapsed
                                  # onto one model left no trace to notice.
                                  "model_requested": detail.get("model_requested", ""),
                                  "model_reported": (rec.get("envelope") or {}).get(
                                      "model_reported", ""),
                                  "confinement_enforced": bool(detail.get("enforced")),
                                  "harness_keys_stripped": rec.get(
                                      "harness_keys_stripped", 0)},
                         path=str(out_path))
        return unit_id, rec, None
    except AuditDriverError as e:
        return unit_id, None, e


def fill(cfg: Config, pid: str, awaiting: list[str], prompts: dict[str, str],
        pdf_dir: str = "", units: list[dict] | None = None) -> dict:
    """Attempt every pending lens. Partial success is success for the lenses that worked.

    THE BUCKETS ARE THE RETRY POLICIES, not the exception classes. `rate_limited` holds
    every `later` failure (an account limit, a 429, an overloaded upstream), `blocked`
    every `never` one (no CLI installed, unauthenticated, a flag the CLI rejects, an
    unreadable PDF), and `failed` only what a second attempt could plausibly fix. The
    controller spends its bounded retry budget on `failed` alone — see
    `harness/failures.py` for why treating all three alike stranded a paper permanently.

    `kinds` carries the classification per lens so the reason survives into the case
    state and the report, rather than only into a log line.

    Every unit runs as its own subprocess with its own sandboxed cwd, its own pinned
    settings and its own prompt/output files (`run_lens`), so dispatching several at once
    on a bounded thread pool (`cfg.audit_concurrency`) is stronger isolation than one
    operator terminal running them one after another, not weaker — see CLAUDE.md's reading
    design. `ThreadPoolExecutor.map` preserves `awaiting`'s order in the results regardless
    of which subprocess actually finished first, so the buckets below are deterministic.
    """
    filled, failed, rate_limited, blocked, kinds = [], {}, {}, {}, {}
    audit_dir = state.project_dir(cfg, pid) / "audit"
    # `awaiting` holds READING UNIT ids, which are lens names on a paper that fits in one
    # pass and `<lens>/part-02` or `<lens>/synthesis` on one that does not. `units` maps
    # each to the lens it belongs to and the file it writes; without it the unit id IS the
    # lens name and the output is `audit/<lens>.json`, which is the historical behaviour
    # and what every caller that does not read in parts still gets.
    by_unit = {u["unit_id"]: u for u in (units or [])}

    def _dispatch(unit_id: str):
        spec = by_unit.get(unit_id, {})
        lens = spec.get("lens", unit_id)
        out_path = Path(spec["out"]) if spec.get("out") else audit_dir / f"{unit_id}.json"
        return _fill_one(cfg, pid, unit_id, lens, Path(prompts[unit_id]), out_path, pdf_dir)

    workers = max(1, min(len(awaiting), cfg.audit_concurrency))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for unit_id, _rec, err in pool.map(_dispatch, awaiting):
            if err is None:
                filled.append(unit_id)
            else:
                kinds[unit_id] = {"kind": err.kind, "retry": err.retry,
                                  "reset_hint": err.reset_hint}
                {"later": rate_limited, "never": blocked}.get(
                    err.retry, failed)[unit_id] = str(err)
    return {"filled": filled, "failed": failed, "rate_limited": rate_limited,
            "blocked": blocked, "kinds": kinds}


if __name__ == "__main__":       # self-check: python -m harness.audit_driver
    cfg = Config.load()

    # --- gate semantics ---------------------------------------------------------------
    closed = Config(allow_auto_audit=False, audit_cmd="x {prompt} {out}")
    assert available(closed)[0] is False, "the gate must be closed by default"
    open_gate = Config(allow_auto_audit=True, audit_cmd="")
    if default_cmd():
        # A reviewer was discovered on this machine, so an empty SH_AUDIT_CMD is no
        # longer a dead end — the harness supplies the invocation itself. This is what
        # makes the orchestration loop closeable in code.
        assert available(open_gate)[0] is True, available(open_gate)[1]
        assert "{prompt}" in default_cmd() and "{out}" in default_cmd()
        assert "claude" in default_cmd().lower()
        # the declared per-lens model reaches the command line — as of the 2026-09-18
        # closure pass every lens declares `sonnet` (see `CLAUDE.md`), so this checks the
        # declaration is honoured, not that the four lenses disagree
        assert "--model sonnet" in default_cmd("overclaim"), default_cmd("overclaim")
        assert "--model sonnet" in default_cmd("confound"), default_cmd("confound")
        assert "--model" not in default_cmd(), "no lens named, so no model is forced"
    else:
        assert available(open_gate)[0] is False
        assert "not on PATH" in available(open_gate)[1]
    # The operator's command always wins over the built-in one.
    mine = Config(allow_auto_audit=True, audit_cmd="mytool {prompt} {out}")
    assert resolve_cmd(mine) == "mytool {prompt} {out}"
    bad = Config(allow_auto_audit=True, audit_cmd="review --in {prompt}")
    assert available(bad)[0] is False, "a template without {out} must be refused"
    good = Config(allow_auto_audit=True, audit_cmd="cp {prompt} {out}")
    assert available(good) == (True, "")

    # --- per-lens confinement, over EVERY declared lens ------------------------------
    # Driven from an injected exe so this is a specification and not a property of
    # whichever machine happens to run it. The old version of these assertions was
    # guarded by `if default_cmd():` and therefore never ran on a host without the CLI.
    _EXE = "/nonexistent/claude"
    for _lens in P.LENSES:
        _cmd = default_cmd(_lens, pdf_dir="/papers", exe=_EXE, settings="/policy/s.json")
        _c = lens_confinement(_lens, "/papers")
        assert "{prompt}" in _cmd and "{out}" in _cmd, _cmd
        for _flag in ("--restricted", "--strict-mcp-config", "--settings",
                      "--disallowedTools", "--allowedTools", "--add-dir", "--model",
                      "--output-format json"):
            assert _flag in _cmd, (_lens, _flag, _cmd)
        # The deny list is DERIVED from the grant, so the two cannot disagree, and every
        # tool this module can name lands in exactly one of them.
        assert not (set(_c.allowed_tools) & set(_c.disallowed_tools)), _lens
        assert set(_c.allowed_tools) | set(_c.disallowed_tools) == set(KNOWN_TOOLS), _lens
        for _tool in CODE_RUNNING_TOOLS:
            assert _tool in _c.disallowed_tools, (_lens, _tool)
        assert _c.model, f"{_lens} declares no model, so the panel is not diverse"
        assert _c.enforced is True and _c.policy()["enforced"] is True
    # NO LENS MAY REACH THE NETWORK. `overclaim` alone used to hold `WebSearch`, and a
    # reader of untrusted paper text with an outbound-request capability is an
    # exfiltration and injection channel whose only mitigation was an instruction in the
    # same prompt the untrusted text arrives in. Every lens is now `Read`-only over the
    # paper's own directory; see the comment above `prompts.audit.LENSES`.
    for _lens in P.LENSES:
        _grant = lens_confinement(_lens).allowed_tools
        assert set(_grant) == {"Read"}, (_lens, _grant)
        for _net in ("WebSearch", "WebFetch"):
            assert _net in lens_confinement(_lens).disallowed_tools, (_lens, _net)
    # No pdf_dir, no `--add-dir`: a default directory would be a grant nobody asked for.
    assert "--add-dir" not in default_cmd("protocol", exe=_EXE)
    # An operator-supplied command line is recorded as UNENFORCED, and cannot be
    # described as anything else — `enforced` is a property of `template`, not a field.
    _op = operator_confinement("lens:overclaim")
    assert _op.enforced is False and _op.flags("/s.json") == ""
    assert "unrecorded" in _op.summary(), _op.summary()
    assert confinement_for(Config(audit_cmd="x {prompt} {out}"), "overclaim").enforced is False
    assert confinement_for(Config(audit_cmd=""), "overclaim").enforced is True

    # --- the pinned settings document -------------------------------------------------
    _settings = json.loads(pinned_settings_json(denied_tools(("Read",))))
    assert _settings["permissions"]["defaultMode"] == "default", "must fail CLOSED in -p"
    assert _settings["permissions"]["allow"] == []
    assert _settings["permissions"]["additionalDirectories"] == []
    assert _settings["enabledPlugins"] == {} and _settings["hooks"] == {}
    assert _settings["enableAllProjectMcpServers"] is False
    for _tool in CODE_RUNNING_TOOLS:
        assert _tool in _settings["permissions"]["deny"], _tool
    # Same bytes for the same deny list, so the recorded sha256 identifies the document.
    assert pinned_settings_json(("Bash",)) == pinned_settings_json(("Bash",))
    assert pinned_settings_json(("Bash",)) != pinned_settings_json(("Bash", "Write"))

    # --- the CLI's own result envelope ------------------------------------------------
    _inner = '{"lens":"overclaim","findings":[]}'
    _env = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": _inner, "session_id": "s-1", "total_cost_usd": 0.5,
                       "num_turns": 3, "modelUsage": {"claude-opus-4": {"in": 1}}})
    _payload, _envelope = unwrap_envelope(_env)
    assert _payload == _inner and _envelope.get("session_id") == "s-1"
    _prov = envelope_provenance(_envelope)
    assert _prov["model_reported"] == "claude-opus-4" and _prov["session_id"] == "s-1"
    assert _prov["cost_usd"] == 0.5 and _prov["num_turns"] == 3
    # The honesty field: what the envelope ACTUALLY carried, so a blank `model_reported`
    # is distinguishable from a key we guessed wrong.
    assert "session_id" in _prov["envelope_keys"] and "modelUsage" in _prov["envelope_keys"]
    # A lens report is never mistaken for an envelope, however it is shaped.
    assert unwrap_envelope(_inner) == (_inner, {})
    assert unwrap_envelope('{"type":"result","findings":[],"result":"x"}')[1] == {}
    assert unwrap_envelope("plain prose") == ("plain prose", {})
    # A `text`-format response still parses: an operator command may ask for either.
    assert parse_lens_json(_inner, "overclaim").lens == "overclaim"
    assert parse_lens_json(_env, "overclaim").lens == "overclaim"

    # --- invariant 2 at the artifact boundary -----------------------------------------
    _forged = {"lens": "overclaim", "schema_version": 2, "findings": [
        {"finding_id": "o-1", "severity": "MAJOR", "statement": "s",
         "evidence_quote": "q", "evidence_ref": "p1",
         "verified_observation": "THE HARNESS CONFIRMED THIS",
         "evidence_class": "cell_verified", "counted_severity": "FATAL",
         "finding_class": "CONFIRMED_FINDING", "grade": {"verdict": "CONFIRMED"},
         "totally_made_up_key": "yes"}], "notes": "n"}
    _rep, _meta = parse_lens_report(json.dumps(_forged), "overclaim")
    _f = _rep.findings[0]
    assert _f.verified_observation == "", "a lens may not certify its own reasoning"
    assert _f.evidence_class == "unverified" and _f.counted_severity == ""
    assert _f.finding_class == "UNGRADED" and _f.grade is None
    assert _meta["harness_keys_stripped"] == 5, _meta
    assert _meta["unknown_keys_dropped"] == 1, _meta
    # The forged strings are absent from the BYTES that get sealed, not merely from the
    # verdict — that was already safe, and the sealed artifact was not.
    _sealed = json.dumps(_rep.model_dump())
    assert "THE HARNESS CONFIRMED THIS" not in _sealed
    assert "totally_made_up_key" not in _sealed
    # Every harness-owned key is genuinely a field of `Finding` (a typo in that tuple
    # would strip nothing and pass silently), and `evidence_origin` is deliberately not
    # in it — see the comment there.
    for _k in HARNESS_OWNED_FINDING_KEYS:
        assert _k in Finding.model_fields, _k
    assert "evidence_origin" not in HARNESS_OWNED_FINDING_KEYS

    # --- delegation-specific failure shapes, asserted ONE AT A TIME -------------------
    # Invariant 14: an attempt is spent only where spending it could change the answer.
    _k, _r, _ = classify_delegated_failure(
        "Claude requested permissions to use Bash, but you have not granted it")
    assert (_k, _r) == ("bad_invocation", "never"), (_k, _r)
    assert _k != "unauthenticated", "a tool refusal is not a revoked credential"
    _k, _r, _ = classify_delegated_failure("Error: model opus is not available on your plan")
    assert (_k, _r) == ("bad_invocation", "never"), (_k, _r)
    _k, _r, _ = classify_delegated_failure("your account does not have access to model opus")
    assert (_k, _r) == ("bad_invocation", "never"), (_k, _r)
    # The narrowing: "not set" alone is not one of THIS harness's gates.
    assert classify_delegated_failure("the random seed was not set")[:2] == ("unknown", "now")
    assert classify_delegated_failure("auto-audit gate is closed")[1] == "never"
    assert classify_delegated_failure("set SH_ALLOW_AUTO_AUDIT=1")[1] == "never"
    # Everything `harness.failures` already classified still classifies the same way.
    for _text, _want in (("You've hit your session limit · resets 3:20pm", "later"),
                         ("HTTP 429 Too Many Requests", "later"),
                         ("'claude' is not recognized as an internal or external command",
                          "never"),
                         ("the request timed out", "now"),
                         ("some entirely novel catastrophe", "now")):
        assert classify_delegated_failure(_text)[1] == _want, _text
    for _text in ("", "\x00", "x" * 5000, "資源制限"):
        _k, _r, _ = classify_delegated_failure(_text)
        assert _k in failures.FAILURE_KINDS and _r in failures.RETRY_POLICIES, _text

    # --- replay: the prompt that ran is identified -------------------------------------
    with tempfile.TemporaryDirectory() as _td:
        _p = Path(_td) / "overclaim.md"
        _p.write_text("first version", encoding="utf-8")
        _sha = prompt_fingerprint(_p)
        assert len(_sha) == 64
        _copy = keep_prompt_copy(_p, _sha)
        assert _copy is not None and _copy.exists() and _sha[:12] in _copy.name
        _side = Path(_td) / "overclaim.driver.json"
        state.write_json(_side, {"prompt_sha256": _sha})
        assert prompt_is_unchanged(_side, _p) == (True, "")
        _p.write_text("regenerated after an edit to prompts/audit.py", encoding="utf-8")
        _ok, _why = prompt_is_unchanged(_side, _p)
        assert _ok is False and "changed after this output was sealed" in _why, _why
        # A sidecar written before the field existed reports `unpinned`, not a mismatch:
        # calling every pre-existing artifact forged would be a false accusation.
        state.write_json(_side, {"lens": "overclaim"})
        assert prompt_is_unchanged(_side, _p) == (True,
                                                  "unpinned (written before the prompt was "
                                                  "fingerprinted)")

    # --- parsing ----------------------------------------------------------------------
    body = ('Here you go:\n```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
            '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
            '"evidence_ref":"p1"}],"unasked_question":"u","notes":"n"}\n```')
    rep = parse_lens_json(body, "overclaim")
    assert rep.lens == "overclaim" and len(rep.findings) == 1
    assert rep.findings[0].severity == "MAJOR"

    for bad_text, expect in (("", "empty file"), ("no json here", "no JSON object"),
                             ("{nope}", "not valid JSON"), ('{"lens":"x"}', "no 'findings' key"),
                             ('{"findings":{}}', "not a list")):
        try:
            parse_lens_json(bad_text, "overclaim")
            raise AssertionError(f"should have rejected {bad_text!r}")
        except AuditDriverError as e:
            assert expect in str(e), (bad_text, str(e))

    # --- a command that writes nothing must fail, not produce an empty lens ------------
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("prompt", encoding="utf-8")
        silent = Config(projects_dir=Path(td), allow_auto_audit=True,
                        audit_cmd="python -c \"pass\" {prompt} {out}")
        try:
            run_lens(silent, "p", "overclaim", prompt, Path(td) / "overclaim.json")
            raise AssertionError("a command writing no output must raise")
        except AuditDriverError as e:
            assert "without writing" in str(e), str(e)

    # --- an account rate limit is distinguished from an ordinary malformed response ----
    # This is the confirmed `sanchez24a-icml` failure: the reviewer's ENTIRE response is
    # "You've hit your session limit · resets 3:20pm (Asia/Kolkata)", printed to stdout,
    # with no output file written. The old code raised a plain `AuditDriverError`
    # identical in kind to a truncated JSON object, so the controller retried it three
    # times in nine seconds against a limit that cannot possibly clear that fast.
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("prompt", encoding="utf-8")
        limiter = Path(td) / "limiter.py"
        limiter.write_text(
            "import sys\n"
            "sys.stdout.write(\"You've hit your session limit \\u00b7 resets 3:20pm "
            "(Asia/Kolkata)\")\n",
            encoding="utf-8",
        )
        limited = Config(projects_dir=Path(td), allow_auto_audit=True,
                         audit_cmd=f'"{sys.executable}" "{limiter}" {{prompt}} {{out}}')
        try:
            run_lens(limited, "p", "overclaim", prompt, Path(td) / "overclaim.json")
            raise AssertionError("a rate-limit response must raise")
        except RateLimited as e:
            assert "3:20pm" in str(e), str(e)
        except AuditDriverError as e:
            raise AssertionError(f"raised the generic class, not RateLimited: {e}") from e

    # --- what this layer must be INCAPABLE of ------------------------------------------
    # It REPORTS how a reading was produced. It may not decide what the reading is worth.
    # Read off the syntax tree rather than by grepping the source text: a source-text
    # guard over this module's OWN self-check matches the forbidden names in its own
    # assertion list, which is how a drift guard ends up asserting that it exists.
    import ast as _ast
    import inspect as _inspect

    _src = _inspect.getsource(sys.modules[__name__])
    _tree = _ast.parse(_src)
    _imported: set[str] = set()
    _assigned: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.ImportFrom):
            _imported.add(_node.module or "")
            _imported.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
        elif isinstance(_node, (_ast.Assign, _ast.AnnAssign)):
            for _t in (_node.targets if isinstance(_node, _ast.Assign) else [_node.target]):
                if isinstance(_t, _ast.Name):
                    _assigned.add(_t.id)
                elif isinstance(_t, _ast.Attribute):
                    _assigned.add(_t.attr)
    for _forbidden in ("grading", "taxonomy", "stages", "planner", "priority", "report"):
        assert _forbidden not in _imported, (
            f"the delegation layer must not reach into harness.{_forbidden} — it reports "
            f"how a reading was produced and may not decide what it is worth")
    for _decision in ("severity", "counted_severity", "verdict", "triage", "claim_status",
                      "finding_class", "binding_cap"):
        assert _decision not in _assigned, f"this layer assigned {_decision!r}"
    _policy_keys = set(lens_confinement("overclaim", "/papers").policy())
    for _leak in ("severity", "counted_severity", "verdict", "triage", "confidence",
                  "finding_class"):
        assert _leak not in _policy_keys, _leak
    assert not any(f.name in ("severity", "verdict", "triage")
                   for f in dataclasses.fields(Confinement))
    # Every parameter of the command builders is a vocabulary string or a path string —
    # no count, no metric, no paper identity, so a per-paper confinement is inexpressible.
    for _fn in (default_cmd, lens_confinement, resolve_cmd):
        for _name, _param in _inspect.signature(_fn).parameters.items():
            assert _param.annotation in ("str", "str | None", "Config", "tuple[str, ...]",
                                         "Path"), (_fn.__name__, _name, _param.annotation)

    # --- no human checkpoint inside the delegated stage --------------------------------
    # Again from the tree, and again because the list of forbidden names would otherwise
    # match itself. Every callee this module names, by bare name or attribute.
    _called: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.Call):
            _fn_node = _node.func
            if isinstance(_fn_node, _ast.Name):
                _called.add(_fn_node.id)
            elif isinstance(_fn_node, _ast.Attribute):
                _called.add(_fn_node.attr)
    for _affordance in ("input", "getpass", "confirm", "prompt_user", "approve",
                        "getch", "ask"):
        assert _affordance not in _called, (
            f"{_affordance!r} is a human checkpoint inside an autonomous stage; the danger "
            f"is not a bug in such a call, it is having one")
    # And the child cannot reach the operator's terminal even if it tries to ask.
    assert "stdin=subprocess.DEVNULL" in _inspect.getsource(run_lens)

    print(json.dumps({"self_check": "ok", "gate_default": cfg.allow_auto_audit}, indent=2))
