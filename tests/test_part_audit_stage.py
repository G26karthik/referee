"""Reading a long paper in parts, driven through the real audit stage and controller.

`tests/test_cross_part_reading.py` pins the reading design as pure functions. These tests
pin what the PIPELINE does with it: how many readings are dispatched, what each one is
allowed to see, what is written to disk, what a resumed run re-runs, and what the terminal
transition refuses.

Every one of them runs a scripted reviewer as a real subprocess through
`audit_driver.run_lens`, so the prompt each reading receives is the prompt the harness
actually wrote, byte for byte. That matters most for the isolation claims: a test that
asked the harness whether part 2 had seen part 1 would be asking the thing under test. A
test that reads part 2's prompt off disk and looks for part 1's words is not.

The claim these support, stated as it will be stated in the paper:

    Each scientific lens is isolated from the other lenses. Long papers are traversed in
    bounded parts without exposing one part's model findings to the next; a final
    lens-local synthesis combines only quotation-grounded observations from that lens to
    recover cross-section relationships.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import audit_driver, state
from harness.artifacts import PaperDoc, Section, Table
from harness.config import Config
from harness.controller import load_case
from harness.stages import audit as audit_stage

PID = "parts-paper"
BUDGET = "6000"          # small enough that a short fixture needs two passes


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv("SH_AUDIT_BUDGET_CHARS", BUDGET)
    return Config(projects_dir=tmp_path / "projects")


ABSTRACT_CLAIM = "we reduce peak memory by 40% across every benchmark"
CONCLUSION_CONCESSION = "peak memory was comparable to the baseline on two of five tasks"
METHOD_LINE = "each model is trained for 100 epochs with a cosine schedule"
PROSE_ABOUT_TABLE = "our method reaches 91.4 accuracy, a gain of six points"


def _long(marker: str, n: int = 5200) -> str:
    """Filler that is unique per section, so a prompt can be attributed to its part."""
    return (marker + " ") * (n // (len(marker) + 1))


def _two_part_doc(pid: str = PID) -> PaperDoc:
    """A paper whose claim and its retraction are at opposite ends, long enough to split."""
    return PaperDoc(
        paper_id=pid, title="A Two Part Paper", n_pages=9, content_sha="b" * 12,
        sections=[
            Section(section_idx=0, title="Abstract", page_start=1,
                    text=f"We propose a method. {ABSTRACT_CLAIM}."),
            Section(section_idx=1, title="Method", page_start=2,
                    text=f"{METHOD_LINE}. " + _long("methodfiller")),
            Section(section_idx=2, title="Results", page_start=5,
                    text=f"{PROSE_ABOUT_TABLE}. " + _long("resultsfiller")),
            Section(section_idx=3, title="Conclusion", page_start=9,
                    text=f"In closing, {CONCLUSION_CONCESSION}."),
        ],
        tables=[Table(table_idx=0, page=5, caption="Table 1: accuracy",
                      rows=[["method", "acc"], ["ours", "85.4"]])])


def _one_part_doc(pid: str = PID) -> PaperDoc:
    return PaperDoc(
        paper_id=pid, title="A Short Paper", n_pages=2, content_sha="c" * 12,
        sections=[Section(section_idx=0, title="Results", page_start=2,
                          text="The proposed method reaches 91.4 accuracy on the held-out "
                               "split.")],
        tables=[Table(table_idx=0, page=2, caption="Table 1: results",
                      rows=[["method", "acc"], ["ours", "91.4"]])])


def _plant(cfg: Config, doc: PaperDoc) -> str:
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    if not (cfg.projects_dir / doc.paper_id / "project.json").exists():
        state.create_project(cfg, "", doc.title, pid=doc.paper_id)
    state.write_json(cfg.projects_dir / doc.paper_id / "paper" / "doc.json", doc.model_dump())
    return doc.paper_id


def _finding(fid: str, quote: str, ref: str, *, severity="MINOR", statement="a concern",
             extra: list[dict] | None = None) -> dict:
    out = {"finding_id": fid, "severity": severity, "title": fid, "statement": statement,
           "evidence_quote": quote, "evidence_ref": ref}
    if extra is not None:
        out["additional_evidence"] = extra
    return out


class Reviewer:
    """A scripted reviewer, run as a real subprocess, answering per reading unit.

    Keyed on the unit id the harness stamps into the prompt header, so a response is bound
    to the reading it answers and a test cannot accidentally satisfy a unit it did not
    mean to. Every prompt is copied out as it was received, which is what the isolation
    tests read.
    """

    def __init__(self, cfg: Config, responses: dict[str, dict]):
        self.dir = cfg.projects_dir.parent / "reviewer"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.map_path = self.dir / "responses.json"
        self.calls_dir = self.dir / "calls"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps(responses), encoding="utf-8")
        script = self.dir / "reviewer.py"
        # One call record PER FILE, named for the unit that produced it, rather than one
        # shared appended log: `fill()` now dispatches several of these as real, separate
        # subprocesses at once (bounded concurrency), and concurrent processes appending
        # to one shared file are not guaranteed atomic — exactly the isolation `fill()`'s
        # own docstring says several real subprocesses at once is stronger than, not
        # weaker, provided each writes somewhere only it owns.
        script.write_text(
            "import json, pathlib, re, sys\n"
            "prompt, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "body = prompt.read_text(encoding='utf-8')\n"
            "unit = re.search(r'# Audit lens: `([^`]+)`', body).group(1)\n"
            f"responses = json.loads(pathlib.Path({str(self.map_path)!r}).read_text('utf-8'))\n"
            f"calls_dir = pathlib.Path({str(self.calls_dir)!r})\n"
            "safe = unit.replace('/', '__')\n"
            "(calls_dir / (safe + '.json')).write_text(json.dumps(\n"
            "    {'unit': unit, 'prompt': str(prompt), 'chars': len(body)}), encoding='utf-8')\n"
            f"(pathlib.Path({str(self.dir)!r}) / (safe + '.prompt.md'))"
            ".write_text(body, encoding='utf-8')\n"
            "payload = responses.get(unit)\n"
            "if payload is None:\n"
            "    payload = {'lens': unit.split('/')[0], 'schema_version': 2, 'findings': []}\n"
            "out.write_text(json.dumps(payload), encoding='utf-8')\n",
            encoding="utf-8")
        self.template = f'"{sys.executable}" "{script}" "{{prompt}}" "{{out}}"'

    @property
    def calls(self) -> list[dict]:
        # By mtime, not by name: several calls within one `fill()` batch now run as
        # concurrent subprocesses and finish in no guaranteed order, but `fill()` itself
        # is still one phase at a time, so calls from an EARLIER batch reliably precede
        # calls from a later one — which is the only ordering property any test here
        # actually relies on (slicing "the calls made in this second batch").
        files = sorted(self.calls_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def prompt_for(self, unit_id: str) -> str:
        return (self.dir / (unit_id.replace("/", "__") + ".prompt.md")).read_text("utf-8")


def _blank(units) -> dict[str, dict]:
    return {u: {"lens": u.split("/")[0], "schema_version": 2, "findings": []} for u in units}


def _units(cfg: Config, pid: str) -> list[str]:
    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    return [u.unit_id for u in audit_stage.units_for(
        cfg.projects_dir / pid, audit_stage.LENSES, audit_stage.plan_for(doc))]


def _case(cfg: Config, pid: str):
    case = load_case(cfg, pid)
    if case is None:
        from harness.artifacts import CaseState
        case = CaseState(paper_id=pid, phase="audit")
        state.write_json(cfg.projects_dir / pid / "case.json", case.model_dump())
    return case


# --------------------------------------------------------------------------- #
# 1. A paper that fits is unchanged
# --------------------------------------------------------------------------- #
def test_a_one_part_paper_is_one_reading_per_lens_and_no_synthesis(cfg: Config):
    """The historical shape, preserved. A synthesis over one part synthesises nothing, and
    dispatching it would spend a model call to combine a set with itself."""
    pid = _plant(cfg, _one_part_doc())
    units = _units(cfg, pid)
    assert units == list(audit_stage.LENSES), units

    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    assert res["parts"] == 1
    assert res["deferred"] == []
    assert sorted(res["prompts"]) == sorted(audit_stage.LENSES)
    for lens in audit_stage.LENSES:
        assert Path(res["prompts"][lens]).name == f"{lens}.md"
        assert "WHICH PART OF THE PAPER THIS IS" not in Path(res["prompts"][lens]).read_text("utf-8")

    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    assert len(reviewer.calls) == len(audit_stage.LENSES) == 4
    after = audit_stage.run_audit(cfg, pid)
    assert after["awaiting"] == [] and after["deferred"] == []
    assert audit_stage.missing_reading_artifacts(cfg, pid) == []
    # and no parts tree was created for a paper that did not need one
    assert not (cfg.projects_dir / pid / "audit" / "overclaim").exists()


# --------------------------------------------------------------------------- #
# 2. A paper that does not fit dispatches every part, then one synthesis per lens
# --------------------------------------------------------------------------- #
def test_a_two_part_paper_dispatches_both_parts_then_one_synthesis_per_lens(cfg: Config):
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    assert units.count("overclaim/part-01") == 1
    assert "overclaim/synthesis" in units
    parts = {u for u in units if "/part-" in u}
    assert len(parts) == 4 * (len(parts) // 4) and len(parts) >= 8, units

    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0

    first = audit_stage.run_audit(cfg, pid)
    assert first["parts"] >= 2
    assert set(first["awaiting"]) == parts, "only the parts can be dispatched yet"
    assert set(first["deferred"]) == {f"{ln}/synthesis" for ln in audit_stage.LENSES}, (
        "a synthesis prompt is not written before the observations it reads exist")

    audit_driver.fill(cfg, pid, first["awaiting"], first["prompts"], units=first["units"])
    second = audit_stage.run_audit(cfg, pid)
    assert set(second["awaiting"]) == {f"{ln}/synthesis" for ln in audit_stage.LENSES}
    assert second["deferred"] == []

    audit_driver.fill(cfg, pid, second["awaiting"], second["prompts"], units=second["units"])
    third = audit_stage.run_audit(cfg, pid)
    assert third["awaiting"] == [] and third["deferred"] == []
    assert sorted(third["lenses_complete"]) == sorted(audit_stage.LENSES)
    assert audit_stage.missing_reading_artifacts(cfg, pid) == []
    assert len(reviewer.calls) == len(units)


# --------------------------------------------------------------------------- #
# 2b. `tools/subagent_accept.py` — the SESSION_SUBAGENT manual-fill path for a
# split paper. No test file existed for this tool before this one.
# --------------------------------------------------------------------------- #
def test_subagent_accept_fills_a_split_papers_parts_and_synthesis_with_no_subprocess(
        cfg: Config):
    """`accept_lens` only ever writes the COMPOSED `audit/<lens>.json`, which does not

    exist until every part and synthesis is sealed — so a paper needing part-splitting
    had no manual-fill path at all before `tools/subagent_accept.py`. This drives the
    same two-part paper as the test above end to end through `seal`/`pending`/`sweep`
    alone, one call per unit, exactly as a controller filling lenses through its own
    subagents would, and checks that `run_audit`'s own `compose_lens` picks the lens up
    the moment its units are in.
    """
    from tools import subagent_accept as sa

    pid = _plant(cfg, _two_part_doc())

    first = audit_stage.run_audit(cfg, pid)  # writes part prompts; dispatches nothing
    part_units = [u for u in first["awaiting"] if "/part-" in u]
    assert part_units and all("/synthesis" not in u for u in part_units)

    pend = {row["unit_id"]: row for row in sa.pending(cfg, pid)}
    for uid in part_units:
        assert pend[uid]["state"] == "PENDING"
        staged = cfg.projects_dir / pid / "audit" / sa.STAGE_DIR / f"{uid.replace('/', '__')}.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(
            {"lens": uid.split("/")[0], "schema_version": 2, "findings": []}),
            encoding="utf-8")

    swept = sa.sweep(cfg, pid)
    assert len(swept) == len(part_units) and all("error" not in r for r in swept)
    for rec in swept:
        assert rec["delegation_mode"] == "SESSION_SUBAGENT"
        assert rec["tool_policy"] == "unrecorded", "this mode cannot prove a tool policy"

    second = audit_stage.run_audit(cfg, pid)
    synth_units = [u for u in second["awaiting"] if u.endswith("/synthesis")]
    assert set(synth_units) == {f"{ln}/synthesis" for ln in audit_stage.LENSES}, (
        "sealing every part through subagent_accept must unblock that lens's synthesis")

    for uid in synth_units:
        staged = cfg.projects_dir / pid / "audit" / sa.STAGE_DIR / f"{uid.replace('/', '__')}.json"
        staged.write_text(json.dumps(
            {"lens": uid.split("/")[0], "schema_version": 2, "findings": []}),
            encoding="utf-8")
    swept2 = sa.sweep(cfg, pid)
    assert len(swept2) == len(synth_units) and all("error" not in r for r in swept2)

    third = audit_stage.run_audit(cfg, pid)
    assert third["awaiting"] == [] and third["deferred"] == []
    assert sorted(third["lenses_complete"]) == sorted(audit_stage.LENSES)
    assert audit_stage.missing_reading_artifacts(cfg, pid) == []

    # The composed lens file exists and honestly reports how it was assembled: from
    # sealed units, not by a CLI subprocess this run never spawned.
    composed = state.read_json(cfg.projects_dir / pid / "audit" / "overclaim.json")
    assert composed["lens"] == "overclaim"
    driver = state.read_json(cfg.projects_dir / pid / "audit" / "overclaim.driver.json")
    assert driver["written_by"] == "composed_from_parts"


def test_every_part_artifact_records_the_span_it_was_given(cfg: Config):
    """Requirement 1: a part output is reconstructible and its input is on disk."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    audit_stage.run_audit(cfg, pid)

    root = cfg.projects_dir / pid
    plan = audit_stage.plan_for(PaperDoc(**state.read_json(root / "paper" / "doc.json")))
    all_units = audit_stage.units_for(root, audit_stage.LENSES, plan)
    anchors = set()
    for unit in all_units:
        rec = state.read_json(unit.manifest_path)
        assert rec["lens"] == unit.lens and rec["unit_id"] == unit.unit_id
        assert rec["prompt_sha256"], unit.unit_id
        if unit.kind == "part":
            assert rec["section_ids"], unit.unit_id
            assert rec["slices"] and all(len(s) == 3 for s in rec["slices"])
            anchors.add(rec["anchor_sha256"])
        side = state.read_json(unit.sidecar_path)
        assert side["content_sha256"] and side["unit_id"] == unit.unit_id
    assert len(anchors) == 1, "the anchor packet must be byte-identical in every part"

    # the composed lens file is the parts, and says so
    composed = state.read_json(root / "audit" / "overclaim.driver.json")
    assert composed["written_by"] == audit_stage.COMPOSED_WRITER
    assert composed["synthesis"] is True and composed["parts"] >= 2
    assert [c["unit_id"] for c in composed["composed_from"]] == [
        u.unit_id for u in all_units if u.lens == "overclaim" and u.kind != "whole"]
    assert audit_stage.lens_is_accepted(root, "overclaim") == (True, "")


# --------------------------------------------------------------------------- #
# 3-5. What each reading may see, read off the prompts themselves
# --------------------------------------------------------------------------- #
def _drive_parts(cfg: Config, pid: str, responses: dict[str, dict]) -> Reviewer:
    reviewer = Reviewer(cfg, responses)
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    res = audit_stage.run_audit(cfg, pid)
    if res["awaiting"]:
        audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
        audit_stage.run_audit(cfg, pid)
    return reviewer


def test_a_part_never_sees_another_parts_findings(cfg: Config):
    """Requirement 2, checked against the bytes rather than against a flag.

    Part 1 returns a finding whose statement carries a string that occurs nowhere in the
    paper. If any later part's prompt contains it, a part inherited another part's
    reasoning and the four readings have started echoing each other.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    leak = "TRACERFROMPARTONE"
    responses = _blank(units)
    responses["overclaim/part-01"] = {
        "lens": "overclaim", "schema_version": 2,
        "findings": [_finding("o-1", ABSTRACT_CLAIM, "p1", statement=f"{leak}: overstated")]}
    reviewer = _drive_parts(cfg, pid, responses)

    for unit in units:
        if unit in ("overclaim/part-01", "overclaim/synthesis"):
            continue
        assert leak not in reviewer.prompt_for(unit), f"{unit} inherited part 1's finding"
    # THE ONE PLACE IT MAY REAPPEAR, and it must: this lens's own cross-part synthesis is
    # where a relationship spanning parts is recovered, and a synthesis that could not see
    # what its own parts observed would have nothing to relate. Asserted rather than
    # merely permitted, so a change that quietly stopped feeding the brief fails here.
    assert leak in reviewer.prompt_for("overclaim/synthesis")


def test_no_lens_ever_sees_another_lenss_output(cfg: Config):
    """Requirement 3. The independence that must not be traded for whole-paper reasoning."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    tracers = {lens: f"TRACER{lens.upper()}" for lens in audit_stage.LENSES}
    responses = _blank(units)
    for lens, tracer in tracers.items():
        responses[f"{lens}/part-01"] = {
            "lens": lens, "schema_version": 2,
            "findings": [_finding(f"{lens}-1", ABSTRACT_CLAIM, "p1",
                                  statement=f"{tracer}: a concern")]}
    reviewer = _drive_parts(cfg, pid, responses)

    for unit in units:
        owner = unit.split("/")[0]
        body = reviewer.prompt_for(unit)
        for lens, tracer in tracers.items():
            if lens == owner:
                continue
            assert tracer not in body, f"{lens}'s output reached {unit}"


def test_a_synthesis_sees_only_its_own_lenss_grounded_observations(cfg: Config):
    """Requirement 3, from the other side: what IS in the brief.

    Both of this lens's parts are represented, and an observation whose quotation is not
    in the paper is not — the same verification gate every other candidate passes, run one
    stage early so a synthesis never reasons over a quotation that does not exist.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["contradiction/part-01"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [_finding("c-1", ABSTRACT_CLAIM, "p1", statement="REALONE"),
                     _finding("c-x", "this sentence is not in the paper at all", "p1",
                              statement="INVENTED")]}
    responses["contradiction/part-02"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [_finding("c-2", CONCLUSION_CONCESSION, "p9", statement="REALTWO")]}
    reviewer = _drive_parts(cfg, pid, responses)

    brief = reviewer.prompt_for("contradiction/synthesis")
    assert "REALONE" in brief and "REALTWO" in brief
    assert "INVENTED" not in brief, (
        "an unverifiable observation must not reach the synthesis")
    manifest = state.read_json(
        cfg.projects_dir / pid / "audit" / "reading" / "contradiction"
        / "synthesis.manifest.json")
    assert manifest["synthesis_inputs"]["candidate_finding_ids"] == ["c-1", "c-2"]
    assert manifest["synthesis_inputs"]["parts_read"] >= 2


# --------------------------------------------------------------------------- #
# 6-7. The relationships a part-local reader structurally could not see
# --------------------------------------------------------------------------- #
def test_an_abstract_conclusion_contradiction_is_recoverable_across_parts(cfg: Config):
    """Requirement 4: both sides survive as independently resolvable locations."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["contradiction/synthesis"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [_finding(
            "c-cross", ABSTRACT_CLAIM, "p1", severity="MAJOR",
            statement="the abstract asserts across every benchmark; the conclusion concedes two of five",
            extra=[{"role": "conclusion concession",
                    "evidence_quote": CONCLUSION_CONCESSION, "evidence_ref": "p9"}])]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    assert invalid == [], invalid
    cross = [f for r in reports for f in r.findings if f.finding_id == "c-cross"]
    assert len(cross) == 1, "the cross-section concern survived verification"
    found = cross[0]
    assert found.cross_section is True
    assert found.source_part == "synthesis"
    assert [e.evidence_ref for e in found.additional_evidence] == ["p9"]
    # BOTH sides were checked by the harness, and both say so in the harness's own words
    assert "occurs verbatim" in found.verified_observation
    assert CONCLUSION_CONCESSION[:20] in found.additional_evidence[0].evidence_quote
    assert found.additional_evidence[0].evidence_class == "prose_verified"


def test_a_table_prose_contradiction_is_recoverable_across_parts(cfg: Config):
    """The second shape, where the two sides are of different kinds.

    The cell is the strong citation and the prose is the other half. The concern is held
    to the WEAKER of the two, which here is the same, and the point of the assertion is
    that both resolved as their own kind rather than one being described by the other.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["contradiction/synthesis"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [_finding(
            "c-tab", PROSE_ABOUT_TABLE, "p5", severity="MAJOR",
            statement="the prose states 91.4 and the table reports 85.4",
            extra=[{"role": "the cell the prose summarises",
                    "evidence_quote": "85.4", "evidence_ref": "T0:r1:c1"}])]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    assert invalid == []
    found = next(f for r in reports for f in r.findings if f.finding_id == "c-tab")
    assert found.cross_section is True
    assert found.evidence_class == "prose_verified"          # the weaker of the two sides
    assert found.additional_evidence[0].evidence_class == "cell_verified"
    assert "85.4" in found.additional_evidence[0].verified_observation


# --------------------------------------------------------------------------- #
# 8-10. What the synthesis may and may not do
# --------------------------------------------------------------------------- #
def test_one_concern_raised_in_two_parts_merges_on_its_resolved_address(cfg: Config):
    """Requirement 6: deduplication is over addresses, never over how prose reads."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    for part in ("part-01", "part-02"):
        responses[f"protocol/{part}"] = {
            "lens": "protocol", "schema_version": 2,
            "findings": [_finding(f"p-{part}", ABSTRACT_CLAIM, "p1",
                                  statement=f"phrased differently in {part}")]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, _invalid = audit_stage.load_reports(cfg, pid, doc)
    protocol = next(r for r in reports if r.lens == "protocol")
    assert len(protocol.findings) == 1, [f.finding_id for f in protocol.findings]
    assert protocol.merged_duplicates == 1
    assert protocol.findings[0].merged_from == ["p-part-02"]
    assert protocol.findings[0].source_part == "part-01", "the survivor keeps its origin"


def test_two_concerns_that_merely_read_alike_are_not_merged(cfg: Config):
    """The other half of requirement 6, and the reason the key is not text similarity."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["protocol/part-01"] = {
        "lens": "protocol", "schema_version": 2,
        "findings": [_finding("p-a", ABSTRACT_CLAIM, "p1", statement="identical words"),
                     _finding("p-b", METHOD_LINE, "p2", statement="identical words")]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, _invalid = audit_stage.load_reports(cfg, pid, doc)
    protocol = next(r for r in reports if r.lens == "protocol")
    assert {f.finding_id for f in protocol.findings} == {"p-a", "p-b"}
    assert protocol.merged_duplicates == 0


def test_a_synthesis_may_withdraw_a_concern_another_part_explains(cfg: Config):
    """Requirement 3's fourth permission, and the only pass that can exercise it.

    Part 1 raises a concern; the synthesis, holding both parts' observations, does not
    carry it forward and says why. Withdrawing is a result: what must NOT happen is the
    withdrawal silently resurrecting the concern or losing the record that it was raised.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["confound/part-01"] = {
        "lens": "confound", "schema_version": 2,
        "findings": [_finding("cf-1", METHOD_LINE, "p2",
                              statement="the schedule is never specified")],
        "notes": "raised from the method span alone"}
    responses["confound/synthesis"] = {
        "lens": "confound", "schema_version": 2, "findings": [],
        "notes": "withdrawing cf-1: the cosine schedule is stated in the same sentence"}
    _drive_parts(cfg, pid, responses)

    root = cfg.projects_dir / pid
    composed = state.read_json(root / "audit" / "confound.json")
    assert [f["finding_id"] for f in composed["findings"]] == ["cf-1"]
    assert "withdrawing cf-1" in composed["notes"]
    assert "[part-01]" in composed["notes"], (
        "the part that raised it is still named, so a withdrawal is traceable")


def test_a_synthesis_cannot_introduce_an_evidence_pointer_that_does_not_resolve(cfg: Config):
    """Requirement 5. "The synthesis said these conflict" is not evidence.

    One side of the concern is real and the other is not in the paper. The whole concern
    is dropped and counted — never kept with the half that happened to check out, which
    would publish the invented half on the model's word.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["contradiction/synthesis"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [
            _finding("c-good", ABSTRACT_CLAIM, "p1", statement="real"),
            _finding("c-bad", ABSTRACT_CLAIM, "p1", statement="half invented",
                     extra=[{"role": "the other side",
                             "evidence_quote": "the conclusion admits total failure",
                             "evidence_ref": "p9"}])]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    assert invalid == []
    ids = {f.finding_id for r in reports for f in r.findings}
    assert "c-good" in ids and "c-bad" not in ids
    assert dropped >= 1, "the unresolvable concern is counted, not silently discarded"


def test_a_further_location_with_no_reference_is_refused_before_it_is_sealed(cfg: Config):
    """One layer earlier: the driver refuses the reading rather than persisting it.

    A quote with no location cannot be verified, and that was already true of a finding's
    primary citation. The second half of a cross-section concern is not a lesser citation,
    and a rule enforced only at the top level is a rule with a hole in it.
    """
    with pytest.raises(audit_driver.AuditDriverError, match="as locatable as the first"):
        audit_driver.parse_lens_json(json.dumps({
            "lens": "contradiction", "findings": [
                _finding("c-1", ABSTRACT_CLAIM, "p1",
                         extra=[{"role": "other side",
                                 "evidence_quote": CONCLUSION_CONCESSION}])]}),
            "contradiction")


def test_a_synthesis_cannot_certify_its_own_second_citation(cfg: Config):
    """Invariant 2, one level down. The machine half of every side is the harness's."""
    report = audit_driver.parse_lens_json(json.dumps({
        "lens": "contradiction", "findings": [
            _finding("c-1", ABSTRACT_CLAIM, "p1",
                     extra=[{"role": "other side", "evidence_quote": CONCLUSION_CONCESSION,
                             "evidence_ref": "p9",
                             "evidence_class": "cell_verified",
                             "verified_observation": "THE HARNESS CONFIRMED THIS"}])]}),
        "contradiction")
    side = report.findings[0].additional_evidence[0]
    assert side.evidence_class == "" and side.verified_observation == ""


# --------------------------------------------------------------------------- #
# 11. A synthesized concern is graded like any other
# --------------------------------------------------------------------------- #
def test_a_serious_synthesized_concern_still_reaches_blinded_grading(cfg: Config):
    """Requirement 7: no privileged severity path for anything a synthesis proposed.

    The candidate list the grader is given is built from the composed lens file, so a
    synthesis concern is in it on exactly the same terms as a part reader's — and the
    grader is shown the finding, never which pass produced it.
    """
    from harness.stages import grade as grade_stage

    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["overclaim/part-01"] = {
        "lens": "overclaim", "schema_version": 2,
        "findings": [_finding("o-part", METHOD_LINE, "p2", severity="MAJOR",
                              statement="a part reader's serious concern")]}
    responses["overclaim/synthesis"] = {
        "lens": "overclaim", "schema_version": 2,
        "findings": [_finding("o-syn", ABSTRACT_CLAIM, "p1", severity="MAJOR",
                              statement="the headline gain is not supported")]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    assert invalid == []
    candidates = grade_stage.select_candidates(reports, scope="serious")
    assert {"o-part", "o-syn"} <= {f.finding_id for f in candidates}, (
        [f.finding_id for f in candidates])

    prompts = grade_stage.build_prompts(cfg, pid, doc, candidates)
    body = Path(prompts[grade_stage.slug_for("o-syn")]).read_text("utf-8")
    assert "the headline gain is not supported" in body
    # WHICH PASS PRODUCED IT is withheld. A part reader and a cross-part synthesis are the
    # same lens under the same rules, and a grader told which had spoken could weigh the
    # pass instead of the argument. Compared against the OTHER candidate's prompt, so this
    # checks that the two are built the same way rather than that one string is absent.
    other = Path(prompts[grade_stage.slug_for("o-part")]).read_text("utf-8")
    for leak in ("source_part", "part-01", "part-02", "synthesis"):
        assert leak not in body, leak
        assert leak not in other, leak


def test_a_grader_weighing_a_cross_section_concern_is_shown_both_sides(cfg: Config):
    """Half of a cross-section concern is not a concern anyone can grade.

    Shown only the abstract quote, a grader asked whether "the abstract asserts what the
    conclusion concedes" holds would have to take the conclusion half from the first
    reader — which is the deference independent grading exists to remove.
    """
    from harness.stages import grade as grade_stage

    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["contradiction/synthesis"] = {
        "lens": "contradiction", "schema_version": 2,
        "findings": [_finding(
            "c-cross", ABSTRACT_CLAIM, "p1", severity="MAJOR",
            statement="the abstract overstates what the conclusion concedes",
            extra=[{"role": "conclusion concession",
                    "evidence_quote": CONCLUSION_CONCESSION, "evidence_ref": "p9"}])]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, _invalid = audit_stage.load_reports(cfg, pid, doc)
    candidates = grade_stage.select_candidates(reports, scope="serious")
    prompts = grade_stage.build_prompts(cfg, pid, doc, candidates)
    body = Path(prompts[grade_stage.slug_for("c-cross")]).read_text("utf-8")
    assert ABSTRACT_CLAIM in body and CONCLUSION_CONCESSION in body
    assert "DEPENDS ON MORE THAN ONE PLACE" in body
    assert "conclusion concession" in body


# --------------------------------------------------------------------------- #
# 12-13. Resume, and the terminal gate
# --------------------------------------------------------------------------- #
def test_a_resumed_run_does_not_re_dispatch_a_reading_it_already_has(cfg: Config):
    """Requirement 9. A sealed part whose prompt has not changed is not read again."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0

    res = audit_stage.run_audit(cfg, pid)
    first_batch = res["awaiting"]
    audit_driver.fill(cfg, pid, first_batch, res["prompts"], units=res["units"])
    calls_after_parts = len(reviewer.calls)
    assert calls_after_parts == len(first_batch)

    # a second invocation of the same phase: the parts are sealed, so only the syntheses
    # that are now dispatchable are read
    res2 = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res2["awaiting"], res2["prompts"], units=res2["units"])
    dispatched = [c["unit"] for c in reviewer.calls[calls_after_parts:]]
    assert sorted(dispatched) == sorted(f"{ln}/synthesis" for ln in audit_stage.LENSES)
    assert not (set(dispatched) & set(first_batch)), "a completed part was re-read"

    # and a third invocation dispatches nothing at all
    res3 = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res3["awaiting"], res3["prompts"], units=res3["units"])
    assert len(reviewer.calls) == len(units)


def test_a_part_whose_prompt_changed_is_no_longer_accepted(cfg: Config):
    """The other half of resume: cheap is not the same as correct.

    A sealed part answers the span it was given. If the span moves — a different budget, a
    re-parse, a section that grew — the seal certifies a correspondence that no longer
    holds, and re-reading is the only honest option.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])

    root = cfg.projects_dir / pid
    doc = PaperDoc(**state.read_json(root / "paper" / "doc.json"))
    unit = next(u for u in audit_stage.units_for(root, audit_stage.LENSES,
                                                 audit_stage.plan_for(doc))
                if u.unit_id == "overclaim/part-01")
    assert audit_stage.unit_is_accepted(unit)[0] is True
    unit.prompt_path.write_text("a different prompt entirely", encoding="utf-8")
    ok, why = audit_stage.unit_is_accepted(unit)
    assert ok is False and "prompt changed" in why


def test_the_terminal_transition_refuses_a_review_missing_a_required_synthesis(cfg: Config):
    """Requirement 9's last clause, asked at the last gate rather than only at the first.

    The parts alone produce findings, grade, and render. Nothing downstream notices that a
    pass this review says it performs never ran, which is exactly why the question is
    asked again here.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    reviewer = Reviewer(cfg, responses)
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    audit_stage.run_audit(cfg, pid)

    missing = audit_stage.missing_reading_artifacts(cfg, pid)
    assert sorted(missing) == sorted(
        [f"{ln}/synthesis" for ln in audit_stage.LENSES]
        + [f"{ln}.json" for ln in audit_stage.LENSES]), missing

    # Driven to the terminal transition with the report phase forced to succeed. The gate
    # under test is the transition itself, and on this fixture the earlier phases refuse
    # first for their own reasons — `collect` has no composed lens file to read. That
    # refusal is correct and it is not this gate: the case this exists for is a review
    # that got all the way to the end, which is reachable on a case resumed or driven from
    # a cached state that never re-entered `audit`.
    import harness.controller as controller_mod
    from harness.controller import PhaseOutcome, step

    case = _case(cfg, pid)
    case.phase = "report"
    original = controller_mod._HANDLERS["report"]
    controller_mod._HANDLERS["report"] = lambda cfg_, case_, **_: PhaseOutcome("ok", "forced")
    try:
        out = step(cfg, case, auto_audit=False)
    finally:
        controller_mod._HANDLERS["report"] = original
    assert out.status != "complete"
    assert "reading artifact" in (out.blocked_reason or ""), out.blocked_reason
    assert out.phase == "audit", "the case is sent back to the phase that can fix it"
    assert out.history[-1].phase == "done" and out.history[-1].outcome == "waiting"


def test_a_complete_reading_set_lets_the_terminal_transition_through(cfg: Config):
    """The same gate, from the side that must not be a wall."""
    pid = _plant(cfg, _one_part_doc())
    units = _units(cfg, pid)
    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    assert audit_stage.missing_reading_artifacts(cfg, pid) == []


# --------------------------------------------------------------------------- #
# The accounting
# --------------------------------------------------------------------------- #
def test_the_reading_record_reports_seven_numbers_separately(cfg: Config):
    """Requirement 8. Collapsed, they are the sentence that says four lenses read a paper
    they were shown a third of."""
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    _drive_parts(cfg, pid, _blank(units))
    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    rec = audit_stage.reading_record(cfg, pid, doc)
    for field in ("extracted_text_fraction", "reader_visible_fraction",
                  "anchor_repeat_fraction", "number_of_parts", "lenses_completed",
                  "lens_syntheses_required", "lens_syntheses_completed"):
        assert field in rec, field
    assert rec["reader_visible_fraction"] == 1.0
    assert rec["number_of_parts"] >= 2
    assert rec["lenses_completed"] == len(audit_stage.LENSES)
    assert rec["lens_syntheses_required"] == len(audit_stage.LENSES)
    assert rec["lens_syntheses_completed"] == len(audit_stage.LENSES)
    assert 0.0 < rec["anchor_repeat_fraction"] < 0.5
    assert rec["extracted_text_fraction"] is not None


def test_a_one_part_paper_requires_no_synthesis_and_pays_no_anchor(cfg: Config):
    pid = _plant(cfg, _one_part_doc())
    units = _units(cfg, pid)
    reviewer = Reviewer(cfg, _blank(units))
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, reviewer.template, 0
    res = audit_stage.run_audit(cfg, pid)
    audit_driver.fill(cfg, pid, res["awaiting"], res["prompts"], units=res["units"])
    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    rec = audit_stage.reading_record(cfg, pid, doc)
    assert rec["number_of_parts"] == 1
    assert rec["lens_syntheses_required"] == 0
    assert rec["anchor_repeat_chars"] == 0 and rec["anchor_chars"] == 0
    assert rec["reader_visible_fraction"] == 1.0


def test_two_concerns_at_one_address_that_classify_differently_stay_apart(cfg: Config):
    """The over-merge this key was widened to prevent, taken from the real corpus.

    `apt-icml` carries two `protocol` concerns anchored on the same quoted row of page 20.
    One says Table 2 prints 100.0% for merged LoRA's inference time where Table 11 shows
    the same computation; the other says the abstract's speedup is normalised against
    LoRA+Prune rather than against fine-tuning. Both summarise to the scientific class
    CONTRADICTION, and they are different concerns — GENUINE_CONTRADICTION against
    DIFFERENT_DENOMINATOR, CONFIRMED_FINDING against PLAUSIBLE_CONCERN.

    Keyed on the summary, the first version of `deduplicate` folded one into the other and
    the review lost it. Over-merging deletes a real concern and leaves a merged id as the
    only trace; under-merging reports one concern twice, which a reader can see and the
    synthesis pass is asked to fold. The key errs toward the second.
    """
    pid = _plant(cfg, _two_part_doc())
    units = _units(cfg, pid)
    responses = _blank(units)
    responses["protocol/part-01"] = {
        "lens": "protocol", "schema_version": 2,
        "findings": [
            {"finding_id": "p-contradiction", "severity": "MINOR", "title": "t",
             "statement": "the table and the prose disagree",
             "evidence_quote": ABSTRACT_CLAIM, "evidence_ref": "p1",
             "discrepancy_type": "GENUINE_CONTRADICTION",
             "candidate_class": "CONFIRMED_FINDING"},
            {"finding_id": "p-denominator", "severity": "NOTE", "title": "t",
             "statement": "the percentage is normalised against a different baseline",
             "evidence_quote": ABSTRACT_CLAIM, "evidence_ref": "p1",
             "discrepancy_type": "DIFFERENT_DENOMINATOR",
             "candidate_class": "PLAUSIBLE_CONCERN"}]}
    _drive_parts(cfg, pid, responses)

    doc = PaperDoc(**state.read_json(cfg.projects_dir / pid / "paper" / "doc.json"))
    reports, _dropped, _invalid = audit_stage.load_reports(cfg, pid, doc)
    protocol = next(r for r in reports if r.lens == "protocol")
    ids = {f.finding_id for f in protocol.findings}
    assert ids == {"p-contradiction", "p-denominator"}, ids
    assert protocol.merged_duplicates == 0
    # they DO share an address and a scientific class — which is what makes the address
    # plus the summary an insufficient key, and is why the test is worth having
    assert len({f.scientific_class for f in protocol.findings}) == 1
    assert len({audit_stage._address_identity(f) for f in protocol.findings}) == 1
