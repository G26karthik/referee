"""Delegating the focused-validation design, and relocating every value it proposes.

`python -m harness.validation_driver` runs the self-check.

One best-effort call per target, never retried, and its failure changes nothing: with the
gate closed — or the CLI unreachable, or the response malformed — the route still runs,
and every design reports `SPECIFICATION_BLOCKED` naming the ingredients the paper does not
bind. That is the property `allow_grading`, `allow_claim_links`, `allow_artifact_review`
and `allow_literature_review` all have, and it is what makes a model channel measurable: a
channel nobody can turn off cannot be compared against one that never ran.

**The designer touches nothing.** The confinement grants NO tools — not `Read`, not
`Grep`. Everything it needs is printed into the prompt: the question, its anchor sentence,
the two arms the paper already reports, and the method text recovered for them. A designer
with `Read` could reach this harness's own artifacts, including the findings and the
grades it must not see.

**And what comes back is RELOCATED, not merged.** A configuration value arrives with the
sentence the designer read it from, and `relocate_configuration` re-mints that sentence
against the parsed paper: a value whose sentence does not resolve is dropped, and an arm
whose remaining keys do not cover what the experiment needs leaves the design blocked.
That is `claims.mint` for a scientific choice, on the same argument
`artifact_evidence.relocate` makes for a line of source — the writer supplies a quotation,
the HARNESS finds it, and a citation that is absent or non-unique is dropped rather than
softened.

**The fields that would make this channel a verdict are stripped at the boundary.**
`state`, `conformance`, `authority`, `answers_question`, `established`, `missing` and
`assumed` are the harness's to write, and so is anything spelled `settled`, `defect`,
`severity` or `paper_decision` — none of which is a field on `ValidationDesign` at all,
which is the point: a designer writing one is not overriding a value, it is inventing a
vocabulary this system does not have. Invariant 2, on a fifth channel.
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

from . import claims, delegation, reviewer_cli
from .artifacts import (NEVER_ASSUMED, PREDICTED_DIRECTIONS,
                        PaperDoc, SETTLEMENT_RULES, SettlementCondition)
from .config import Config
from .prompts import validation as VP
from .reviewer_cli import (Confinement, _kill_tree, denied_tools, operator_confinement,
                           unwrap_envelope, write_pinned_settings)

# What a designer may supply. Everything that decides what the design IS, or what its
# result would be entitled to say, is the harness's.
_DESIGN_KEYS = ("buildable", "changed_variable", "controlled_variables", "arms",
                "settlement", "unstated", "notes")
HARNESS_OWNED_DESIGN_KEYS = (
    "state", "conformance", "conformance_basis", "authority", "answers_question",
    "established", "missing", "assumed", "design_id", "question_ref", "metric_unit",
    "split", "comparison_id", "statement",
    # THE FOUR THAT WOULD MAKE THIS CHANNEL A VERDICT. None is a field on
    # `ValidationDesign`, and a designer writing one is inventing a vocabulary this system
    # does not have rather than overriding a value it does.
    "settled", "defect", "severity", "paper_decision",
)
_SETTLEMENT_KEYS = ("rule", "predicted_direction", "tolerance", "tolerance_basis",
                    "statement")


class ValidationDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return delegation.resolve_model(cfg.validation_model, VP.DESIGN_ROLE_SPEC.get("model", ""))


def design_confinement(cfg: Config, *, settings_sha256: str = "") -> Confinement:
    """NO TOOLS. The designer is given the paper's own text and asked to read it.

    Zero rather than `Read`, for the reason every reader in this system gets zero unless
    it demonstrably needs more: the passages it must quote from are printed in the prompt,
    so a quotation it returns can be checked against text this harness already holds. A
    designer that opened a file could quote something `claims.mint` will never find.
    """
    if cfg.validation_cmd.strip():
        return operator_confinement("focused validation designer")
    return Confinement(
        role="validation", allowed_tools=(), disallowed_tools=denied_tools(()),
        add_dir=(), model=role_model(cfg), restricted=True, strict_mcp=True, bare=False,
        settings_sha256=settings_sha256, output_format="json",
    )


def default_cmd(cfg: Config | None = None, *, exe: str = "", settings: str = "") -> str:
    cfg = cfg or Config()
    exe = delegation.resolve_reviewer_exe(exe, cfg.reviewer_exe)
    if not exe:
        return ""
    extra = design_confinement(
        Config(validation_cmd="", validation_model=cfg.validation_model)).flags(settings)
    return reviewer_cli.cli_pipe_command(exe, extra)


def resolve_cmd(cfg: Config, *, settings: str = "") -> str:
    return cfg.validation_cmd.strip() or default_cmd(cfg, exe=cfg.reviewer_exe,
                                                     settings=settings)


def available(cfg: Config) -> tuple[bool, str]:
    if not cfg.allow_validation_design:
        return False, ("focused-validation design gate is closed: "
                       "SH_ALLOW_VALIDATION_DESIGN is not set. With it closed the route "
                       "still runs and every design reports SPECIFICATION_BLOCKED naming "
                       "the ingredients the paper does not bind")
    return delegation.check_command(resolve_cmd(cfg), "SH_VALIDATION_CMD")


# --------------------------------------------------------------------------------------
# PARSING — a proposal, and nothing here is a ValidationDesign
# --------------------------------------------------------------------------------------
def _outer(text: str) -> dict:
    """The JSON object inside the CLI's envelope, however the designer wrapped it."""
    raw = (text or "").strip()
    if not raw:
        raise ValidationDriverError("the command wrote an empty file")
    try:
        body, envelope = unwrap_envelope(raw)
    except Exception as exc:                       # noqa: BLE001 — any shape but ours
        raise ValidationDriverError(f"unparseable response: {exc}") from exc
    if envelope and envelope.get("is_error"):
        raise ValidationDriverError(f"the designer reported an error: {str(body)[:200]}")
    if isinstance(body, dict):
        return body
    inner = str(body or "")
    start, end = inner.find("{"), inner.rfind("}")
    if start < 0 or end <= start:
        raise ValidationDriverError(
            f"no JSON object in the output (first 120 chars: {inner[:120]!r})")
    try:
        parsed = json.loads(inner[start:end + 1])
    except ValueError as exc:
        raise ValidationDriverError(f"response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationDriverError(f"response was {type(parsed).__name__}, not an object")
    return parsed


def parse_design(text: str) -> tuple[dict, dict]:
    """(proposal, meta). A plain dict, deliberately.

    A `ValidationDesign` carries a `state` and a `conformance`, and both are the harness's
    to write. Returning a dict means there is no moment at which an unverified proposal
    exists wearing the type that means "this experiment could be built".
    """
    body = _outer(text)
    stripped = dropped = 0
    clean: dict = {}
    for key, value in body.items():
        k = str(key).strip()
        if k in HARNESS_OWNED_DESIGN_KEYS:
            stripped += 1
            continue
        if k not in _DESIGN_KEYS:
            dropped += 1
            continue
        clean[k] = value

    out: dict = {
        "buildable": bool(clean.get("buildable")),
        "changed_variable": " ".join(str(clean.get("changed_variable") or "").split()),
        "controlled_variables": [" ".join(str(v).split())
                                 for v in (clean.get("controlled_variables") or [])
                                 if str(v).strip()],
        "arms": {}, "settlement": {},
        "unstated": [" ".join(str(v).split()) for v in (clean.get("unstated") or [])
                     if str(v).strip()],
        "notes": str(clean.get("notes") or ""),
    }
    arms = clean.get("arms")
    if isinstance(arms, dict):
        for role, cfg in arms.items():
            if str(role).strip() not in ("control", "treatment") or not isinstance(cfg, dict):
                continue
            keys: dict = {}
            for name, spec in cfg.items():
                if isinstance(spec, dict):
                    keys[str(name).strip()] = {"value": str(spec.get("value") or ""),
                                               "quote": str(spec.get("quote") or "")}
                else:
                    # A BARE VALUE HAS NO QUOTATION, so it can never relocate. Kept rather
                    # than discarded here so `relocate_configuration` reports it as
                    # unquoted, which is a different fact from never having been offered.
                    keys[str(name).strip()] = {"value": str(spec), "quote": ""}
            out["arms"][str(role).strip()] = keys

    settlement = clean.get("settlement")
    if isinstance(settlement, dict):
        for k in _SETTLEMENT_KEYS:
            if k in settlement:
                out["settlement"][k] = settlement[k]

    meta = {"harness_keys_stripped": stripped, "unknown_keys_dropped": dropped}
    return out, meta


# --------------------------------------------------------------------------------------
# RELOCATION — a value the paper does not say is not a value
# --------------------------------------------------------------------------------------
def relocate_configuration(doc: PaperDoc, proposed: dict, *, artifact_text: str = ""
                           ) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(configuration, basis, dropped) for ONE arm.

    `claims.mint` for a scientific choice. Every key arrives with the sentence the designer
    read it from; the harness re-mints that sentence against the parsed paper and keeps the
    key only when the sentence RESOLVES — which means it is in this document and occurs
    exactly once, since `mint` refuses an ambiguous quotation.

    A quotation that is not in the paper may still be in the PINNED CHECKOUT, and that is
    an admissible source for a configuration: `artifact_text` is the checkout text this
    harness printed into the prompt, so a substring of it is a line this harness holds.
    Anything else is dropped and named.
    """
    configuration: dict[str, str] = {}
    basis: dict[str, str] = {}
    dropped: list[str] = []
    for key, spec in (proposed or {}).items():
        value = str((spec or {}).get("value") or "").strip()
        quote = str((spec or {}).get("quote") or "").strip()
        if not value:
            dropped.append(f"{key}: no value")
            continue
        if not quote:
            dropped.append(f"{key}: no quotation to relocate")
            continue
        got = claims.mint(doc, quote)
        if got.resolution == "resolved":
            configuration[key] = value
            basis[key] = got.ref
            continue
        if artifact_text and quote in artifact_text:
            configuration[key] = value
            basis[key] = "the pinned checkout, as printed in the prompt"
            continue
        dropped.append(f"{key}: {got.detail or 'the quotation is not in this paper'}")
    return configuration, basis, dropped


def settlement_from(proposed: dict) -> SettlementCondition | None:
    """The declared condition, or None when what came back is not one.

    `declared_before_execution` is set HERE, by the harness, at the moment the design is
    built and before any process exists. It is not a field the designer can write: a rule
    that claimed to have been declared first would be exactly the post-hoc threshold this
    whole arrangement refuses.
    """
    p = proposed or {}
    rule = str(p.get("rule") or "").strip().upper()
    direction = str(p.get("predicted_direction") or "").strip().upper()
    if rule not in SETTLEMENT_RULES or direction not in PREDICTED_DIRECTIONS:
        return None
    tolerance = p.get("tolerance")
    try:
        tol = None if tolerance is None else float(tolerance)
    except (TypeError, ValueError):
        tol = None
    return SettlementCondition(
        rule=rule, tolerance=tol,
        tolerance_basis=" ".join(str(p.get("tolerance_basis") or "").split()),
        predicted_direction=direction,
        statement=" ".join(str(p.get("statement") or "").split()),
        declared_before_execution=True)


# --------------------------------------------------------------------------------------
# THE CALL
# --------------------------------------------------------------------------------------
def call(cfg: Config, prompt_text: str, *, tag: str = "validation") -> tuple[str, dict]:
    """One confined call. ('', record) on ANY failure — and the record says which failure.

    A silent None makes "the designer found nothing to build" and "the designer never
    answered" indistinguishable, and those are opposite facts: the first is a statement
    about the paper's method section and the second is a statement about this host.
    """
    started = time.time()
    record: dict = {"tag": tag, "seconds": 0.0}
    ok, why = available(cfg)
    if not ok:
        record.update({"failure": "gate_or_command", "detail": why})
        return "", record
    with tempfile.TemporaryDirectory(prefix="sh-val-") as td:
        prompt, out = Path(td) / "prompt.md", Path(td) / "out.txt"
        prompt.write_text(prompt_text, encoding="utf-8")
        policy_dir = Path(tempfile.mkdtemp(prefix="sh-val-policy-"))
        conf = design_confinement(cfg)
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
        sandbox = Path(tempfile.mkdtemp(prefix="sh-val-sandbox-"))
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
                stdout, stderr = proc.communicate(timeout=cfg.validation_timeout_s)
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
if __name__ == "__main__":       # self-check: python -m harness.validation_driver
    from .artifacts import Section

    assert available(Config(allow_validation_design=False))[0] is False
    assert "SH_ALLOW_VALIDATION_DESIGN" in available(Config(allow_validation_design=False))[1]
    bad = Config(allow_validation_design=True, validation_cmd="reader --in {prompt}")
    assert available(bad)[0] is False, "a template with no {out} must be refused"
    assert available(Config(allow_validation_design=True,
                            validation_cmd="cp {prompt} {out}")) == (True, "")

    # NO TOOLS AT ALL, and the ones that would reach the filesystem are denied by name.
    conf = design_confinement(Config(allow_validation_design=True))
    assert conf.allowed_tools == (), conf.allowed_tools
    for tool in ("WebSearch", "WebFetch", "Bash", "Read", "Grep", "Write"):
        assert tool in conf.disallowed_tools, tool
    assert conf.model == VP.DESIGN_ROLE_SPEC["model"] and conf.enforced is True

    # A DESIGNER MAY NOT SIGN THE HARNESS'S NAME.
    proposal, meta = parse_design(json.dumps({
        "buildable": True, "changed_variable": "  the   regulariser ",
        "controlled_variables": ["schedule", " "],
        "arms": {"control": {"regulariser": {"value": "off", "quote": "Q1"}},
                 "treatment": {"regulariser": {"value": "on", "quote": "Q2"}},
                 "placebo": {"x": {"value": "1", "quote": "Q"}}},
        "settlement": {"rule": "DIRECTION_AGREES", "predicted_direction": "INCREASE"},
        "state": "DESIGNED", "conformance": "CONFORMANT", "established": True,
        "answers_question": True, "severity": "FATAL", "paper_decision": "RED",
        "made_up_key": 1}))
    assert meta["harness_keys_stripped"] == 6, meta
    assert meta["unknown_keys_dropped"] == 1, meta
    assert proposal["changed_variable"] == "the regulariser"
    assert proposal["controlled_variables"] == ["schedule"]
    assert set(proposal["arms"]) == {"control", "treatment"}, "a third arm is not a role"
    assert "state" not in proposal and "severity" not in proposal

    # AN UNKNOWN SETTLEMENT RULE IS NOT RENAMED INTO A USABLE ONE.
    assert settlement_from({"rule": "IT_LOOKS_BETTER", "predicted_direction": "INCREASE"}) is None
    assert settlement_from({"rule": "DIRECTION_AGREES", "predicted_direction": "UP"}) is None
    got = settlement_from({"rule": "direction_agrees", "predicted_direction": "increase",
                           "tolerance": "not a number"})
    assert got is not None and got.rule == "DIRECTION_AGREES" and got.tolerance is None
    assert got.declared_before_execution is True, (
        "the harness writes this, before anything runs, and the designer cannot")

    # RELOCATION: a value the paper does not say is not a value.
    doc = PaperDoc(paper_id="p", title="T", sections=[Section(
        section_idx=0, title="Setup",
        text=("All models train for 200 epochs with SGD. "
              "The regulariser is disabled in the ablation arm."))])
    cfgmap, basis, dropped = relocate_configuration(doc, {
        "epochs": {"value": "200", "quote": "All models train for 200 epochs with SGD."},
        "regulariser": {"value": "off",
                        "quote": "The regulariser is disabled in the ablation arm."},
        "learning_rate": {"value": "0.1", "quote": "We use a learning rate of 0.1."},
        "optimizer": {"value": "SGD", "quote": ""},
        "batch_size": {"value": "", "quote": "anything"}})
    assert set(cfgmap) == {"epochs", "regulariser"}, cfgmap
    assert basis["epochs"].startswith("P0:"), basis
    assert len(dropped) == 3 and any("learning_rate" in d for d in dropped)
    assert any("optimizer: no quotation" in d for d in dropped)
    # AND THE DROPPED ONES ARE THE ONES THAT MATTER — every one is a NEVER_ASSUMED key,
    # so the design that would have used them is blocked rather than completed.
    assert {d.split(":")[0] for d in dropped} <= set(NEVER_ASSUMED)

    # A QUOTATION FROM THE PINNED CHECKOUT IS ADMISSIBLE; one from nowhere is not.
    from_repo, repo_basis, _ = relocate_configuration(
        doc, {"seed_policy": {"value": "0-4", "quote": "SEEDS = range(5)"}},
        artifact_text="train.py:12: SEEDS = range(5)")
    assert from_repo == {"seed_policy": "0-4"} and "checkout" in repo_basis["seed_policy"]

    # A MALFORMED RESPONSE IS A FAILURE, never a partial design.
    for bad_text in ("", "   ", "not json", "[1, 2]", '{"buildable"'):
        try:
            parse_design(bad_text)
        except ValidationDriverError:
            pass
        else:
            raise AssertionError(f"parsed {bad_text!r}")

    # THE GATE BEING SHUT IS A RECORDED OUTCOME, not a silent None.
    body, record = call(Config(allow_validation_design=False), "prompt")
    assert body == "" and record["failure"] == "gate_or_command"
    assert "SH_ALLOW_VALIDATION_DESIGN" in record["detail"]
    print("harness.validation_driver self-check ok")
