"""Validation, provenance and sealing for every model-answered role — the four audit
lenses, the blinded second-reader grader, and the whole-paper substantive verdict.

**This module does not dispatch anything.** There is ONE delegation channel now: the
controlling Claude Code session dispatches its own isolated subagents, one per delegable
unit, coordinated by `harness/tasks.py` (`tasks.pending` lists what still needs an answer
and the prompt to give it; `tasks.seal` validates and seals what comes back, using the
`parse_*`/`accept_*`/`load_*` functions below). Nothing in this module — or anywhere else
in this harness — builds a `claude` CLI command line or spawns a subprocess.

What stays here, and why it is still one module rather than three: every role needs the
SAME four things — a validated parse of a delegate's raw JSON answer (tolerant of a CLI
result envelope or a fenced code block, intolerant of a forged harness-owned field), a
content-hash SEAL over the accepted payload (`harness/sealing.py`), a PROVENANCE record
naming which delegation mode produced it (`harness/delegation.py`), and a LOAD that
re-verifies the seal before trusting a cached answer. What genuinely differs per role —
which prompt module built the prompt, which response schema, which model tier — is data
on a `RoleSpec`, not a second copy of the shared validation skeleton.

Governed reconstruction and the authors'-code audit are NOT duplicated here:
`harness/reimplement_driver.py` and `harness/artifact_review_driver.py` are the live
implementations for those two roles.

THINGS WORTH FLAGGING, none of them silently changed:

1. **`HARNESS_OWNED_FINDING_KEYS` stays an OPT-OUT list.**
   `_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)`
   trusts every field of the `Finding` model FROM THE LENS unless it is explicitly named
   as harness-owned. A new `Finding` field added later is trusted by default, not refused
   by default — the opposite of what a from-scratch design would choose. Preserved
   faithfully; inverting it is a bigger decision than this consolidation.

2. **`prompt_sha256` pinning is on every role's every write path.** `accept_verdict` takes
   an optional `prompt_sha256=""` and records it when given, matching the lens/grade
   sealing paths — a stale opinion about a changed set of findings can be told apart from
   a fresh one on every role, not only some.

`python -m harness.agent` runs the self-check.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

from . import delegation, failures, reviewer_cli, sealing, state
from .schema import Finding, Grade, LensReport, SubstantiveVerdict
from .config import Config
from .prompts import audit as AUDIT_P
from .prompts import grade as GRADE_P
from .prompts import verdict as VERDICT_P

# =============================================================================================
# PART 1 -- DELEGATION VOCABULARY  (harness/delegation.py, imported rather than re-inlined)
# =============================================================================================
DELEGATION_MODES = delegation.DELEGATION_MODES
WRITTEN_BY = delegation.WRITTEN_BY
ISOLATION_CLAIM = delegation.ISOLATION_CLAIM

resolve_model = delegation.resolve_model
modes_available = delegation.modes_available
choose = delegation.choose
provenance_record = delegation.provenance_record
mode_of = delegation.mode_of
summarise = delegation.summarise


# =============================================================================================
# PART 2 -- SEALING  (harness/sealing.py, imported rather than re-inlined)
# =============================================================================================
seal = sealing.seal
verify_seal = sealing.verify_seal


def _verify_and_read(out: Path, writers: tuple[str, ...], *, prompt_sha256: str = ""
                     ) -> tuple[dict, dict] | None:
    """(sidecar_record, payload) or None -- the ONE `sealing.verify_seal` call site every
    role's `load_*` function uses. A `prompt_sha256` mismatch is refused the same way on
    every role: absent on EITHER side is never treated as a mismatch, so a sidecar sealed
    before this field existed, or a caller not yet passing one, is not refused over a
    field that did not exist then."""
    ok, _why = verify_seal(out, accepted_writers=writers)
    if not ok:
        return None
    try:
        rec = state.read_json(out.with_suffix(".driver.json"))
        if not isinstance(rec, dict):
            return None
        recorded_prompt = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        return rec, state.read_json(out)
    except Exception:
        return None


def _seal_role(out: Path, payload: dict, record: dict) -> dict:
    """The write half every role's own `_seal`/`seal` used to reimplement independently
    (verdict, reimplement and artifact_review each had a byte-for-byte identical version
    of this function). `mode`/`reviewer`/`tool_policy` are re-derived FROM `record` rather
    than recomputed a second time, because `record` already carries whatever
    `provenance_record` produced."""
    return seal(out, payload, mode=record.get("delegation_mode", "MANUAL"),
               reviewer=record.get("reviewer") or record.get("reader", ""),
               tool_policy=record.get("tool_policy", "unrecorded"), extra=record)


# =============================================================================================
# PART 3 -- FAILURE CLASSIFICATION and ENVELOPE PARSING  (harness/reviewer_cli.py, imported
# rather than re-inlined -- see that module's own docstring for why it still exists)
# =============================================================================================
class AgentError(RuntimeError):
    """A delegate's answer did not parse into a usable result. Self-classifying: every
    instance carries `kind`/`retry`/`reset_hint` from `harness.failures.classify` over its
    own message, so a raise site never has to know which retry-policy bucket its text
    falls into."""

    def __init__(self, message: str, *, kind: str = "", retry: str = "",
                 reset_hint: str = "") -> None:
        super().__init__(message)
        if not kind:
            kind, retry, reset_hint = failures.classify(message)
        self.kind, self.retry, self.reset_hint = kind, retry, reset_hint


class RateLimited(AgentError):
    """A refusal a LATER attempt could satisfy: an account limit, a 429, an overloaded
    upstream, reported IN the delegate's own answer text."""


class NonRetryable(AgentError):
    """A refusal that will fail identically forever: a tool-permission refusal, a model
    the account cannot reach, an unreadable input."""


def driver_error(message: str) -> AgentError:
    """Build the right exception class for a failure message, using
    `reviewer_cli.classify_delegated_failure`'s classification."""
    kind, retry, hint = reviewer_cli.classify_delegated_failure(message)
    cls = {"later": RateLimited, "never": NonRetryable}.get(retry, AgentError)
    return cls(message, kind=kind, retry=retry, reset_hint=hint)


prompt_fingerprint = reviewer_cli.prompt_fingerprint
keep_prompt_copy = reviewer_cli.keep_prompt_copy
prompt_is_unchanged = reviewer_cli.prompt_is_unchanged
unwrap_envelope = reviewer_cli.unwrap_envelope
envelope_provenance = reviewer_cli.envelope_provenance
classify_delegated_failure = reviewer_cli.classify_delegated_failure


# =============================================================================================
# PART 4 -- ROLE METADATA -- the model tier and accepted-writer vocabulary every parse/accept/
# load function below needs. No confinement, no command resolution, no spawn: there is nothing
# left to build an argv from.
# =============================================================================================
@dataclasses.dataclass(frozen=True)
class RoleSpec:
    """What used to be one of five copy-pasted `role_model()` functions, one set per
    driver file, plus the accepted-writer vocabulary each role's `load_*` verifies against."""

    key: str
    model_attr: str              # Config attribute: model override ("" for "lens" -- each
                                  # lens declares its own model in `prompts.audit.LENSES`)
    writers: tuple[str, ...]     # accepted writers when reading a sealed/cached artifact
    role_spec: dict              # `prompts.<role>.ROLE_SPEC` ({} for "lens", which has none)


ROLES: dict[str, RoleSpec] = {
    "lens": RoleSpec(
        key="lens", model_attr="",
        writers=("audit_driver", "session_subagent", "manual_accept"), role_spec={}),
    "grade": RoleSpec(
        key="grade", model_attr="grade_model",
        writers=("grade_driver", "session_subagent", "manual_accept"),
        role_spec=GRADE_P.ROLE_SPEC),
    "verdict": RoleSpec(
        key="verdict", model_attr="verdict_model",
        writers=("verdict_driver", "audit_driver", "session_subagent", "manual_accept"),
        role_spec=VERDICT_P.ROLE_SPEC),
}
# `reimplement`/`artifact_review` are not roles here: their live homes are
# `harness/reimplement_driver.py` and `harness/artifact_review_driver.py`, whose own
# `role_model` reads `cfg.reimplementation_model`/`cfg.artifact_review_model` directly.


def role_model(cfg: Config, role_key: str, **kw) -> str:
    """WHICH model tier a task for this role should request. Never a CLI's ambient
    default -- there is no CLI; this is metadata `harness.tasks.pending` attaches to the
    task dict for whatever dispatches the subagent to read."""
    if role_key == "lens":
        lens = kw.get("lens")
        spec = AUDIT_P.LENSES.get(lens or "", {})
        return str(spec.get("model", "")) if lens else ""
    role = ROLES[role_key]
    override = getattr(cfg, role.model_attr) if role.model_attr else ""
    return resolve_model(override, role.role_spec.get("model", ""))


# =============================================================================================
# PART 5a -- LENS ROLE  (the four audit lenses; parsing only -- dispatch was
# `harness/audit_driver.py`, then `run_lens`/`fill_lenses` here, both removed)
# =============================================================================================
# INVARIANT 2, at the artifact boundary. See the module docstring, point 1: this stays an
# OPT-OUT list, faithfully.
HARNESS_OWNED_FINDING_KEYS: tuple[str, ...] = (
    "verified_observation", "evidence_class", "scientific_class", "verification_state",
    "calc_class", "origin_consistency", "finding_class", "counted_severity",
    "grade_state", "binding_cap", "derivation", "grader_evidence_class",
    "grader_verified_observation", "evidence_ceiling", "evidence_sources", "grade",
    "source_part", "merged_from", "cross_section",
)
POINTER_OWNED_KEYS: tuple[str, ...] = ("evidence_class", "verified_observation")
# `schema_version` decides whether `audit.pass_b_state` treats this report's degeneracy
# fields as checkable (`"legacy"` skips the verification_state == "incomplete" -> NOTE cap
# entirely, invariant 2) -- a lens naming its own version is exactly the self-certification
# invariant 2 forbids, so it is harness-owned and forced below, not merely stripped-if-absent.
HARNESS_OWNED_REPORT_KEYS: tuple[str, ...] = ("merged_duplicates", "schema_version")
# `evidence_origin` is deliberately ABSENT: `stages/audit._coerce` compares the lens's own
# claimed origin against the one derived from the reference's shape, and stripping this key
# here would silently disable that check while leaving it looking like it still runs.
_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)
_LENS_ALLOWED = frozenset(LensReport.model_fields) - frozenset(HARNESS_OWNED_REPORT_KEYS)
_POINTER_ALLOWED = frozenset(("role", "evidence_quote", "evidence_ref"))


def strip_harness_keys(data: dict) -> tuple[dict, int, int]:
    """(clean, n_harness_stripped, n_unknown_dropped) -- a lens report reduced to its own
    words. Two counts: a stripped harness key is an ATTEMPTED FORGERY, a dropped unknown
    key is a reviewer being chatty."""
    if not isinstance(data, dict):
        return {}, 0, 0
    stripped = unknown = 0
    clean: dict = {}
    for key, value in data.items():
        if key in _LENS_ALLOWED:
            clean[key] = value
        elif key in HARNESS_OWNED_REPORT_KEYS:
            stripped += 1
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


def parse_lens_json(text: str, lens: str) -> LensReport:
    return parse_lens_report(text, lens)[0]


def parse_lens_report(text: str, lens: str) -> tuple[LensReport, dict]:
    """(report, meta). Tolerant of a JSON object wrapped in prose or a fenced code block,
    and of a CLI-style result envelope, should a delegate's answer happen to carry one.
    Not tolerant of a missing `findings` key or of a quote with no reference -- an
    unlocatable finding is refused, not persisted."""
    outer = (text or "").strip()
    if not outer:
        raise AgentError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "harness_keys_stripped": 0, "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise driver_error(raw[:300] or "the reviewer reported an error and returned no result")
    if not raw:
        raise AgentError("the reviewer returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AgentError("output JSON is not an object")
    if "findings" not in data:
        raise AgentError("output JSON has no 'findings' key")
    if not isinstance(data["findings"], list):
        raise AgentError("'findings' is not a list")

    for i, f in enumerate(data["findings"]):
        if not isinstance(f, dict):
            raise AgentError(f"findings[{i}] is not an object")
        if (f.get("evidence_quote") or "").strip() and not (f.get("evidence_ref") or "").strip():
            raise AgentError(
                f"findings[{i}] ({f.get('finding_id') or 'unnamed'}) carries an "
                f"evidence_quote but no evidence_ref; a quote with no location cannot be "
                f"verified and must not be persisted")
        for j, side in enumerate(f.get("additional_evidence") or []):
            if not isinstance(side, dict):
                raise AgentError(f"findings[{i}].additional_evidence[{j}] is not an object")
            if not (side.get("evidence_quote") or "").strip():
                raise AgentError(
                    f"findings[{i}].additional_evidence[{j}] carries no evidence_quote; a "
                    f"further location with nothing quoted from it names nothing")
            if not (side.get("evidence_ref") or "").strip():
                raise AgentError(
                    f"findings[{i}].additional_evidence[{j}] carries an evidence_quote but "
                    f"no evidence_ref; the second half of a cross-section concern has to be "
                    f"as locatable as the first")

    data, stripped, unknown = strip_harness_keys(data)
    meta["harness_keys_stripped"] = stripped
    meta["unknown_keys_dropped"] = unknown
    data["lens"] = lens
    # Harness-owned, forced regardless of what strip_harness_keys removed or the lens wrote
    # (invariant 2): a lens naming a stale or absent version would otherwise put its own
    # pass-B degeneracy check into `audit.pass_b_state`'s uncapped "legacy" branch, deciding
    # for itself whether its own falsification/steelman fields are checkable.
    data["schema_version"] = 2
    try:
        return LensReport(**data), meta
    except Exception as e:
        raise AgentError(f"output does not match the lens schema: {e}") from e


# =============================================================================================
# PART 5b -- GRADE ROLE  (the blinded second-reader grader; parsing only)
# =============================================================================================
def parse_grade_json(text: str) -> Grade:
    return parse_grade_report(text)[0]


def parse_grade_report(text: str) -> tuple[Grade, dict]:
    """(grade, meta). Not tolerant of a missing `verdict`. Unknown keys are DROPPED rather
    than kept -- a model-assigned numeric weight or score never survives into the sealed
    grade (invariant 11)."""
    outer = (text or "").strip()
    if not outer:
        raise AgentError("the command wrote an empty file")
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "desiderata_violations": [], "desiderata_passed": None,
                 "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise driver_error(raw[:300] or "the grader reported an error and returned no result")
    if not raw:
        raise AgentError("the grader returned an empty result")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AgentError("output JSON is not an object")
    if not str(data.get("verdict") or "").strip():
        raise AgentError("output JSON has no 'verdict'")

    violations, unknown_desiderata = GRADE_P.parse_violations(data.get("desiderata_violations"))
    meta["desiderata_violations"] = list(violations)
    meta["unknown_desiderata"] = list(unknown_desiderata)
    if data.get("desiderata_violations") is not None or violations:
        meta["desiderata_passed"] = len(violations) == 0
    if unknown_desiderata:
        meta["desiderata_passed"] = False

    clean = {k: v for k, v in data.items() if k in Grade.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return Grade(**clean), meta
    except Exception as e:
        raise AgentError(f"output does not match the grade schema: {e}") from e


# =============================================================================================
# PART 5c -- VERDICT ROLE  (the whole-paper opinion)
# =============================================================================================
def parse_verdict_json(text: str) -> SubstantiveVerdict:
    return parse_verdict_report(text)[0]


def parse_verdict_report(text: str) -> tuple[SubstantiveVerdict, dict]:
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {},
                 "unknown_keys_dropped": 0}
    if envelope and envelope.get("is_error"):
        raise AgentError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise AgentError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("verdict") or "").strip():
        raise AgentError("output JSON has no 'verdict'")
    clean = {k: v for k, v in data.items() if k in SubstantiveVerdict.model_fields}
    meta["unknown_keys_dropped"] = len(data) - len(clean)
    try:
        return SubstantiveVerdict(**clean), meta
    except Exception as e:
        raise AgentError(f"output does not match the schema: {e}") from e


def _verdict_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "reports" / f"{pid}.substantive.json"


def _seal_verdict(cfg: Config, pid: str, verdict: SubstantiveVerdict, record: dict) -> dict:
    return _seal_role(_verdict_path(cfg, pid), verdict.model_dump(),
                      {**record, "paper_id": pid, "verdict": verdict.verdict})


def accept_verdict(cfg: Config, pid: str, raw: str, *, reader: str = "",
                   tool_policy: str = "unrecorded", mode: str = "MANUAL",
                   prompt_sha256: str = "") -> dict:
    """Seal a whole-paper read -- a human, or a session subagent (the only two delegation
    modes now possible)."""
    verdict = parse_verdict_json(raw)
    prov = provenance_record(mode=mode, reviewer=reader, tool_policy=tool_policy)
    record = {"written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
             "reader": prov["reviewer"], "tool_policy": prov["tool_policy"],
             "isolation_claim": prov["isolation_claim"],
             "tool_policy_provable": prov["tool_policy_provable"]}
    if prompt_sha256:
        record["prompt_sha256"] = prompt_sha256
    return _seal_verdict(cfg, pid, verdict, record)


def load_verdict(cfg: Config, pid: str, *, prompt_sha256: str = "") -> SubstantiveVerdict | None:
    got = _verify_and_read(_verdict_path(cfg, pid), ROLES["verdict"].writers,
                          prompt_sha256=prompt_sha256)
    if got is None:
        return None
    _rec, data = got
    try:
        return SubstantiveVerdict(**data)
    except Exception:
        return None


# =============================================================================================
# self-check: python -m harness.agent
# =============================================================================================
if __name__ == "__main__":
    import ast as _ast
    import inspect as _inspect
    import tempfile as _tf

    cfg = Config.load()

    # --- ROLES metadata is the shape validation needs, nothing more ----------------------
    assert set(ROLES) == {"lens", "grade", "verdict"}
    for _role_key in ROLES:
        assert isinstance(ROLES[_role_key].writers, tuple) and ROLES[_role_key].writers

    # --- ROLE_SPEC is READ, never hardcoded -----------------------------------------------
    for _role_key in ("grade", "verdict"):
        assert role_model(Config(), _role_key) == ROLES[_role_key].role_spec["model"]
    assert role_model(Config(grade_model="haiku"), "grade") == "haiku", "an override wins"
    assert role_model(Config(), "lens", lens="overclaim") == \
        AUDIT_P.LENSES["overclaim"].get("model", "")
    assert role_model(Config(), "lens") == "", "no lens named -> no declared model"

    # --- opt-in/opt-out trust boundary: HARNESS_OWNED_FINDING_KEYS is OPT-OUT, preserved -
    _forged = {"lens": "overclaim", "schema_version": 2, "findings": [
        {"finding_id": "o-1", "severity": "MAJOR", "title": "t", "statement": "s",
         "evidence_quote": "q", "evidence_ref": "p1",
         "verified_observation": "THE HARNESS CONFIRMED THIS",
         "evidence_class": "cell_verified", "counted_severity": "FATAL",
         "finding_class": "CONFIRMED_FINDING", "totally_made_up_key": "yes"}], "notes": "n"}
    _rep, _meta = parse_lens_report(json.dumps(_forged), "overclaim")
    _f = _rep.findings[0]
    assert _f.verified_observation == "", "a lens may not certify its own reasoning"
    assert _f.evidence_class == "unverified" and _f.counted_severity == ""
    assert _f.finding_class == "UNGRADED"
    assert _meta["harness_keys_stripped"] == 5, _meta     # 4 finding keys + schema_version
    assert _meta["unknown_keys_dropped"] == 1, _meta
    for _k in HARNESS_OWNED_FINDING_KEYS:
        assert _k in Finding.model_fields, _k          # every harness-owned key is real
    assert "evidence_origin" not in HARNESS_OWNED_FINDING_KEYS

    # --- seal / verify_seal round-trip, and cache invalidation ---------------------------
    with _tf.TemporaryDirectory() as td:
        out = Path(td) / "thing.json"
        seal(out, {"a": 1}, mode="SESSION_SUBAGENT", extra={"prompt_sha256": "a" * 64})
        ok, why = verify_seal(out, accepted_writers=("session_subagent",))
        assert ok, why
        assert _verify_and_read(out, ("session_subagent",), prompt_sha256="a" * 64) is not None
        assert _verify_and_read(out, ("session_subagent",), prompt_sha256="b" * 64) is None, (
            "a changed prompt_sha256 must invalidate the cache")
        out.write_text('{"a": 999}', encoding="utf-8")
        assert _verify_and_read(out, ("session_subagent",)) is None, (
            "an edited artifact is not the artifact that was sealed")

    # --- accept_verdict / load_verdict round-trip, and prompt pinning --------------------
    with _tf.TemporaryDirectory() as td:
        _cfg = Config(projects_dir=Path(td))
        assert load_verdict(_cfg, "p") is None
        _v = SubstantiveVerdict(verdict="STRONG", reason="r", strongest_contribution="c",
                               weakest_link="w", weaknesses_are="LOCAL")
        accept_verdict(_cfg, "p", json.dumps(_v.model_dump()), reader="alice",
                       mode="SESSION_SUBAGENT", prompt_sha256="c" * 64)
        assert load_verdict(_cfg, "p") is not None
        assert load_verdict(_cfg, "p", prompt_sha256="c" * 64) is not None
        assert load_verdict(_cfg, "p", prompt_sha256="d" * 64) is None
        _out = _verdict_path(_cfg, "p")
        state.write_json(_out, {"verdict": "TAMPERED"})
        assert load_verdict(_cfg, "p") is None

    # --- parse_grade_report / parse_verdict_report: minimal round trips ------------------
    _grade, _gmeta = parse_grade_report(json.dumps({"verdict": "CONFIRMED", "severity": "MAJOR"}))
    assert _grade.verdict == "CONFIRMED"
    for _bad in ("", "not json", '{"nope": 1}'):
        try:
            parse_grade_report(_bad)
            raise AssertionError(f"should have refused {_bad!r}")
        except AgentError:
            pass

    # --- parsing: envelope unwrap, prose/fence tolerance --------------------------------
    body = ('Here you go:\n```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
           '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
           '"evidence_ref":"p1"}],"unasked_question":"u","notes":"n"}\n```')
    _rep2 = parse_lens_json(body, "overclaim")
    assert _rep2.lens == "overclaim" and len(_rep2.findings) == 1
    _inner = '{"lens":"overclaim","findings":[]}'
    _env = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": _inner, "session_id": "s-1", "total_cost_usd": 0.5,
                      "num_turns": 3, "modelUsage": {"claude-opus-4": {"in": 1}}})
    _payload, _envelope = unwrap_envelope(_env)
    assert _payload == _inner and _envelope.get("session_id") == "s-1"
    _prov = envelope_provenance(_envelope)
    assert _prov["model_reported"] == "claude-opus-4" and _prov["cost_usd"] == 0.5
    assert "session_id" in _prov["envelope_keys"]

    # --- delegation-specific failure classification -------------------------------------
    assert classify_delegated_failure(
        "Claude requested permissions to use Bash, but you have not granted it"
    )[:2] == ("bad_invocation", "never")
    assert classify_delegated_failure("the random seed was not set")[:2] == ("unknown", "now")
    for _text, _want in (("You've hit your session limit · resets 3:20pm", "later"),
                        ("HTTP 429 Too Many Requests", "later"),
                        ("the request timed out", "now")):
        assert classify_delegated_failure(_text)[1] == _want, _text

    # --- delegation vocabulary: pure decision functions admit only str/bool ------------
    for _fn in (modes_available, provenance_record):
        for _name, _p in _inspect.signature(_fn).parameters.items():
            assert str(_p.annotation) in ("str", "bool"), f"{_fn.__name__}.{_name}"
    assert set(WRITTEN_BY) | {"UNAVAILABLE"} == set(DELEGATION_MODES)
    assert len(set(WRITTEN_BY.values())) == len(WRITTEN_BY)
    cli = provenance_record(mode="CLI_SUBPROCESS", reviewer="claude", tool_policy="enforced: x")
    assert cli["tool_policy_provable"] is True, "reading an OLD seal must still work"
    for _mode in ("SESSION_SUBAGENT", "MANUAL"):
        _rec = provenance_record(mode=_mode, tool_policy="enforced: x")
        assert _rec["tool_policy"] == "unrecorded", _mode

    # --- what this layer must be INCAPABLE of: cannot decide the report's outcome, cannot
    # spawn anything, cannot ask a human -------------------------------------------------
    _src = _inspect.getsource(sys.modules[__name__])
    _tree = _ast.parse(_src)
    _imported: set[str] = set()
    _assigned: set[str] = set()
    _called: set[str] = set()
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
        elif isinstance(_node, _ast.Call):
            _fn_node = _node.func
            if isinstance(_fn_node, _ast.Name):
                _called.add(_fn_node.id)
            elif isinstance(_fn_node, _ast.Attribute):
                _called.add(_fn_node.attr)
    for _forbidden in ("grading", "taxonomy", "stages", "planner", "priority", "report",
                      "subprocess", "os"):
        assert _forbidden not in _imported, (
            f"this module must not reach into harness.{_forbidden} or spawn a process")
    for _decision in ("severity", "counted_severity", "triage", "claim_status",
                     "finding_class", "binding_cap"):
        assert _decision not in _assigned, f"this layer assigned {_decision!r}"
    for _affordance in ("input", "getpass", "confirm", "prompt_user", "approve",
                       "getch", "ask", "Popen", "run", "system"):
        assert _affordance not in _called, (
            f"{_affordance!r} is a human checkpoint or a process spawn in a module that "
            f"has neither")

    print(json.dumps({"self_check": "ok", "roles": sorted(ROLES)}, indent=2))
