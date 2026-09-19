"""The multi-agent delegation path — what it must be INCAPABLE of, not what it can do.

This file exists because the delegation architecture was described and never exercised.
Measured over the seven-paper corpus before this landed: 28 of 28 lens sidecars say
`written_by: manual_accept`, 28 of 28 record `tool_policy: "unrecorded (general-purpose
subagent, no sandbox restriction, full tool access incl. WebSearch)"`, zero grade
artifacts exist, zero substantive verdicts exist, and zero rejected outputs exist. So the
sandboxed cwd, the tool restriction and the per-lens model selection applied to none of
the readings that produced every published result.

Worse than untested: UNTESTABLE. The only assertions on the isolation flags lived inside
`audit_driver.__main__` behind `if default_cmd():`, no pytest test executes a module
self-check, and `tests/test_audit_driver.py` is two tests about process-tree kill. On any
host without the `claude` CLI — every CI box — the confinement and diversity guarantees
were asserted nowhere at all. Every test below therefore runs from an INJECTED executable
path (`Config.reviewer_exe`, or `exe=` on the command builders), so the argv the harness
builds is checked without a reviewer installed, and the filesystem-isolation tests drive
the real `run_lens` against a scripted reviewer double the way
`tests/test_sandbox_backend.py` drives the cloud provider against a scripted double.

WHAT THIS LAYER MAY NOT DO is the other half of the file. It reports how a reading was
produced. It may not set a severity, write a verdict, raise a triage colour, or reach
`grading.derive` — and the tests that forbid those are signature and import assertions,
so the forbidden thing is inexpressible rather than merely absent.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sys
from pathlib import Path

import pytest

from harness import audit_driver, grade_driver, grading, verdict_driver
from harness.artifacts import Finding, Grade, LensReport, SubstantiveVerdict
from harness.audit_driver import (CODE_RUNNING_TOOLS, HARNESS_OWNED_FINDING_KEYS,
                                  KNOWN_TOOLS, Confinement)
from harness.config import Config
from harness.prompts import audit as AP
from harness.prompts import grade as GP
from harness.prompts import verdict as VP

HARNESS_ROOT = Path(__file__).resolve().parents[1]
FAKE_EXE = "/nonexistent/bin/claude"
LENSES = tuple(AP.LENSES)

# The three delegated roles, each as (name, argv builder). Built from an injected exe so
# every assertion below runs with no CLI on PATH.
ROLES: tuple[tuple[str, object], ...] = (
    ("lens:overclaim",
     lambda: audit_driver.default_cmd("overclaim", pdf_dir="/papers", exe=FAKE_EXE,
                                      settings="/policy/settings.pinned.json")),
    ("grader",
     lambda: grade_driver.default_cmd(Config(), exe=FAKE_EXE,
                                      settings="/policy/settings.pinned.json")),
    ("assessor",
     lambda: verdict_driver.default_cmd(Config(), exe=FAKE_EXE,
                                        settings="/policy/settings.pinned.json")),
)

CONFINEMENTS: tuple[tuple[str, Confinement], ...] = (
    tuple((f"lens:{ln}", audit_driver.lens_confinement(ln, "/papers")) for ln in LENSES)
    + (("grader", grade_driver.grade_confinement(Config())),
       ("assessor", verdict_driver.verdict_confinement(Config()))))

LENS_JSON = json.dumps({"lens": "overclaim", "schema_version": 2, "findings": [],
                        "unasked_question": "", "notes": "read by a double"})


# --------------------------------------------------------------------------- #
# a scripted reviewer double — the technique from tests/test_sandbox_backend.py
# --------------------------------------------------------------------------- #
def _executable(tmp_path: Path, script: Path, name: str) -> Path:
    """Wrap a Python script as something the shell can invoke as `"{exe}" -p ...`.

    A `.py` file cannot stand in for the CLI: `default_cmd` quotes the executable as a
    single token, so a two-word `python script.py` cannot be substituted for it. The
    wrapper is what lets the REAL built-in template — pipe, flags, redirect and all — run
    against a double, instead of the tests only ever exercising an operator-supplied
    `audit_cmd` that bypasses every flag this file is about.
    """
    if sys.platform == "win32":
        exe = tmp_path / f"{name}.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def _reviewer_double(tmp_path: Path, *, stdout_body: str, probe_out: Path,
                     name: str = "double") -> Path:
    """A stand-in reviewer that records what it could see and prints what it was told to.

    It writes its observations to `probe_out` (an absolute path baked into the script, so
    nothing depends on the environment reaching the child) and prints `stdout_body`, which
    the template redirects into the staging file.
    """
    script = tmp_path / f"{name}_script.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "probe = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "         'cwd_entries': sorted(os.listdir('.')),\n"
        "         'prompt_chars': len(prompt), 'read_ok': [], 'read_refused': []}\n"
        "for rel in ('overclaim.json', 'protocol.json', 'confound.json',\n"
        "            'harness/audit_driver.py', '../audit/overclaim.json',\n"
        "            'settings.pinned.json', 'projects'):\n"
        "    try:\n"
        "        pathlib.Path(rel).read_text(encoding='utf-8')\n"
        "        probe['read_ok'].append(rel)\n"
        "    except OSError:\n"
        "        probe['read_refused'].append(rel)\n"
        f"pathlib.Path({str(probe_out)!r}).write_text(json.dumps(probe), encoding='utf-8')\n"
        f"sys.stdout.write({stdout_body!r})\n",
        encoding="utf-8")
    return _executable(tmp_path, script, name)


def _envelope(inner: str, **over) -> str:
    """The CLI's `--output-format json` result wrapper around a reviewer's answer."""
    env = {"type": "result", "subtype": "success", "is_error": False, "result": inner,
           "session_id": "sess-abcdef", "model": "double-model-1", "num_turns": 1,
           "total_cost_usd": 0.0123, "version": "9.9.9",
           "usage": {"input_tokens": 10, "output_tokens": 2}}
    env.update(over)
    return json.dumps(env)


def _run_a_lens(tmp_path: Path, *, stdout_body: str, lens: str = "overclaim"):
    """Drive the REAL built-in path once. Returns (record, sidecar, probe, out)."""
    papers = tmp_path / "papers"
    papers.mkdir(exist_ok=True)
    prompts = tmp_path / "audit" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    prompt = prompts / f"{lens}.md"
    prompt.write_text("audit this paper — em dash — and Ω survive the pipe",
                      encoding="utf-8")
    probe_out = tmp_path / "probe.json"
    exe = _reviewer_double(tmp_path, stdout_body=stdout_body, probe_out=probe_out)
    cfg = Config(projects_dir=tmp_path, allow_auto_audit=True, reviewer_exe=str(exe),
                 audit_timeout_s=60)
    out = tmp_path / "audit" / f"{lens}.json"
    record = audit_driver.run_lens(cfg, "p", lens, prompt, out, pdf_dir=str(papers))
    sidecar = json.loads(out.with_suffix(".driver.json").read_text(encoding="utf-8"))
    probe = json.loads(probe_out.read_text(encoding="utf-8")) if probe_out.exists() else {}
    return record, sidecar, probe, out


# --------------------------------------------------------------------------- #
# 1. flags that DENY, not only flags that allow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role,build", ROLES, ids=[r for r, _ in ROLES])
def test_every_delegated_role_passes_the_flags_that_deny_rather_than_only_those_that_allow(
        role: str, build):
    """`--allowedTools` is an ALLOW list and denies nothing on its own.

    The failure this prevents is a claim, not a crash: three docstrings in this subsystem
    asserted a confinement — "zero tools, no filesystem access at all", "a sealed session
    that sees the paper and nothing else" — that rested entirely on the CLI's permission
    mode, which the harness neither set nor recorded. On a host whose settings file said
    `defaultMode: auto`, a granted `Read` reached the whole disk and the review said it
    had not.
    """
    cmd = build()
    for flag in ("--restricted", "--strict-mcp-config", "--settings", "--disallowedTools",
                 "--allowedTools", "--output-format json"):
        assert flag in cmd, (role, flag, cmd)


def test_the_grader_and_the_assessor_drop_the_operators_own_memory_file_and_a_lens_does_not():
    """A user-level `CLAUDE.md` reaches every session regardless of cwd.

    Prevents a blinding LEAK, not noise. The grader's whole value is that it has not seen
    the first reader's opinion; an operator's own notes about this paper arriving through
    `~/.claude/CLAUDE.md`, plus hooks and plugins, defeat that silently. `--bare` is the
    flag that stops it. A lens deliberately does NOT take it — it legitimately needs
    `Read`, and `--restricted` plus the pinned settings close the same channel for it — so
    this test also forbids "apply --bare everywhere and stop thinking about it".
    """
    assert "--bare" in grade_driver.default_cmd(Config(), exe=FAKE_EXE)
    assert "--bare" in verdict_driver.default_cmd(Config(), exe=FAKE_EXE)
    assert grade_driver.grade_confinement(Config()).bare is True
    assert verdict_driver.verdict_confinement(Config()).bare is True
    for lens in LENSES:
        assert "--bare" not in audit_driver.default_cmd(lens, exe=FAKE_EXE)
        assert audit_driver.lens_confinement(lens).bare is False


@pytest.mark.parametrize("role,conf", CONFINEMENTS, ids=[r for r, _ in CONFINEMENTS])
def test_no_tool_is_both_granted_and_denied_to_the_same_agent(role: str, conf: Confinement):
    """A deny list written down beside the grant drifts the moment the grant changes.

    Prevents the shape where `overclaim` is given `WebSearch` and a hand-maintained deny
    list still names it, so the lens declared in `prompts.audit.LENSES` cannot do the one
    thing that table says it may. The deny list is DERIVED from the grant here; this
    asserts the derivation, over every role.
    """
    assert not (set(conf.allowed_tools) & set(conf.disallowed_tools)), role


@pytest.mark.parametrize("role,conf", CONFINEMENTS, ids=[r for r, _ in CONFINEMENTS])
def test_every_known_tool_is_either_granted_or_explicitly_denied(role: str, conf: Confinement):
    """The whole content of "the allow list denies nothing": a tool in neither list.

    Prevents a tool that is not granted and not named in `--disallowedTools` from being
    available because the ambient permission mode allowed it. Every code-running tool must
    additionally be denied to every role, including the lens that has `Read`.
    """
    assert set(conf.allowed_tools) | set(conf.disallowed_tools) == set(KNOWN_TOOLS), role
    for tool in CODE_RUNNING_TOOLS:
        assert tool in conf.disallowed_tools, (role, tool)


def test_the_declared_model_is_in_the_command_that_actually_runs():
    """`prompts.audit.LENSES` declared a per-lens `model` that nothing read for a long time.

    Prevents a declared model from being decoration: the defect was invisible precisely
    because the key existed while every lens actually ran on the CLI's own default. As of
    the 2026-09-18 closure pass all four lenses declare `sonnet` — the one-vendor-CLI
    "diversity" a single `opus` lens used to buy was a ceiling, not real independence, and
    was retired as not worth its cost at this system's scale (see `CLAUDE.md`) — so this
    now checks that the uniform declaration reaches the command line, not that it varies.
    """
    for lens in LENSES:
        assert "--model sonnet" in audit_driver.default_cmd(lens, exe=FAKE_EXE), lens
        assert audit_driver.lens_confinement(lens).model == "sonnet", lens


@pytest.mark.parametrize("role,build", [(r, b) for r, b in ROLES if r != "lens:overclaim"],
                         ids=["grader", "assessor"])
def test_the_second_reader_does_not_inherit_the_operators_ambient_model(role: str, build):
    """`grade_driver.default_cmd()` took no arguments and emitted no `--model` at all.

    Prevents the "independent second reader" from being whichever model the operator's own
    `~/.claude/settings.json` names — on the development host `opus[1m]`, i.e. potentially
    the same model as the `overclaim` lens it exists to check. An identity that is a
    property of one machine's config is not a recorded fact about the review.
    """
    assert "--model" in build(), role
    assert grade_driver.role_model(Config()) == GP.ROLE_SPEC["model"]
    assert verdict_driver.role_model(Config()) == VP.ROLE_SPEC["model"]
    assert grade_driver.role_model(Config(grade_model="haiku")) == "haiku"
    assert verdict_driver.role_model(Config(verdict_model="haiku")) == "haiku"
    assert "--model haiku" in grade_driver.default_cmd(Config(grade_model="haiku"), exe=FAKE_EXE)
    assert "--model haiku" in verdict_driver.default_cmd(Config(verdict_model="haiku"),
                                                         exe=FAKE_EXE)


def test_a_lens_is_granted_the_paper_directory_and_only_when_there_is_one():
    """`--add-dir` is a grant, and a default grant is a grant nobody asked for.

    Prevents `SOURCE_FIDELITY` (open the PDF to settle an extraction ambiguity) from
    turning into filesystem access when no paper directory was supplied, and prevents the
    grader or assessor — which need no files at all — from receiving one.
    """
    assert "--add-dir" not in audit_driver.default_cmd("protocol", exe=FAKE_EXE)
    granted = audit_driver.default_cmd("protocol", pdf_dir="/papers", exe=FAKE_EXE)
    assert '--add-dir "/papers"' in granted
    assert audit_driver.lens_confinement("protocol", "/papers").add_dir == ("/papers",)
    assert grade_driver.grade_confinement(Config()).add_dir == ()
    assert verdict_driver.verdict_confinement(Config()).add_dir == ()
    assert "--add-dir" not in grade_driver.default_cmd(Config(), exe=FAKE_EXE)
    assert "--add-dir" not in verdict_driver.default_cmd(Config(), exe=FAKE_EXE)


def test_the_availability_check_validates_the_command_that_will_actually_run():
    """`available(cfg)` resolved the LENS-LESS template; `run_lens` executed a per-lens one.

    Harmless while the placeholders happened to be lens-independent, and exactly the shape
    of check that stops catching things the moment the per-lens branch grows — the branch
    that now carries `--model`, `--add-dir` and the pinned settings.
    """
    params = inspect.signature(audit_driver.available).parameters
    assert "lens" in params and "pdf_dir" in params
    src = inspect.getsource(audit_driver.run_lens)
    assert "available(cfg, lens, pdf_dir)" in src, (
        "run_lens must validate the command it is about to run, not a different one")


def test_an_operator_supplied_command_is_recorded_as_unenforced_and_never_described():
    """`tool_policy` was `"restricted"` whenever `SH_AUDIT_CMD` was empty — a label from an `if`.

    Prevents a provenance field that cannot be wrong. The old value was derived from which
    branch of a conditional the code took, not from anything observed, and the word
    "restricted" named a code path while `--restricted` was never passed. An operator
    command wins unconditionally and may be anything; characterising its confinement would
    be describing an argv the harness did not write.
    """
    op = audit_driver.operator_confinement("lens:overclaim")
    assert op.enforced is False
    assert op.flags("/policy/s.json") == "", "no flags are claimed for a foreign argv"
    assert "unrecorded" in op.summary()
    assert op.policy()["enforced"] is False
    # `enforced` is a property of `template`, so it cannot be set to True beside one.
    assert "enforced" not in {f.name for f in dataclasses.fields(Confinement)}
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        op.enforced = True                                          # type: ignore[misc]
    for cfg, expected in ((Config(audit_cmd="x {prompt} {out}"), False),
                          (Config(audit_cmd=""), True)):
        assert audit_driver.confinement_for(cfg, "overclaim").enforced is expected
    assert grade_driver.grade_confinement(Config(grade_cmd="x")).enforced is False
    assert verdict_driver.verdict_confinement(Config(verdict_cmd="x")).enforced is False


def test_the_pinned_settings_replace_the_operators_own_rather_than_merging_with_them():
    """Measured on the development host: a permission mode, a default model, a PreToolUse
    hook, ten plugins and four MCP servers (one a filesystem/symbol server, one a
    vault-write server) were inherited by every delegated call.

    Prevents a review whose reproducibility depends on one operator's machine
    configuration, and — for the grader — on their own notes about the paper.
    """
    doc = json.loads(audit_driver.pinned_settings_json(audit_driver.denied_tools(("Read",))))
    assert doc["enabledPlugins"] == {}
    assert doc["hooks"] == {}
    assert doc["enableAllProjectMcpServers"] is False
    assert doc["permissions"]["allow"] == []
    assert doc["permissions"]["additionalDirectories"] == []
    for tool in CODE_RUNNING_TOOLS:
        assert tool in doc["permissions"]["deny"], tool


def test_the_pinned_settings_fail_closed_in_a_one_shot_session():
    """`defaultMode` decides what happens to a tool nobody named.

    Prevents the mode that looks permissive on a desktop from being permissive here:
    `default` means "ask", and a `-p` one-shot session has nobody to ask, so an unnamed
    tool is refused. `bypassPermissions` would be the same word count and the opposite
    behaviour.
    """
    doc = json.loads(audit_driver.pinned_settings_json(()))
    assert doc["permissions"]["defaultMode"] == "default"
    assert doc["permissions"]["defaultMode"] != "bypassPermissions"


def test_the_settings_document_is_content_addressed_by_what_it_actually_says():
    """A `settings_sha256` that did not change with the deny list would identify nothing.

    Prevents the sidecar from recording a hash that says "some settings were pinned"
    rather than "these settings were pinned".
    """
    a = audit_driver.pinned_settings_json(("Bash",))
    assert a == audit_driver.pinned_settings_json(("Bash",))
    assert a != audit_driver.pinned_settings_json(("Bash", "Write"))


# --------------------------------------------------------------------------- #
# 2. filesystem isolation, driven against the double
# --------------------------------------------------------------------------- #
def test_a_reviewer_that_reaches_for_a_sibling_lens_file_is_refused(tmp_path: Path):
    """Four independent readings are four pieces of evidence; one that can read the other
    three is one reading echoed four times.

    This is the property the module docstrings claimed and no test checked. The double
    tries to open every sibling lens file, this harness's own source, the project tree and
    the pinned policy document, by relative path from where it was started.
    """
    _rec, _side, probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    assert probe, "the double did not run"
    assert probe["read_ok"] == [], probe["read_ok"]
    for target in ("overclaim.json", "protocol.json", "confound.json",
                   "harness/audit_driver.py", "../audit/overclaim.json", "projects"):
        assert target in probe["read_refused"], target


def test_a_reviewer_starts_in_an_empty_directory_that_is_not_the_repository(tmp_path: Path):
    """A lens started in the repo root — the old behaviour — can `Read` anything in it.

    The cwd is the one part of the confinement this harness enforces itself rather than
    asking the CLI for, so it is asserted from the child's own point of view: the directory
    it woke up in, and what was in it.
    """
    _rec, _side, probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    assert probe["cwd_entries"] == [], probe["cwd_entries"]
    cwd = Path(probe["cwd"]).resolve()
    assert cwd != HARNESS_ROOT
    assert HARNESS_ROOT not in cwd.parents
    assert tmp_path.resolve() not in cwd.parents and cwd != tmp_path.resolve()


def test_the_policy_document_is_not_the_first_readable_file_in_the_sandbox(tmp_path: Path):
    """Writing our own settings file into the reviewer's cwd would furnish the one
    directory whose emptiness is the guarantee.

    Small, and it is the whole property that directory has. The pinned settings live in a
    separate scratch directory and are passed by absolute path.
    """
    _rec, side, probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    assert "settings.pinned.json" in probe["read_refused"]
    assert side["tool_policy_detail"]["settings_sha256"], "settings were still pinned"


def test_the_lens_that_actually_runs_is_the_confined_one(tmp_path: Path):
    """A template that carries the flags proves nothing if the runner builds a different one.

    Asserted over the argv the child itself received, and over the argv recorded on the
    sidecar, so the two cannot disagree either.
    """
    _rec, side, probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    argv = " ".join(probe["argv"])
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--settings",
                 "--disallowedTools", "--allowedTools", "--add-dir", "--model",
                 "--output-format"):
        assert flag in argv, (flag, argv)
    for flag in ("--restricted", "--strict-mcp-config", "--settings"):
        assert flag in side["command"], flag
    # And the prompt reached it through the pipe, unmangled by shell quoting.
    assert probe["prompt_chars"] > 20


# --------------------------------------------------------------------------- #
# 3. provenance and exact replay
# --------------------------------------------------------------------------- #
def test_a_delegated_lens_records_the_model_the_cli_said_answered(tmp_path: Path):
    """`--output-format text` discarded the envelope carrying the model and session id.

    Prevents panel diversity, grader independence and every reproducibility claim from
    being unverifiable after the fact. Nothing in this repository records which model
    produced any of its 28 lens readings, and that is why.
    """
    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    env = side["envelope"]
    assert env["model_reported"] == "double-model-1"
    assert env["session_id"] == "sess-abcdef"
    assert env["cli_version"] == "9.9.9"
    assert env["cost_usd"] == 0.0123
    assert env["usage"]["input_tokens"] == 10
    # The honesty field: a blank `model_reported` must be distinguishable from a key we
    # guessed wrong, so what the envelope ACTUALLY carried is recorded verbatim.
    assert "modelUsage" not in env["envelope_keys"]
    assert {"session_id", "version", "usage"} <= set(env["envelope_keys"])


def test_a_reading_produced_without_an_envelope_says_so_rather_than_inventing_a_model(
        tmp_path: Path):
    """An operator command may still ask for `--output-format text`.

    Prevents the opposite failure to the one above: a provenance field filled in with a
    plausible default. No envelope means an empty record and an empty `envelope_keys`, not
    a model name nobody reported.
    """
    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=LENS_JSON)
    assert side["envelope"] == {}


def test_the_tool_policy_records_the_flags_that_were_passed_and_not_which_template_was_chosen(
        tmp_path: Path):
    """The old `tool_policy` was two-valued and unfalsifiable — see
    `test_an_operator_supplied_command_is_recorded_as_unenforced_and_never_described`.

    Here the structured record's own shape is the assertion: `restricted` is a BOOLEAN
    about a flag, not a string about a code path.
    """
    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    detail = side["tool_policy_detail"]
    for key in ("allowed_tools", "disallowed_tools", "add_dir", "restricted",
                "strict_mcp", "bare", "settings_sha256", "model_requested",
                "output_format", "template", "enforced", "role"):
        assert key in detail, key
    assert detail["restricted"] is True and isinstance(detail["restricted"], bool)
    assert detail["strict_mcp"] is True
    assert detail["enforced"] is True and detail["template"] == "built_in"
    assert detail["model_requested"] == "sonnet"
    # Read-only, and the NETWORK tools are on the deny side. No lens may search: see
    # `prompts.audit.LENSES` and `tests/test_isolation_boundary.py` for the same posture
    # applied to execution.
    assert detail["allowed_tools"] == ["Read"], detail["allowed_tools"]
    for denied in ("Bash", "WebSearch", "WebFetch"):
        assert denied in detail["disallowed_tools"], denied
    # The string stays too, under the original key, because `manuscript/check_claims.py`
    # collects these into a SET and a dict is not hashable. Dropping it would make the
    # honesty check that forces the manuscript to admit this path never ran crash.
    assert isinstance(side["tool_policy"], str) and side["tool_policy"]
    assert "unrecorded" not in side["tool_policy"]


def test_the_raw_response_survives_a_successful_reading_not_only_a_failed_one(tmp_path: Path):
    """The failure path was better instrumented than the success path.

    Prevents losing any prose the reviewer wrapped its JSON in — a hedge, a note that it
    could not open the PDF, a statement that it skimmed rather than read. On success only
    the parsed model was written, so a systematically degraded lens left nothing to
    diagnose from.
    """
    body = _envelope(LENS_JSON)
    _rec, side, _probe, out = _run_a_lens(tmp_path, stdout_body=body)
    kept = Path(side["raw_response"])
    assert kept.exists() and kept.read_text(encoding="utf-8") == body
    assert len(side["raw_sha256"]) == 64
    # And the sealed lens file is still the VALIDATED, normalised one — not the raw text.
    assert json.loads(out.read_text(encoding="utf-8"))["lens"] == "overclaim"


def test_a_lens_whose_prompt_changed_after_sealing_is_not_silently_counted(tmp_path: Path):
    """Prompts are regenerated unconditionally on every run and were never hashed.

    So a sealed lens file plus an edited `prompts/audit.py` still passed its own provenance
    check: the seal certified an output against a prompt that no longer existed. The seal
    was over the right bytes and the wrong correspondence.
    """
    _rec, side, _probe, out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    sidecar = out.with_suffix(".driver.json")
    prompt = tmp_path / "audit" / "prompts" / "overclaim.md"
    assert len(side["prompt_sha256"]) == 64
    assert audit_driver.prompt_is_unchanged(sidecar, prompt) == (True, "")

    prompt.write_text("regenerated after an edit to prompts/audit.py", encoding="utf-8")
    ok, why = audit_driver.prompt_is_unchanged(sidecar, prompt)
    assert ok is False and "changed after this output was sealed" in why
    # The content-addressed copy is what makes the reading replayable at all.
    copy = Path(side["prompt_copy"])
    assert copy.exists() and side["prompt_sha256"][:12] in copy.name
    assert "em dash" in copy.read_text(encoding="utf-8")


def test_a_sidecar_written_before_the_prompt_was_fingerprinted_is_not_called_forged():
    """Every artifact in this repository predates `prompt_sha256`.

    Prevents a new provenance field from retroactively accusing 28 existing sidecars. The
    middle state — recorded nothing, so nothing to compare — is reported as `unpinned` and
    is the caller's decision, not this function's.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "overclaim.md"
        prompt.write_text("x", encoding="utf-8")
        sidecar = Path(td) / "overclaim.driver.json"
        sidecar.write_text(json.dumps({"lens": "overclaim", "written_by": "manual_accept"}),
                           encoding="utf-8")
        ok, why = audit_driver.prompt_is_unchanged(sidecar, prompt)
        assert ok is True and "unpinned" in why


@pytest.mark.parametrize("field", [
    "command", "returncode", "seconds", "written_by", "tool_policy",
    "tool_policy_detail", "content_sha256", "prompt_sha256", "prompt_copy",
    "raw_sha256", "raw_response", "envelope", "harness_keys_stripped", "ts",
])
def test_every_field_needed_to_replay_a_delegated_reading_is_on_the_sidecar(
        tmp_path: Path, field: str):
    """"No delegated lens in this repository is replayable" was the recon's conclusion.

    Only re-runnable — against a prompt that may have changed and a model nobody recorded.
    Each field below is one of the things a person redoing this call by hand needs, and
    each was absent.
    """
    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    assert field in side, field


def test_the_pinned_policy_is_regenerable_from_the_record_alone(tmp_path: Path):
    """The settings file is deleted with the run, so the record has to be enough.

    Prevents the confinement from being unverifiable after the fact. The file cannot be
    kept in the reviewer's cwd — that directory's emptiness is the guarantee — and keeping
    a copy elsewhere would need a policy about where. Instead the bytes are a pure
    function of this module plus the recorded deny list, so step 3 of `REPLAY_RECIPE`
    reproduces them and the hash proves it. This is exactly the assertion a checked-in
    config file could not support once `denied_tools` had drifted from it.
    """
    import hashlib

    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    detail = side["tool_policy_detail"]
    regenerated = audit_driver.pinned_settings_json(tuple(detail["disallowed_tools"]))
    assert hashlib.sha256(regenerated.encode("utf-8")).hexdigest() == detail["settings_sha256"]
    # And the deny list on the record is the one the argv actually carried.
    assert ",".join(detail["disallowed_tools"]) in side["command"]


def test_the_documented_replay_procedure_names_only_fields_that_are_actually_recorded(
        tmp_path: Path):
    """"Replayable" is a claim, and a claim without a method is a slogan.

    Prevents the written procedure and the record from drifting apart in either
    direction: every field `REPLAY_RECIPE` tells a reader to use must be on the sidecar,
    and every step it lists must appear in the text a reader is given.
    """
    _rec, side, _probe, _out = _run_a_lens(tmp_path, stdout_body=_envelope(LENS_JSON))
    for field in audit_driver.REPLAY_FIELDS:
        assert field in side, field
        assert field in audit_driver.REPLAY_RECIPE, field
    # And it states what a replay CANNOT recover, rather than implying it recovers the
    # answer: the weights behind a version alias, and sampling temperature.
    assert "cannot" in audit_driver.REPLAY_RECIPE
    assert "re-derives the CALL, not the answer" in audit_driver.REPLAY_RECIPE


# --------------------------------------------------------------------------- #
# 4. invariant 2 at the artifact boundary
# --------------------------------------------------------------------------- #
def test_a_sealed_lens_file_carries_no_field_the_harness_writes():
    """Demonstrated forgery: `verified_observation='THE HARNESS CONFIRMED THIS'`,
    `evidence_class='cell_verified'` and `counted_severity='FATAL'` all round-tripped.

    `artifacts._Base` is `extra="allow"` with every `Finding` field optional, so
    `parse_lens_json` persisted model-supplied values in harness-owned field NAMES into
    `audit/<lens>.json`, which the driver then content-hashed as authentic. The VERDICT was
    never reachable that way — `stages/audit._coerce` rebuilds each finding with explicit
    keyword arguments — but the sealed artifact carried a forged machine attestation, and
    anything that later trusts that file inherits it.
    """
    forged = {"lens": "overclaim", "schema_version": 2, "findings": [{
        "finding_id": "o-1", "severity": "MAJOR", "statement": "s",
        "evidence_quote": "q", "evidence_ref": "p1",
        "verified_observation": "THE HARNESS CONFIRMED THIS",
        "evidence_class": "cell_verified", "counted_severity": "FATAL",
        "finding_class": "CONFIRMED_FINDING", "binding_cap": "grader",
        "derivation": "the harness derived this", "scientific_class": "CONFOUND",
        "grade": {"verdict": "CONFIRMED", "severity": "FATAL"},
    }], "notes": "n"}
    report, meta = audit_driver.parse_lens_report(json.dumps(forged), "overclaim")
    f = report.findings[0]
    assert f.verified_observation == ""
    assert f.evidence_class == "unverified"
    assert f.counted_severity == ""
    assert f.finding_class == "UNGRADED"
    assert f.binding_cap == "" and f.derivation == ""
    assert f.grade is None
    assert meta["harness_keys_stripped"] == 8, meta
    sealed = json.dumps(report.model_dump())
    for forged_text in ("THE HARNESS CONFIRMED THIS", "the harness derived this"):
        assert forged_text not in sealed, forged_text
    # The lens's OWN words are untouched: stripping must not become censoring.
    assert f.severity == "MAJOR" and f.evidence_quote == "q"


def test_the_forgery_count_distinguishes_an_attempted_attestation_from_a_chatty_reviewer():
    """One number would let a forgery hide inside a reviewer's stray key.

    A stripped harness key is an attempted forgery; a dropped unknown key is a reviewer
    volunteering something the schema does not have. Reporting them together would make
    "this lens tried to sign the harness's name three times" unreadable.
    """
    data = {"lens": "overclaim", "findings": [{
        "finding_id": "o-1", "severity": "MINOR", "statement": "s",
        "counted_severity": "FATAL", "helpful_extra": "hi", "another": 1}],
        "notes": "n", "top_level_extra": True}
    _report, meta = audit_driver.parse_lens_report(json.dumps(data), "overclaim")
    assert meta["harness_keys_stripped"] == 1
    assert meta["unknown_keys_dropped"] == 3


def test_no_field_the_finding_model_calls_harness_written_is_missing_from_the_strip_list():
    """A typo in `HARNESS_OWNED_FINDING_KEYS` would strip nothing and pass silently.

    So the list is checked against the model's OWN descriptions: every `Finding` field
    whose description says the harness writes it must be stripped, with exactly one
    documented exception — `evidence_origin`, which `_coerce` reads back as the lens's
    CLAIM in order to produce `origin_consistency == "corrected"`. Stripping that would
    turn off a check that still runs, still passes, and can no longer fail.
    """
    declared = {name for name, f in Finding.model_fields.items()
                if "WRITTEN BY THE HARNESS" in (f.description or "").upper()}
    missing = declared - set(HARNESS_OWNED_FINDING_KEYS) - {"evidence_origin"}
    assert missing == set(), missing
    for key in HARNESS_OWNED_FINDING_KEYS:
        assert key in Finding.model_fields, key
    assert "evidence_origin" not in HARNESS_OWNED_FINDING_KEYS


def test_a_grade_and_a_verdict_are_stripped_of_keys_their_schemas_do_not_have():
    """The same `extra="allow"` hole, on the other two artifacts.

    Prevents a model-supplied number sitting inside a sealed grade or opinion, where it
    reads as measured. The two external rubric designs this was compared against both do
    exactly this, with a model-assigned 0-10 weight; a model that can set severity violates
    invariant 11 outright.
    """
    grade, gmeta = grade_driver.parse_grade_report(json.dumps(
        {"verdict": "CONFIRMED", "weight": 8, "score": 0.72, "notes": "kept"}))
    assert gmeta["unknown_keys_dropped"] == 2
    assert grade.notes == "kept"
    for name in ("weight", "score"):
        assert name not in json.dumps(grade.model_dump()), name
    opinion, vmeta = verdict_driver.parse_verdict_report(json.dumps(
        {"verdict": "STRONG", "reason": "r", "certainty": 0.9}))
    assert vmeta["unknown_keys_dropped"] == 1
    assert "certainty" not in json.dumps(opinion.model_dump())


# --------------------------------------------------------------------------- #
# 5. the grader's desiderata — invariant 11
# --------------------------------------------------------------------------- #
def test_a_grader_desideratum_cannot_reach_the_function_that_decides_what_a_finding_counts_as():
    """The one idea imported from an external rubric design, imported WITHOUT its scoring.

    Prevents "the grader scored this 8 of 10, therefore MAJOR". `grading.derive` is the
    only function that decides what a finding counts as, and its signature admits
    vocabulary strings and booleans only — so a violation SET, a count of violations, a
    weight or a total is inexpressible there, not merely absent. The desiderata are
    recorded on the sidecar and consumed by nothing.
    """
    params = inspect.signature(grading.derive).parameters
    for forbidden in ("desiderata", "desiderata_violations", "violations", "score",
                      "weight", "rubric", "n_violations", "total", "points"):
        assert forbidden not in params, forbidden
    for name, param in params.items():
        assert param.annotation in ("str", "bool"), (name, param.annotation)
    # And no module on the delegation path calls it.
    for mod in (audit_driver, grade_driver, verdict_driver):
        tree = ast.parse(inspect.getsource(mod))
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                     for a in n.names}
        assert "grading" not in imported, mod.__name__


def test_grading_can_still_only_demote_over_the_whole_reachable_input_space():
    """Invariant 11, restated at this layer after the desiderata were added.

    Prevents the addition from having created a promotion path by any route. Swept over
    every lens severity crossed with every grader verdict and severity: the counted
    severity is never above the lens's own.
    """
    severities = ("FATAL", "MAJOR", "MINOR", "NOTE")
    for lens_sev in severities:
        for verdict in ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT"):
            for grade_sev in severities + ("NONE",):
                for graded in (True, False):
                    _cls, counted, _cap, _why = grading.derive(
                        lens_severity=lens_sev, verification_state="complete",
                        calc_class="", graded=graded, grade_verdict=verdict,
                        grade_severity=grade_sev, confidence="HIGH")
                    assert grading.RANK[counted] <= grading.RANK[lens_sev], (
                        lens_sev, verdict, grade_sev, graded, counted)


def test_a_grader_parse_failure_produces_no_grade_rather_than_a_neutral_one():
    """The anti-pattern, named: `return 0.5` on a parse failure.

    Prevents a fabricated measurement in exactly the shape invariants 6, 7 and 21 forbid.
    A grader that could not be parsed produces no grade at all; a grader that answered but
    said nothing about the desiderata has `None`, which is not a pass.
    """
    for bad in ("", "sorry, I cannot help with that", '{"nope": 1}', "{broken"):
        with pytest.raises(grade_driver.GradeDriverError):
            grade_driver.parse_grade_json(bad)
    silent = grade_driver.parse_grade_report(json.dumps({"verdict": "CONFIRMED"}))[1]
    assert silent["desiderata_passed"] is None
    assert silent["desiderata_passed"] is not True
    assert silent["desiderata_violations"] == []
    # And no neutral constant exists to fall back to. Read off the syntax tree, over
    # literals, rather than by grepping the text — the module's own docstring names `0.5`
    # as the anti-pattern it refuses, so a text search matches the explanation.
    for mod in (grade_driver, verdict_driver):
        tree = ast.parse(inspect.getsource(mod))
        # The module's RUNNING code only. The `if __name__ == "__main__"` self-check is
        # skipped because its whole job is to feed the parser numbers and prove they are
        # dropped, so the fixtures it needs would fail the rule they are asserting.
        floats = {n.value
                  for top in tree.body if not isinstance(top, ast.If)
                  for n in ast.walk(top)
                  if isinstance(n, ast.Constant) and isinstance(n.value, float)}
        assert floats == set(), (mod.__name__, floats)


def test_a_grader_that_invents_a_desideratum_has_not_passed_the_candidate():
    """A closed vocabulary that accepts anything is not closed.

    Prevents a grader writing its own rubric and then satisfying the pass rule by naming
    zero of the real requirements while naming several that do not exist. The invented
    names are recorded, excluded from the violation set, and force the pass to False.
    """
    meta = grade_driver.parse_grade_report(json.dumps(
        {"verdict": "CONFIRMED", "desiderata_violations": ["ALL_GOOD", "looks fine"]}))[1]
    assert meta["desiderata_violations"] == []
    assert sorted(meta["unknown_desiderata"]) == ["ALL_GOOD", "LOOKS FINE"]
    assert meta["desiderata_passed"] is False


@pytest.mark.parametrize("token", GP.DESIDERATA)
def test_no_desideratum_is_a_severity_floor_wearing_a_rubric_as_a_hat(token: str):
    """"no variance reported = MAJOR" was removed from the audit prompt for this reason.

    Prevents it coming back through the grader's own channel. Each desideratum asks whether
    the candidate's ARGUMENT holds; none of them may name an impact grade, because impact
    is a separate axis and deciding it from something that is not impact is the collapse
    invariants 11, 12 and 20 all forbid.
    """
    for banned in ("FATAL", "MAJOR", "MINOR", "NOTE", "SEVERITY", "SEVERE"):
        assert banned not in token, (token, banned)
    assert token in GP.DESIDERATA_GLOSS
    assert token in GP.DESIDERATA_BLOCK


def test_the_desiderata_vocabulary_is_closed_and_totally_parsed():
    """A parser that raises on a malformed violation list would let a grader's typo
    discard the whole grade.

    Prevents both directions: an unparseable value yields two empty tuples rather than an
    exception, and no value outside the vocabulary ever reaches the recognised set.
    """
    for raw in (None, "", [], 0, 3.4, {}, ["  "], [None], "nonsense",
                list(GP.DESIDERATA), ["x", GP.DESIDERATA[0]]):
        rec, inv = GP.parse_violations(raw)
        assert isinstance(rec, tuple) and isinstance(inv, tuple)
        assert set(rec) <= set(GP.DESIDERATA)
        assert not (set(rec) & set(inv))
    # Order is the vocabulary's, so two graders naming one set produce one record.
    assert (GP.parse_violations([GP.DESIDERATA[2], GP.DESIDERATA[0]])[0]
            == GP.parse_violations([GP.DESIDERATA[0], GP.DESIDERATA[2]])[0])


def test_the_grader_still_sees_none_of_what_it_is_blinded_to():
    """Adding a whole block to the grade prompt is a chance to leak into it.

    Prevents the desiderata block from carrying the lens's severity, the thresholds, or
    any other candidate — the three things the withheld list promises are absent.
    """
    built = GP.build("claim", "statement", "target", "reasoning", "conclusion",
                     ["counter"], "quote", "T1:r0:c0", "cell_verified", "obs",
                     "sections", "tables", "sections truncated")
    for leak in ("counted_severity", "RED_FATAL", "MATERIAL_SEVERITY",
                 "severity_rationale", "prior_grades", "other_findings"):
        assert leak not in built, leak
    withheld = inspect.getsource(grade_driver.fill)
    for name in ("severity", "lens", "other_findings", "prior_grades",
                 "verdict_thresholds", "derivation_table"):
        assert name in withheld, name


def test_the_grader_path_runs_end_to_end_and_records_what_produced_the_grade(tmp_path: Path):
    """Zero grade artifacts exist in this repository. The path had never run.

    So the "independent second reader" was a capability nobody had exercised, and its
    sidecar — the only place its model, its confinement and its prompt could be
    recorded — recorded no `tool_policy` at all. This drives the real `run_candidate`
    against a double, so the grader path is exercisable without a CLI and its record is
    checked rather than described.
    """
    inner = json.dumps({"verdict": "PLAUSIBLE", "severity": "MINOR", "confidence": "MEDIUM",
                        "desiderata_violations": [GP.DESIDERATA[0]],
                        "independent_evidence_ref": "T2:r1:c3", "notes": "checked the cell"})
    probe_out = tmp_path / "gprobe.json"
    exe = _reviewer_double(tmp_path, stdout_body=_envelope(inner), probe_out=probe_out,
                           name="gdouble")
    prompts = tmp_path / "audit" / "grade" / "prompts"
    prompts.mkdir(parents=True)
    prompt = prompts / "c-01.md"
    prompt.write_text("grade this one candidate", encoding="utf-8")
    cfg = Config(projects_dir=tmp_path, allow_grading=True, reviewer_exe=str(exe),
                 grade_timeout_s=60)
    out = tmp_path / "audit" / "grade" / "c-01.json"
    rec = grade_driver.run_candidate(cfg, "p", "c-01", prompt, out,
                                     withheld=["severity", "lens"])
    side = json.loads(out.with_suffix(".driver.json").read_text(encoding="utf-8"))
    assert rec["verdict"] == "PLAUSIBLE"
    assert side["written_by"] == "grade_driver"
    assert side["tool_policy_detail"]["allowed_tools"] == []
    assert side["tool_policy_detail"]["bare"] is True
    assert side["tool_policy_detail"]["model_requested"] == GP.ROLE_SPEC["model"]
    assert side["envelope"]["model_reported"] == "double-model-1"
    assert side["prompt_sha256"] and side["raw_sha256"]
    assert side["desiderata_violations"] == [GP.DESIDERATA[0]]
    assert side["desiderata_passed"] is False
    # The grader saw an empty directory and could reach none of the lens files it is
    # blinded to — the whole content of "blinded", asserted from the child's own view.
    probe = json.loads(probe_out.read_text(encoding="utf-8"))
    assert probe["cwd_entries"] == [] and probe["read_ok"] == []
    # The grade file itself is the validated model, and the raw answer is kept beside it.
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "PLAUSIBLE"
    assert Path(side["raw_response"]).exists()


# --------------------------------------------------------------------------- #
# 6. failure classification — invariant 14
# --------------------------------------------------------------------------- #
def test_a_tool_permission_refusal_does_not_read_as_a_revoked_credential():
    """"permission denied" matched `unauthenticated`, so the recorded operator action was
    to re-authenticate a credential that works.

    A tool-permission refusal is a fact about the argv this harness built, and the fix is
    the tool policy. Naming it `unauthenticated` sends a human to the wrong place with
    the right urgency.
    """
    for text in ("Claude requested permissions to use Bash, but you have not granted it",
                 "tool use was denied by the current permission mode",
                 "Bash is not in the allowed tools for this session",
                 "you are not allowed to use WebFetch here"):
        kind, retry, _hint = audit_driver.classify_delegated_failure(text)
        assert kind == "bad_invocation", (text, kind)
        assert kind != "unauthenticated", text
        assert retry == "never", text


def test_a_model_entitlement_error_does_not_burn_the_retry_budget():
    """`--model opus` on an account without opus matched nothing, so it fell to
    `unknown`/`now` and spent every attempt against a flag that will never be accepted.

    Invariant 14: a retry budget is spent only where spending it could change the answer.
    """
    from harness import failures

    for text in ("Error: model opus is not available on your plan",
                 "your account does not have access to model opus",
                 "invalid model: opus-9",
                 "model_not_found: claude-nonexistent"):
        kind, retry, _hint = audit_driver.classify_delegated_failure(text)
        assert retry == "never", (text, kind, retry)
        assert not failures.consumes_attempt(retry), text


def test_a_reviewer_message_that_merely_contains_not_set_is_not_a_permanently_closed_gate():
    """`failures._RULES` maps a bare "not set" to `gate_closed`/never.

    Right for this harness's own refusal string, wrong for a reviewer's: it abandons a
    paper permanently on a phrase that can appear in any diagnostic. The drivers raise
    their own gate refusals directly, never through this classifier, so anything reaching
    it came from the reviewer's mouth. The trade is one wasted attempt on a genuinely
    permanent message, which is the direction `harness/failures.py` argues for itself.
    """
    kind, retry, _ = audit_driver.classify_delegated_failure("the random seed was not set")
    assert (kind, retry) == ("unknown", "now")


def test_the_harnesss_own_gate_refusal_is_still_permanently_closed():
    """The mirror of the test above: narrowing a rule must not delete it.

    A message that actually names a gate — the words, or an `SH_` variable — stays
    `gate_closed`/never, so the controller does not retry against an operator decision.
    """
    for text in ("auto-audit gate is closed", "the grading gate is shut",
                 "set SH_ALLOW_AUTO_AUDIT=1 to let the controller fill lenses"):
        kind, retry, _ = audit_driver.classify_delegated_failure(text)
        assert (kind, retry) == ("gate_closed", "never"), (text, kind, retry)


def test_delegated_classification_stays_inside_the_published_vocabularies():
    """A new rule that invents a kind would persist a token no artifact schema documents.

    `Case.failure_kind` enumerates `failures.FAILURE_KINDS` in its own description, and a
    value outside it is a field a reader cannot interpret.
    """
    from harness import failures

    for text in ("", None, "\x00\x01", "x" * 10_000, "429", "資源制限",
                 "requested permissions to use Bash", "model opus is not available"):
        kind, retry, hint = audit_driver.classify_delegated_failure(text)
        assert kind in failures.FAILURE_KINDS, (text, kind)
        assert retry in failures.RETRY_POLICIES, (text, retry)
        assert isinstance(hint, str)


def test_everything_the_shared_taxonomy_already_classified_classifies_the_same_way():
    """A pre-rule table inserted in front of an existing classifier can shadow it.

    Prevents the two delegation-specific rules from capturing the account rate limit that
    `harness/failures.py` was written for — the confirmed `sanchez24a-icml` failure, whose
    misclassification burned three retries in nine seconds.
    """
    for text, want in (("You've hit your session limit · resets 3:20pm", "later"),
                       ("HTTP 429 Too Many Requests", "later"),
                       ("Error: 503 Service Unavailable", "later"),
                       ("'claude' is not recognized as an internal or external command",
                        "never"),
                       ("Error: Unauthorized. Please run `claude login`.", "never"),
                       ("the request timed out", "now"),
                       ("the reviewer printed no JSON at all", "now"),
                       ("some entirely novel catastrophe", "now")):
        assert audit_driver.classify_delegated_failure(text)[1] == want, text


def test_a_cli_that_reports_its_own_failure_inside_a_well_formed_envelope_is_classified():
    """`--output-format json` makes a failure look like a success to the file check.

    The output file exists, parses, and contains no report — so without reading `is_error`
    the driver would call it a malformed response and retry a rate limit. The envelope's
    own error text is classified instead.
    """
    body = _envelope("You've hit your session limit · resets 3:20pm", is_error=True)
    with pytest.raises(audit_driver.RateLimited) as e:
        audit_driver.parse_lens_report(body, "overclaim")
    assert "3:20pm" in str(e.value)


# --------------------------------------------------------------------------- #
# 7. the whole-paper opinion
# --------------------------------------------------------------------------- #
def test_a_whole_paper_verdict_is_never_unattributed(tmp_path: Path):
    """`verdict_driver.run()` persisted NOTHING, for the only model output in this system
    that can flip `run.py` to exit 3.

    So a subprocess whole-paper opinion had no record of the command, the model, the prompt
    or the time, and fed `verdict_agreement`, the CONTESTED flag and the exit code anyway.
    """
    body = _envelope(json.dumps({"verdict": "SOUND_WITH_MINOR_CONCERNS", "reason": "r",
                                 "weaknesses_are": "LOCAL"}))
    probe_out = tmp_path / "vprobe.json"
    exe = _reviewer_double(tmp_path, stdout_body=body, probe_out=probe_out, name="vdouble")
    cfg = Config(projects_dir=tmp_path, allow_substantive_verdict=True,
                 reviewer_exe=str(exe))
    got = verdict_driver.run(cfg, "the verdict prompt", timeout_s=60, pid="p")
    assert got is not None and got.verdict == "SOUND_WITH_MINOR_CONCERNS"
    sidecar = json.loads(
        (tmp_path / "p" / "reports" / "p.substantive.driver.json").read_text(encoding="utf-8"))
    assert sidecar["written_by"] == "verdict_driver"
    for field in ("command", "returncode", "seconds", "tool_policy",
                  "tool_policy_detail", "prompt_sha256", "raw_sha256", "envelope",
                  "content_sha256", "ts"):
        assert field in sidecar, field
    assert sidecar["envelope"]["model_reported"] == "double-model-1"
    assert sidecar["tool_policy_detail"]["bare"] is True


def test_a_second_run_reuses_the_sealed_verdict_instead_of_paying_for_it_again(tmp_path: Path):
    """`load_accepted` refused anything but `manual_accept`, so a subprocess opinion could
    be produced and never loaded back.

    Every `run.py review` on a paper with the gate open therefore paid for a fresh
    whole-paper opinion, kept no record of it, and used it to decide an exit code.
    """
    body = _envelope(json.dumps({"verdict": "STRONG", "reason": "r",
                                 "weaknesses_are": "LOCAL"}))
    probe_out = tmp_path / "vprobe.json"
    exe = _reviewer_double(tmp_path, stdout_body=body, probe_out=probe_out, name="vdouble")
    cfg = Config(projects_dir=tmp_path, allow_substantive_verdict=True,
                 reviewer_exe=str(exe))
    assert verdict_driver.run(cfg, "prompt text", timeout_s=60, pid="p") is not None
    reloaded = verdict_driver.load_accepted(cfg, "p")
    assert reloaded is not None and reloaded.verdict == "STRONG"
    assert "verdict_driver" in verdict_driver.WRITERS
    assert "manual_accept" in verdict_driver.WRITERS


def test_a_sealed_verdict_about_a_different_prompt_is_not_reused(tmp_path: Path):
    """Reuse without a prompt check is a cached judgement of a different set of findings.

    Prevents the cheaper answer from being the wrong one: this artifact's only
    consequence is a CONTESTED flag and an exit code, so an opinion about last run's
    findings decides this run's exit status.
    """
    body = _envelope(json.dumps({"verdict": "STRONG", "reason": "r",
                                 "weaknesses_are": "LOCAL"}))
    probe_out = tmp_path / "vprobe.json"
    exe = _reviewer_double(tmp_path, stdout_body=body, probe_out=probe_out, name="vdouble")
    cfg = Config(projects_dir=tmp_path, allow_substantive_verdict=True,
                 reviewer_exe=str(exe))
    prompt_text = "the findings as of this run"
    assert verdict_driver.run(cfg, prompt_text, timeout_s=60, pid="p") is not None
    import hashlib

    same = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    other = hashlib.sha256(b"a different set of findings").hexdigest()
    assert verdict_driver.load_accepted(cfg, "p", prompt_sha256=same) is not None
    assert verdict_driver.load_accepted(cfg, "p", prompt_sha256=other) is None


def test_a_whole_paper_opinion_still_degrades_to_nothing_rather_than_to_a_placeholder(
        tmp_path: Path):
    """This driver may never block a report, and never invent one.

    Prevents the persistence added above from turning a failure into a stored default. A
    closed gate and an unparseable answer both yield None, and — the part the persistence
    could have broken — nothing is sealed for a reading that did not produce an opinion,
    so the next run does not load a placeholder back as a real judgement.
    """
    assert verdict_driver.run(Config(allow_substantive_verdict=False), "p") is None

    probe_out = tmp_path / "vprobe.json"
    exe = _reviewer_double(tmp_path, stdout_body="I would rather not say.",
                           probe_out=probe_out, name="vdouble")
    cfg = Config(projects_dir=tmp_path, allow_substantive_verdict=True,
                 reviewer_exe=str(exe))
    assert verdict_driver.run(cfg, "prompt", timeout_s=60, pid="p") is None
    assert not (tmp_path / "p" / "reports" / "p.substantive.json").exists()
    assert verdict_driver.load_accepted(cfg, "p") is None


# --------------------------------------------------------------------------- #
# 8. the autonomy guarantee (R7) over the delegation surface
# --------------------------------------------------------------------------- #
DELEGATION_MODULES = ("harness/audit_driver.py", "harness/grade_driver.py",
                      "harness/verdict_driver.py", "harness/config.py",
                      "harness/prompts/audit.py", "harness/prompts/grade.py",
                      "harness/prompts/verdict.py")


@pytest.mark.parametrize("rel", DELEGATION_MODULES)
def test_no_module_on_the_delegation_path_can_ask_a_human_anything(rel: str):
    """The danger is not a bug in such a function — it is having one.

    "No human checkpoint in execution or decision-making" was a property nothing tested.
    Read off the syntax tree rather than by grepping the text, because a source-text sweep
    over a file that contains this list matches itself.
    """
    tree = ast.parse((HARNESS_ROOT / rel).read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    for affordance in ("input", "getpass", "confirm", "approve", "prompt_user", "getch"):
        assert affordance not in called, (rel, affordance)


@pytest.mark.parametrize("fn", [audit_driver.run_lens, grade_driver.run_candidate,
                                verdict_driver.run],
                         ids=["lens", "grader", "assessor"])
def test_no_delegated_child_inherits_the_operators_terminal(fn):
    """`stdin` was inherited by all three launchers.

    A delegated reviewer that decided to ask a question would have blocked on the
    operator's own terminal until the timeout — a human checkpoint inside an autonomous
    stage that nobody put there deliberately, and one that looks like a hang rather than a
    refusal.
    """
    assert "stdin=subprocess.DEVNULL" in inspect.getsource(fn)


def test_a_gate_flag_widens_and_never_narrows():
    """An operator who exported `SH_ALLOW_GRADING=1` and then ran without `--auto-grade`
    did not ask for grading to be turned off.

    Prevents a flag default silently closing a gate the environment opened, which is the
    same class of surprise as the one this method exists to fix, in the other direction.
    """
    cfg = Config(allow_auto_audit=False, allow_grading=True,
                 allow_substantive_verdict=False)
    opened = cfg.open_delegation_gates(auto_audit=True, auto_grade=False)
    assert opened == ["allow_auto_audit"]
    assert cfg.allow_auto_audit is True
    assert cfg.allow_grading is True, "a False flag must not close what the env opened"
    assert cfg.allow_substantive_verdict is False
    # Idempotent, and reports only what it actually changed.
    assert cfg.open_delegation_gates(auto_audit=True) == []


def test_a_gate_is_never_widened_by_a_library_reading_the_process_command_line():
    """The rejected alternative: read `sys.argv` in `Config`, so the flags "just work".

    One line, and wrong twice — it opens a gate in any process whose argv happens to
    contain the string (a test runner, a subagent, an unrelated tool), and it makes the
    widening invisible at the call site, which is the one place a security-relevant
    decision has to be readable.
    """
    tree = ast.parse((HARNESS_ROOT / "harness" / "config.py").read_text(encoding="utf-8"))
    reads_argv = any(isinstance(n, ast.Attribute) and n.attr == "argv"
                     for n in ast.walk(tree))
    assert not reads_argv, "config.py must not inspect the process command line"


def test_the_flag_the_entrypoint_documents_is_either_wired_or_not_promised():
    """The shipped strings contradicted the shipped code.

    `run.py review --paper x.pdf --auto-audit --auto-grade` — the command the project's
    own documentation calls "the entrypoint" — set neither gate, and the operator got back
    "auto-audit gate is closed; set SH_ALLOW_AUTO_AUDIT=1 (or pass --auto-audit)" having
    just passed `--auto-audit`. This test forbids the contradiction in either direction:
    either the entrypoint widens the gates, or the drivers stop promising that it does.
    """
    run_src = (HARNESS_ROOT / "run.py").read_text(encoding="utf-8")
    wired = ("open_delegation_gates" in run_src
             or "cfg.allow_auto_audit = True" in run_src)
    promised = "--auto-audit" in audit_driver.available(Config())[1]
    assert not promised or wired, (
        "audit_driver.available promises that --auto-audit opens the gate; run.py must "
        "call Config.open_delegation_gates (or set the gate) for that to be true")
    promised_grade = "--auto-grade" in grade_driver.available(Config())[1]
    wired_grade = ("open_delegation_gates" in run_src
                   or "cfg.allow_grading = True" in run_src)
    assert not promised_grade or wired_grade, (
        "grade_driver.available promises that --auto-grade opens the gate; run.py must "
        "call Config.open_delegation_gates (or set the gate) for that to be true")


def test_every_delegation_gate_is_shut_by_default():
    """Widening is per-invocation, and the default is the safe one.

    Prevents the mechanism added for the flags from becoming a default-on path: a fresh
    `Config` with no environment must refuse all three roles.
    """
    fresh = Config(allow_auto_audit=False, allow_grading=False,
                   allow_substantive_verdict=False)
    assert audit_driver.available(fresh)[0] is False
    assert grade_driver.available(fresh)[0] is False
    assert verdict_driver.available(fresh)[0] is False
    assert "SH_ALLOW_AUTO_AUDIT" in audit_driver.available(fresh)[1]
    assert "SH_ALLOW_GRADING" in grade_driver.available(fresh)[1]
    assert "SH_ALLOW_SUBSTANTIVE_VERDICT" in verdict_driver.available(fresh)[1]


# --------------------------------------------------------------------------- #
# 9. this layer reports; it does not decide
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", DELEGATION_MODULES)
def test_nothing_on_the_delegation_path_writes_a_severity_a_verdict_or_a_colour(rel: str):
    """The governing rule: a new mechanism may only cap, name, or report.

    Prevents the provenance layer from acquiring an opinion. Assignment targets are read
    off the syntax tree, so `counted_severity = ...` anywhere in these modules fails
    regardless of how it is spelled or nested.
    """
    tree = ast.parse((HARNESS_ROOT / rel).read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for node in ast.walk(tree):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name):
                assigned.add(t.id)
            elif isinstance(t, ast.Attribute):
                assigned.add(t.attr)
    for decision in ("counted_severity", "finding_class", "binding_cap", "triage",
                     "claim_status", "overall_verdict", "verified_observation",
                     "evidence_class"):
        assert decision not in assigned, (rel, decision)


@pytest.mark.parametrize("rel", DELEGATION_MODULES)
def test_no_module_on_the_delegation_path_imports_a_module_that_decides(rel: str):
    """A report-only layer that can reach the deciding layer is one edit from deciding.

    Import-absence rather than call-absence: the point is that the decision modules are
    not in scope at all, so no future edit can quietly consult one.
    """
    tree = ast.parse((HARNESS_ROOT / rel).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
    for decider in ("grading", "taxonomy", "planner", "priority", "discovery",
                    "questions", "ledger", "stages"):
        assert decider not in imported, (rel, decider)


def test_the_confinement_record_carries_no_field_that_could_be_read_as_a_grade():
    """A provenance record is not a place to put an opinion.

    Prevents a future consumer from finding a severity-shaped key on the sidecar and
    treating it as one. Swept over the dataclass's fields and over the serialised policy.
    """
    names = {f.name for f in dataclasses.fields(Confinement)}
    keys = set(audit_driver.lens_confinement("overclaim", "/papers").policy())
    for leak in ("severity", "counted_severity", "verdict", "triage", "confidence",
                 "finding_class", "score", "weight"):
        assert leak not in names, leak
        assert leak not in keys, leak


def test_the_command_builders_admit_only_vocabulary_and_path_strings():
    """No paper-specific confinement, made inexpressible rather than merely absent.

    Prevents "if this paper, grant one more tool" and "if this metric, use the other
    model": a builder whose parameters are a lens name, a directory and a settings path
    cannot express either. Invariant 10, at this layer.
    """
    for fn in (audit_driver.default_cmd, audit_driver.lens_confinement,
               audit_driver.resolve_cmd, grade_driver.grade_confinement,
               verdict_driver.verdict_confinement):
        for name, param in inspect.signature(fn).parameters.items():
            assert param.annotation in ("str", "str | None", "Config", "Config | None",
                                        "tuple[str, ...]", "Path"), (
                fn.__name__, name, param.annotation)


def test_no_prompt_on_the_delegation_path_names_a_pilot_paper_or_a_role_specific_paper():
    """Invariant 10, over the text that reaches a model.

    Prevents a prompt or a role table from acquiring a paper. The four lens prompts, the
    grade prompt and the verdict prompt are built from a rendered document and a role name,
    and nothing in the role spec added here may reintroduce one.
    """
    blobs = [json.dumps(GP.ROLE_SPEC), json.dumps(VP.ROLE_SPEC), GP.DESIDERATA_BLOCK]
    blobs += [str(spec) for spec in AP.LENSES.values()]
    for pilot in ("apt", "finchain", "sapg", "weathergen", "ldreg", "sanchez"):
        for blob in blobs:
            assert pilot not in blob.lower(), (pilot, blob[:80])


def test_the_lens_report_schema_still_gates_what_a_sealed_artifact_may_contain():
    """Stripping unknown keys must not depend on remembering to list them.

    The allowed set is derived from the models themselves, so a field added to
    `LensReport` or `Finding` tomorrow is accepted without an edit here, and a key on
    neither model is dropped without one.
    """
    assert audit_driver._LENS_ALLOWED == (
        frozenset(LensReport.model_fields)
        - frozenset(audit_driver.HARNESS_OWNED_REPORT_KEYS))
    assert audit_driver._FINDING_ALLOWED == (
        frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS))
    # A report-level field the HARNESS writes is subtracted for the same reason a
    # finding-level one is. `merged_duplicates` counts what deduplication folded, which
    # happens after a reading has finished, so a reading supplying it would be describing
    # work it did not do — and a sealed artifact would carry the number as if checked.
    for key in audit_driver.HARNESS_OWNED_REPORT_KEYS:
        assert key in LensReport.model_fields, key
        assert key not in audit_driver._LENS_ALLOWED, key
    # The nested rule: an evidence pointer's machine half is the harness's, one level down.
    assert audit_driver._POINTER_ALLOWED == frozenset(("role", "evidence_quote",
                                                      "evidence_ref"))
    assert not (audit_driver._POINTER_ALLOWED & frozenset(audit_driver.POINTER_OWNED_KEYS))
    # And the same for the other two artifacts, whose strip sets are their own schemas —
    # `Grade` is entirely the grader's claim, so nothing on it is harness-owned and the
    # whole model is allowed; `SubstantiveVerdict` likewise.
    assert audit_driver._FINDING_ALLOWED < frozenset(Finding.model_fields)
    assert grade_driver.parse_grade_report(json.dumps(
        {"verdict": "CONFIRMED", "notes": "n", "steelman": "s"})
    )[1]["unknown_keys_dropped"] == 0
    assert set(Grade.model_fields) >= {"verdict", "severity", "confidence"}
    assert "verdict" in SubstantiveVerdict.model_fields
