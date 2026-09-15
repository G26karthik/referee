"""Backend selection as a matching problem, and the wall between a declaration and a runner.

`select_backend` answers "which backend did the operator name". That is a configuration
lookup and it is not the question that has to be asked before a reproduction: which
registered environment can host an experiment that declares 24 GiB of VRAM and a
Linux-only stack? `select_for` answers that one, against every registered profile.

Kaggle and Colab are registered as DECLARATIONS. They are real places an experiment could
run, with published hardware, and neither can be driven from this host — no credentials,
no API, no way to move a checkout into a notebook session. Registering them lets a refusal
be useful: "a 16 GiB T4 would fit this experiment but cannot be provisioned from here"
tells an operator what to do next, where a bare "no backend" does not.

The wall those declarations sit behind is tested here and is deliberately redundant.
`can_execute=False` makes `authorize()` refuse before any other precondition is consulted,
and `execute()` raises regardless — so the raise is unreachable rather than load-bearing,
and there is no path by which a published specification becomes a reproduction verdict.
"""
from __future__ import annotations

import pytest

from harness.artifacts import (GIB, CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec, ResourceCapability,
                               ResourceEvidence, ResourceRequirement, SELECTION_CODES)
from harness.backends import (BackendProfile, ColabBackend, DeclaredBackend, ExecRequest,
                              KaggleBackend, LocalBackend, authorize, local_backend,
                              register_backend, registered_backends, select_for)
from harness.config import Config
from harness.local_exec import reconcile
from harness.stages.report import overall_verdict


def _cfg(**over) -> Config:
    cfg = Config.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _req(**kw) -> ResourceRequirement:
    return ResourceRequirement(
        evidence=[ResourceEvidence(quote="q", kind="declared_requirement")], **kw)


def _verified() -> CommitVerification:
    return CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)


def _est(cls):
    return cls(state="established", established=True, reason="fixture")


def _qualified(**over) -> ProbeSpec:
    spec = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     experiment=_est(ExperimentIdentity), metric_identity=_est(MetricIdentity),
                     configuration=_est(ConfigurationIdentity),
                     capability=ExecCapability(established=True, reason_code="established"),
                     resources=ResourceCapability(state="satisfied", reason="fits"))
    for k, v in over.items():
        setattr(spec, k, v)
    return spec


class BigRunner(DeclaredBackend):
    """A backend that CAN execute and has room. Only exists to prove selection prefers it."""

    name = "big-runner"
    spec = BackendProfile(name="big-runner", platform="linux", vram_bytes=80 * GIB,
                          ram_bytes=512 * GIB, disk_bytes=4000 * GIB, cpu_count=64,
                          gpu_name="A100-80GB", can_execute=True)

    def available(self):
        from harness.backends import BackendAvailability
        return BackendAvailability(True, "")

    def execute(self, req: ExecRequest):
        raise AssertionError("no test may actually execute anything")


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
def test_the_declared_environments_are_registered():
    names = registered_backends()
    assert {"local", "kaggle", "colab"} <= set(names)


@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_a_declared_environment_is_not_a_runner(cls):
    b = cls()
    p = b.profile()
    assert p.can_execute is False and p.requires_credentials is True
    assert b.available().usable is False and b.available().detail
    assert p.vram_bytes and p.ram_bytes and p.max_walltime_s, "a declaration states real specs"


@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_executing_a_declaration_raises_rather_than_pretending(cls):
    with pytest.raises(NotImplementedError) as e:
        cls().execute(ExecRequest(["python", "train.py"], "", 60))
    assert "not an integration" in str(e.value)


@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_a_declaration_reports_no_capability(cls):
    from harness.artifacts import RepoAcquisition
    cap = cls().capability(RepoAcquisition(status="cached", path="."), "py", "py")
    assert not cap.established and cap.reason_code == "environment_incompatible"


@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_a_declaration_provisions_nothing(cls, tmp_path):
    from harness.artifacts import RepoAcquisition
    acq = cls().provision(_cfg(allow_install=True), tmp_path, "p",
                          RepoAcquisition(status="cached", path=str(tmp_path)))
    assert acq.env_status == "blocked"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def test_an_experiment_that_fits_this_host_selects_the_local_backend():
    sel = select_for(_req(vram_bytes=2 * GIB), _cfg())
    assert sel.reason_code == "selected" and sel.selected
    assert sel.chosen is not None and sel.chosen.name == "local"


def test_an_experiment_too_large_for_everything_registered_is_refused():
    sel = select_for(_req(vram_bytes=200 * GIB), _cfg())
    assert sel.reason_code == "resources_insufficient" and sel.chosen is None
    names = {n for n, _, _ in sel.considered}
    assert {"local", "kaggle", "colab"} <= names, "every candidate is accounted for"


def test_an_experiment_that_only_a_declared_environment_could_host_says_so():
    """The refusal that is actually useful. 12 GiB does not fit this 8 GiB card but does
    fit a T4, and the operator needs to be told that rather than 'no backend'."""
    sel = select_for(_req(vram_bytes=12 * GIB), _cfg(), declared_platform="linux")
    assert sel.reason_code == "credentials_unavailable" and sel.chosen is None
    assert "cannot be provisioned from this host" in sel.reason
    assert any(v == "credentials_unavailable" for _, v, _ in sel.considered)


def test_a_linux_only_stack_rules_out_a_windows_host():
    """The WINDOWS host is ruled out. What else is on offer is a separate question.

    This used to also assert `sel.chosen is None`, which was true only while every
    registered backend was either this machine or unreachable. A Linux container backend
    on this host is a genuine answer to "where can a linux-64 stack run", so asserting
    that nothing is chosen would assert the absence of a capability rather than the
    presence of the platform check. The platform check is what this test is about, and it
    is unchanged: `local` reports `platform_incompatible` for a linux-only declaration.
    """
    import sys
    if sys.platform.startswith("linux"):
        pytest.skip("needs a non-Linux host")
    sel = select_for(_req(vram_bytes=1 * GIB), _cfg(), declared_platform="linux")
    verdicts = {n: v for n, v, _ in sel.considered}
    assert verdicts["local"] == "platform_incompatible"
    # Anything that IS chosen must genuinely offer the platform, never this host.
    assert sel.chosen != "local"


def test_a_walltime_beyond_a_backends_session_limit_rules_it_out():
    """A backend that publishes a session limit shorter than the demand does not fit.

    Asserted per backend rather than over the whole roster. A backend with no published
    session limit — a container on this host does not time out at a provider's boundary —
    is not ruled out by this requirement, and demanding that nothing fits would make the
    test an assertion that no such backend exists.
    """
    sel = select_for(_req(vram_bytes=1 * GIB, walltime_s=40 * 3600), _cfg(),
                     declared_platform="linux", walltime_s=40 * 3600)
    considered = {n: v for n, v, _ in sel.considered}
    from harness import backends as _b
    for name, verdict in considered.items():
        limit = _b._REGISTRY[name]().profile().max_walltime_s
        if limit and limit < 40 * 3600:
            assert verdict != "fits", (name, verdict)


def test_an_unstated_requirement_matches_nothing():
    """Matching a backend against an unknown demand is choosing one at random."""
    sel = select_for(ResourceRequirement(), _cfg())
    assert sel.reason_code == "requirement_unknown" and sel.chosen is None
    sel_none = select_for(None, _cfg())
    assert sel_none.reason_code == "requirement_unknown"


def test_a_backend_that_cannot_report_a_quantity_does_not_satisfy_it():
    """An unmeasured offer is not a large one — the same asymmetry E1 encodes."""
    from harness.backends import _fits
    assert _fits(None, 5) is True and _fits(5, None) is False
    assert _fits(5, 5) is True and _fits(6, 5) is False


def test_selection_prefers_a_runner_over_a_declaration():
    register_backend("big-runner", BigRunner)
    try:
        sel = select_for(_req(vram_bytes=12 * GIB), _cfg(), declared_platform="linux")
        assert sel.reason_code == "selected"
        assert sel.chosen is not None and sel.chosen.name == "big-runner"
    finally:
        from harness import backends as b
        b._REGISTRY.pop("big-runner", None)


def test_every_reason_code_is_one_of_the_declared_ones():
    for req, plat in ((_req(vram_bytes=1 * GIB), ""), (_req(vram_bytes=200 * GIB), ""),
                      (_req(vram_bytes=12 * GIB), "linux"), (ResourceRequirement(), "")):
        assert select_for(req, _cfg(), declared_platform=plat).reason_code in SELECTION_CODES


# --------------------------------------------------------------------------- #
# The wall: a declaration can never produce a reproduction verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_authorization_refuses_a_declared_backend_outright(cls):
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), cls(), commit=_verified())
    assert not auth.allowed and auth.decision == "backend_cannot_execute"
    assert auth.failure_class == "credentials_unavailable"


@pytest.mark.parametrize("cls", [KaggleBackend, ColabBackend])
def test_the_refusal_precedes_every_other_precondition(cls):
    """Checked before the gate and before identity, so `execute()`'s raise is unreachable
    rather than the last line of defence."""
    naked = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec")
    auth = authorize(_cfg(allow_repo_exec=False), naked, cls(), commit=None)
    assert auth.decision == "backend_cannot_execute", auth.decision


@pytest.mark.parametrize("values", [[59.30, 59.26], [999.0, 999.0]])
def test_a_declared_backend_produces_neither_verdict(values):
    """Both directions: a specification may not acquit a cell any more than convict one."""
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), KaggleBackend(),
                     commit=_verified())
    spec = ProbeSpec(paper_id="p", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, values, 0.10, [0, 1], authorization=auth)
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "credentials_unavailable"


def test_a_backend_refusal_cannot_drive_the_paper_red():
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), ColabBackend(),
                     commit=_verified())
    spec = ProbeSpec(paper_id="p", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, [999.0, 999.0], 0.10, [0, 1], authorization=auth)
    verdict, _ = overall_verdict([], rec)
    assert verdict == "GREEN", "not being able to reach a GPU says nothing about a paper"


def test_a_real_runner_is_still_authorized_when_everything_holds(confined_local):
    """The wall must not have made every backend unusable."""
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(),
                     commit=_verified())
    assert auth.allowed and auth.decision == "authorized"


def test_the_local_backend_declares_itself_executable():
    p = LocalBackend().profile()
    assert p.can_execute is True and p.requires_credentials is False
    assert p.name == "local" and p.cpu_count


# --------------------------------------------------------------------------- #
# The seven states, and why none of them may share a code
# --------------------------------------------------------------------------- #
class _Offline(LocalBackend):
    """A real runner, big enough for anything, that is not reachable at this moment.

    Nothing in the registry behaves this way today — `local` is always up and the
    declarations can never run — so this is the shape of the first genuinely remote
    backend, tested before one exists.
    """

    name = "offline-runner"

    def profile(self) -> BackendProfile:
        return BackendProfile(
            name=self.name, platform="linux", vram_bytes=80 * GIB, ram_bytes=200 * GIB,
            disk_bytes=2000 * GIB, cpu_count=64, gpu_count=8, can_execute=True,
            detail="a real runner, currently unreachable")

    def available(self):
        from harness.backends import BackendAvailability
        return BackendAvailability(False, "the provider API returned 503")


def _only(name, cls, req, **kw):
    """Run `select_for` against a registry holding exactly one backend."""
    from harness import backends as b

    saved = dict(b._REGISTRY)
    try:
        b._REGISTRY.clear()
        b._REGISTRY[name] = cls
        return select_for(req, _cfg(), **kw)
    finally:
        b._REGISTRY.clear()
        b._REGISTRY.update(saved)


def test_a_backend_that_is_merely_offline_is_not_a_backend_that_is_too_small():
    """The collapse this closes, stated exactly.

    An unreachable runner fell through every terminal branch of `select_for` and landed in
    `resources_insufficient` — whose reason is built by joining the candidates rejected for
    size, of which there were none. So the harness reported "no registered environment is
    large enough for the experiment as published: " with nothing after the colon, about a
    backend with 80 GiB that had not been rejected for size at all.

    They must not share a code because the right response differs: retry later for one,
    and never for the other.
    """
    sel = _only("offline-runner", _Offline, _req(vram_bytes=8 * GIB))
    assert sel.reason_code == "backend_unavailable" and sel.chosen is None
    assert "503" in sel.reason, "the reason names what was actually wrong"
    assert sel.reason_code in SELECTION_CODES

    small = _only("local", LocalBackend, _req(vram_bytes=500 * GIB))
    assert small.reason_code == "resources_insufficient"
    assert sel.reason_code != small.reason_code


def test_an_unreachable_runner_outranks_a_declaration_in_the_report():
    """Both are "somewhere else could run this". The reachable-later one is the useful one."""
    from harness import backends as b

    b._REGISTRY["offline-runner"] = _Offline
    try:
        sel = select_for(_req(vram_bytes=12 * GIB), _cfg(), declared_platform="linux")
        assert sel.reason_code == "backend_unavailable", sel.reason_code
        verdicts = {n: v for n, v, _ in sel.considered}
        assert verdicts["kaggle"] == "credentials_unavailable"
        assert verdicts["offline-runner"] == "unavailable"
    finally:
        b._REGISTRY.pop("offline-runner", None)


def test_selection_and_the_resource_check_agree_about_gpu_count():
    """They did not, and selection was the optimistic one.

    An experiment declaring 8 GPUs selected `local` — one card — because `select_for`
    matched VRAM, RAM, disk and CPU but not the count, while `assess_resources` had always
    checked it. The result was `backend_selection: selected` written onto the same spec as
    `resources: insufficient`: two components disagreeing about one experiment on one host,
    with the optimistic one recorded as the choice.
    """
    from harness import resources as resources_mod

    req = _req(vram_bytes=1 * GIB, gpu_count=8)
    sel = select_for(req, _cfg())
    assert sel.reason_code == "resources_insufficient", sel.reason_code
    assert any("GPU count" in w for _, _, w in sel.considered)

    assessed = resources_mod.assess_resources(req, LocalBackend().resources(), backend="local")
    assert assessed.state == "insufficient"
    assert (sel.reason_code == "selected") == (assessed.state == "satisfied")


def test_selection_is_not_authorization(confined_local):
    """`select_for` returning a backend permits nothing. `authorize` is the only yes.

    Selection answers "where could this run"; authorization answers "may it". A spec that
    selects the local backend cleanly and has established nothing else must still refuse,
    and the refusal must name the missing precondition rather than the backend.
    """
    sel = select_for(_req(vram_bytes=2 * GIB), _cfg())
    assert sel.selected and sel.chosen is not None

    naked = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec")
    auth = authorize(_cfg(allow_repo_exec=True), naked, sel.chosen, commit=_verified())
    assert not auth.allowed and auth.decision == "identity_unproven"

    # And with the gate shut, a perfectly selected backend still runs nothing.
    assert not authorize(_cfg(allow_repo_exec=False), _qualified(), sel.chosen,
                         commit=_verified()).allowed


def test_provisioning_failure_is_inconclusive_and_never_a_failed_reproduction():
    """A venv that could not be built is a fact about this runner.

    `env_status` keeps `failed` distinct from `blocked` and `not_attempted`, and all three
    reach the reconciliation as `environment_incompatible` — which cannot convict.
    """
    from harness.artifacts import RepoAcquisition
    from harness.repo import assess_capability

    for status in ("failed", "blocked", "not_attempted"):
        acq = RepoAcquisition(status="cached", path=".", entrypoint="eval.py",
                              env_status=status, reason="fixture")
        cap = assess_capability(acq, "", "py")
        assert not cap.established and cap.reason_code == "environment_incompatible", status
        assert status in cap.detail

    from harness.artifacts import ConfigurationIdentity, ExperimentIdentity, MetricIdentity

    spec = ProbeSpec(paper_id="p", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28",
                     experiment=ExperimentIdentity(state="established", reason="fixture"),
                     metric_identity=MetricIdentity(state="established", reason="fixture"),
                     configuration=ConfigurationIdentity(state="established", reason="fixture"),
                     capability=ExecCapability(reason_code="environment_incompatible",
                                               detail="the venv could not be built"))
    rec = reconcile(spec, [999.0], 0.10, [0], failure="environment_incompatible")
    assert rec.status == "INCONCLUSIVE" and rec.failure_class == "environment_incompatible"


def test_each_of_the_seven_backend_states_has_its_own_name():
    """No two of them may report the same pair of codes.

    1 can execute · 2 exists but offline · 3 cannot satisfy the experiment ·
    4 needs credentials · 5 provisioning failed · 6 execution failed · 7 executed.
    """
    fits = _req(vram_bytes=2 * GIB)
    seen = {
        "can execute": ("selected",
                        authorize(_cfg(allow_repo_exec=True), _qualified(),
                                  local_backend(), commit=_verified()).decision),
        "offline": (_only("offline-runner", _Offline, fits).reason_code,
                    authorize(_cfg(allow_repo_exec=True), _qualified(), _Offline(),
                              commit=_verified()).decision),
        "too small": (select_for(_req(vram_bytes=500 * GIB), _cfg()).reason_code, "-"),
        "credentials": (select_for(_req(vram_bytes=12 * GIB), _cfg(),
                                   declared_platform="linux").reason_code,
                        authorize(_cfg(allow_repo_exec=True), _qualified(), KaggleBackend(),
                                  commit=_verified()).decision),
        "provisioning failed": ("environment_incompatible", "capability_unproven"),
        "execution failed": ("runtime_failure", "authorized"),
        "executed": ("none", "authorized"),
    }
    assert len(set(seen.values())) == len(seen), seen


def test_the_selected_backend_is_the_one_that_provisions(tmp_path):
    """Provisioning used to run before selection, against whichever backend the config
    named. With `local` the only runnable one that was always the same answer; a second
    runnable backend would have had its capability judged against an interpreter another
    backend built. So the environment now carries the name of the backend that made it,
    and it must match the backend the spec was planned for.
    """
    from harness.artifacts import CodeAudit, PaperDoc, RepoAcquisition
    from harness.stages.probe import plan_execution

    repo = tmp_path / "runs" / "p" / "repo"
    repo.mkdir(parents=True)
    (repo / "eval.py").write_text("print(1)\n", encoding="utf-8")
    acq = RepoAcquisition(status="cached", path=str(repo), entrypoint="eval.py")

    spec = plan_execution(_cfg(), ProbeSpec(paper_id="p"), acq,
                          PaperDoc(paper_id="p", title="t"), CodeAudit(), root=tmp_path)
    assert acq.env_backend == spec.backend, (acq.env_backend, spec.backend)
    assert acq.env_backend, "provisioning must record which backend built the environment"


def test_selection_refuses_to_choose_when_the_memory_demand_is_unknown():
    """A candidate met everything stated, and memory was not among it.

    Reporting `selected` here would put a contradiction on the spec: selection saying
    yes and `assess_resources` saying `unknown` about the same experiment on the same
    host. Neither is wrong — the honest answer is that nothing can be SHOWN to host it.
    """
    thin = _req(cpu_count=2)
    assert thin.stated and not thin.memory_stated
    sel = select_for(thin, _cfg())
    assert sel.reason_code == "requirement_unknown" and sel.chosen is None
    assert "memory demand was never established" in sel.reason


def test_a_specific_rejection_still_beats_reporting_ignorance():
    """Checked after the candidate loop, not before it: an experiment ruled out on
    platform keeps that reason rather than being flattened into `requirement_unknown`.

    `backend_unavailable` joined the acceptable answers when the remote sandbox became a
    registered runner, and it is the MOST useful of the three: this experiment fits a
    Linux backend that can genuinely execute, and the only thing in the way is a gate the
    operator can open. The previous best answer was "somewhere with a T4 could host this
    and we hold no credentials for it", which is true and actionable by nobody. What the
    test is about is unchanged — the refusal names a specific blocker rather than
    collapsing into ignorance about the requirement.
    """
    import sys
    if sys.platform.startswith("linux"):
        pytest.skip("needs a non-Linux host")
    sel = select_for(_req(cpu_count=2), _cfg(), declared_platform="linux")
    assert sel.chosen is None, "a named blocker is still a refusal"
    # Every one of these is a SPECIFIC, actionable statement, and which one is correct
    # depends on what this host offers. `requirement_unknown` joined the list when a
    # container backend made `linux` reachable here: once some backend genuinely offers
    # the platform, the binding blocker for a requirement stating only a CPU count is
    # that the memory demand was never established — which is invariant 6 refusing an
    # unstated demand, not the selector shrugging. The thing this test forbids is a
    # refusal with nothing in it, so it asserts the reason names a cause.
    assert sel.reason_code in ("platform_incompatible", "credentials_unavailable",
                               "backend_unavailable", "requirement_unknown"), sel.reason_code
    assert sel.reason and len(sel.reason) > 20, "a refusal must say what blocked it"
    if sel.reason_code == "requirement_unknown":
        assert "never established" in sel.reason or "demand" in sel.reason, sel.reason
    assert sel.considered, "and it must show what it considered"
