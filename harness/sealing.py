"""The shared write-then-verify mechanism every accept/seal instance in this harness
reimplemented independently.

A prior read-only investigation found the identical "seal an artifact, verify it on read"
pattern hand-rolled 9 times inside `harness/`: write the validated model as JSON, hash the
file, write a `.driver.json` sidecar recording `written_by`/`content_sha256`/`ts`, and on
read refuse unless the sidecar exists, parses, names a recognised writer, and its recorded
hash still matches the artifact's current bytes. The 9 instances — `stages/audit.py`
(`accept_lens`/`compose_lens`, and `audit_driver.run_lens`'s own inline seal),
`stages/probe.py` (`accept_spec`), `stages/grade.py` (`accept_grade`, and
`grade_driver.run_candidate`'s own inline seal), `verdict_driver.py` (`_seal`),
`artifact_review_driver.py` (`seal`), `claimlink_driver.py` (`seal`),
`literature_driver.py` (`seal`), `validation_driver.py` (`seal`, currently unreachable from
any live caller) and `reimplement_driver.py` (`_seal`) — shared one identical core and
differed only in schema-specific extras layered on top of it.

**Relationship to `harness/delegation.py`.** Delegation decides the MODE a reasoning task
was carried out under and owns the provenance vocabulary (`WRITTEN_BY`, `ISOLATION_CLAIM`,
`provenance_record`). Sealing writes the artifact-plus-sidecar pair and verifies it later.
This module calls `delegation.provenance_record`, never the reverse — `delegation.py`
cannot import this module at all: its own architectural-purity test
(`tests/test_delegation_modes.py::
test_the_harness_cannot_detect_whether_the_controller_can_delegate`) AST-parses that file
and asserts its imports are a subset of exactly `{shutil, inspect, __future__}`, which
excludes both `hashlib` and `state` — the two things a seal/verify mechanism needs. This
module is a plain sibling with no such restriction.

`python -m harness.sealing` runs the self-check.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import delegation, state


def seal(path: Path, payload: dict, *, mode: str, reviewer: str = "",
         tool_policy: str = "unrecorded", extra: dict | None = None) -> dict:
    """Write `payload` as the artifact at `path`, then a `.driver.json` sidecar recording
    who produced it and a content hash a reader can re-check. The shared write half of
    every accept/seal instance in this harness: `stages/audit.accept_lens`,
    `stages/probe.accept_spec`, `stages/grade.accept_grade`, `verdict_driver.accept_verdict`,
    and five more, each layering its own schema-specific validation before calling this and
    its own extra sidecar fields via `extra` (which is merged in AFTER the provenance
    fields, so an instance may override a provenance-derived field — `stages/probe.py`'s
    `accept_spec` forces `written_by: "driver_accept"` regardless of `mode` this way).

    `payload` is whatever dict the caller has already validated and wants written verbatim
    — usually `some_model.model_dump()`, but `reimplement_driver` seals a wrapper
    `{"script": ..., "conformance": ...}` instead, which is exactly why this takes a plain
    dict rather than doing the model_dump itself.
    """
    state.write_json(path, payload)
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    record = dict(delegation.provenance_record(mode=mode, reviewer=reviewer,
                                                tool_policy=tool_policy))
    record["content_sha256"] = content_sha256
    record["ts"] = state.now()
    record.update(extra or {})
    state.write_json(path.with_suffix(".driver.json"), record)
    return record


def verify_seal(path: Path, *, accepted_writers: tuple[str, ...]) -> tuple[bool, str]:
    """(ok, reason) — the read-side hash+writer check shared by every seal instance's own
    `*_is_accepted`/`load`/`load_accepted`. Refuses if the artifact is missing, if its
    sidecar is missing or unparseable, if the sidecar's `written_by` is not in
    `accepted_writers` (each instance passes its own — `tuple(WRITTEN_BY.values()) + (...)`),
    or if the artifact's current bytes no longer hash to the sidecar's `content_sha256`.
    Callers needing more (a prompt re-mint check, a commit match, an independent-
    verification cross-check) read the sidecar again themselves afterward and layer their
    own refusal on top — this function only ever answers the question every instance asks
    identically.
    """
    if not path.exists():
        return False, "no output file"
    sidecar = path.with_suffix(".driver.json")
    if not sidecar.exists():
        return False, ("no provenance sidecar (.driver.json) — most likely side-written "
                       "instead of produced through the validated staging path")
    try:
        rec = state.read_json(sidecar)
    except Exception:
        return False, "provenance sidecar is not valid JSON"
    if not isinstance(rec, dict) or rec.get("written_by") not in accepted_writers:
        return False, (f"provenance sidecar written_by="
                       f"{rec.get('written_by') if isinstance(rec, dict) else None!r} "
                       f"not recognized")
    want = rec.get("content_sha256")
    if not want:
        return False, "provenance sidecar has no content_sha256"
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        return False, "output file content changed after its provenance sidecar was written"
    return True, ""


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "thing.json"

        # --- a basic seal round-trips and verifies ------------------------------------
        rec = seal(out, {"a": 1}, mode="CLI_SUBPROCESS", reviewer="claude",
                  tool_policy="enforced: tools=Read", extra={"paper_id": "p1"})
        assert rec["written_by"] == "audit_driver"
        assert rec["delegation_mode"] == "CLI_SUBPROCESS"
        assert rec["paper_id"] == "p1"
        assert len(rec["content_sha256"]) == 64
        ok, why = verify_seal(out, accepted_writers=("audit_driver",))
        assert ok is True and why == ""

        # --- extra, merged AFTER provenance, may override a provenance-derived field --
        overridden = seal(out, {"a": 2}, mode="CLI_SUBPROCESS",
                          extra={"written_by": "driver_accept"})
        assert overridden["written_by"] == "driver_accept"
        ok, why = verify_seal(out, accepted_writers=("audit_driver",))
        assert ok is False and "not recognized" in why
        ok, why = verify_seal(out, accepted_writers=("driver_accept",))
        assert ok is True

        # --- missing file, missing sidecar, unparseable sidecar, wrong writer ----------
        missing = root / "absent.json"
        assert verify_seal(missing, accepted_writers=("x",)) == (False, "no output file")

        no_sidecar = root / "no_sidecar.json"
        no_sidecar.write_text("{}", encoding="utf-8")
        ok, why = verify_seal(no_sidecar, accepted_writers=("x",))
        assert ok is False and "no provenance sidecar" in why

        bad_json = root / "badjson.json"
        bad_json.write_text("{}", encoding="utf-8")
        bad_json.with_suffix(".driver.json").write_text("not json", encoding="utf-8")
        ok, why = verify_seal(bad_json, accepted_writers=("x",))
        assert ok is False and "not valid JSON" in why

        # --- a file edited after sealing is not the file that was sealed --------------
        tampered = root / "tampered.json"
        seal(tampered, {"a": 1}, mode="MANUAL", extra={"written_by": "manual_accept"})
        tampered.write_text('{"a": 999}', encoding="utf-8")
        ok, why = verify_seal(tampered, accepted_writers=("manual_accept",))
        assert ok is False and "changed after" in why

    print("harness.sealing self-check ok")


if __name__ == "__main__":
    _self_check()
