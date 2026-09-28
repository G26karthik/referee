"""Regressions for "every paper ends complete / BLOCKED_METHOD with no admissible check of
its main claim": a public repository missed for its phrasing, README-documented programs
never harvested, a --seed flag the repository does not define, table labels and settings
treated as results, citation years and indices read as reported numbers, template defaults
("accuracy", "digits", 30 epochs) written into target specs, metric/protocol binding that
only knew one field's vocabulary, a shared budget that let proof fragments crowd out
printed results, and a finished workflow read as a finished scientific check.

Synthetic documents and repositories only — no paper names, numbers or expected verdicts.
Run: `python -m pytest tests/test_general_repairs.py -q`
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harness import decide, discover, execute, experiment_id, locate, paper, repo, routes
from harness.config import Config
from harness.schema import (
    CandidateCommand, ConfigurationIdentity, DiscoveredObject, ExecCapability,
    ExperimentIdentity, MetricIdentity, PaperDoc, PlanDecision, ProbeSpec, RepoAcquisition,
    ResourceCapability, Section, Table, TargetOutcome, TargetSet,
)


def _doc(text: str, tables: list[Table] | None = None, title: str = "") -> PaperDoc:
    return PaperDoc(paper_id="p", title=title or "A Study of Something Quite Specific",
                    sections=[Section(section_idx=0, title="Main", text=text)],
                    tables=tables or [])


# --- 1. a repository advertised in any phrasing is attributed ---------------------------
def test_repository_cue_in_any_authorship_phrasing():
    doc = _doc("Everything needed is available in our public repository: "
               "https://github.com/someone/project and nowhere else.")
    assert repo.official_repo_url(doc) == "https://github.com/someone/project"
    bare = _doc("A related tool is https://github.com/other/tool for comparison.")
    assert repo.official_repo_url(bare) == ""
    for other in ("We make use of the graph library https://github.com/pyg/geo for sparsity.",
                  "The benchmark of Smith et al. is publicly available on "
                  "https://github.com/smith/bench and we use it.",
                  "We provide baseline numbers computed with https://github.com/o/baselines .",
                  "For our experiments we use the data from https://github.com/o/corpus here."):
        assert repo.official_repo_url(_doc(other)) == "", other


def test_public_readme_must_cite_the_full_title_and_an_attribution():
    doc = _doc("Maria Kowalska\nUniversity of Somewhere\nAbstract. We study it. arXiv:2601.01234",
               title="A Study of Something Quite Specific")
    assert repo.readme_attributes("# Tool\nofficial code for A Study of Something Quite "
                                  "Specific (ICML).", doc)
    assert repo.readme_attributes("# A Study of Something Quite Specific\nby Maria Kowalska", doc)
    for foreign in ("# Unofficial PyTorch implementation of A Study of Something Quite "
                    "Specific, official numbers", "We attempt a re-implementation of A Study of "
                    "Something Quite Specific (arXiv 2601.01234), our work",
                    "# A Study of Something Quite Specific\n@inproceedings{booktitle="
                    "{International Conference}} arXiv 2601.01234"):
        assert not repo.readme_attributes(foreign, doc), foreign
    assert not repo.readme_attributes("A Study of Something", doc)          # partial title
    assert not repo.readme_attributes("# A Study of Something Quite Specific", doc)  # no evidence
    listing = "# Awesome papers\n" + "".join(f"- paper 2601.0{i:04d}\n" for i in range(9)) + \
              "- official code for A Study of Something Quite Specific (Person Alpha)\n"
    assert not repo.readme_attributes(listing, doc), "a curated list cites; it is not the code"
    buried = "# Tool\n" + "text " * 400 + "official code for A Study of Something Quite Specific"
    assert not repo.readme_attributes(buried, doc)
    ids = repo.paper_identifiers(_doc("arXiv:2601.01234v3 [cs.LG] preprint"))
    assert ids["arxiv_id"] == "2601.01234" and ids["arxiv_version"] == "v3"


def test_source_search_is_gated_and_never_guesses_from_a_short_title():
    cfg = Config(allow_source_search=False)
    assert repo.discover_public_repo(cfg, _doc("x"))[0] == ""
    cfg = Config(allow_source_search=True, allow_network=True)
    url, why = repo.discover_public_repo(cfg, _doc("x", title="Short"))
    assert url == "" and "too short" in why


# --- 2. the repository's documented interface is harvested ------------------------------
def _repo(tmp_path: Path, readme: str, files: dict[str, str]) -> Path:
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    return tmp_path


def test_readme_prose_and_brace_patterns_name_programs_that_exist(tmp_path):
    r = _repo(tmp_path, "- `/calc_{gt/es}{A/B}_{Alpha/Btea}.py`: evaluation codes.\n"
                        "To train, execute `train_model.py`.\n",
              {"calc_gtA_Alpha.py": "", "calc_esB_Beta.py": "", "sub/train_model.py": "",
               "unrelated.py": ""})
    argvs = {" ".join(c.argv) for c in experiment_id.harvest_candidates(r)}
    assert "python calc_gtA_Alpha.py" in argvs and "python sub/train_model.py" in argvs
    assert "python calc_esB_Beta.py" not in argvs, "a README typo ('Btea') names nothing"
    assert not any("unrelated" in a for a in argvs)


def test_a_bare_glob_names_no_program_and_prose_never_breaks_a_unique_command(tmp_path):
    r = _repo(tmp_path, "All `*.py` files are formatted with black.\nSee `helper.py`.\n"
                        "```bash\npython train.py\n```\n",
              {"train.py": "print('{\"accuracy\": 1}')\nkey = 'accuracy'", "helper.py": "",
               "other.py": ""})
    argvs = [" ".join(c.argv) for c in experiment_id.harvest_candidates(r)]
    assert "python other.py" not in argvs
    t = Table(table_idx=0, label="1", caption="Accuracy on X", header=["method", "accuracy"],
              rows=[["train", "0.9"]])
    e = experiment_id.resolve_experiment(_doc("", [t]), r, "T0:r0:c1")
    assert e.state == "established" and e.command.argv == ["python", "train.py"]


def test_env_runner_continuation_loop_and_placeholder_slots(tmp_path):
    readme = ("```bash\n"
              "uv run python exp.py \\\n    --dataset <name> --mode <fast|slow>\n"
              "for ds in alpha beta gamma; do\n"
              "  uv run python exp.py --dataset \"$ds\"   # one per dataset\n"
              "done\n```\n")
    r = _repo(tmp_path, readme, {"exp.py": ""})
    cands = experiment_id.harvest_candidates(r)
    loop = next(c for c in cands if "$ds" in c.argv)
    assert loop.argv[:2] == ["python", "exp.py"] and "#" not in loop.argv
    assert loop.slots == {"$ds": ["alpha", "beta", "gamma"]}
    filled, _ = experiment_id.instantiate(loop, "beta results, Table 1")
    assert filled is not None and filled.argv[-1] == "beta" and filled.bound_slots == {"$ds": "beta"}
    free = next(c for c in cands if "<name>" in c.argv)
    assert experiment_id.instantiate(free, "beta")[0] is None           # no documented values
    assert experiment_id.instantiate(loop, "alpha and beta")[0] is None  # names two: refuse


def test_slots_bind_whole_tokens_never_seeds_and_holes_are_refused(tmp_path):
    readme = ("```bash\nfor ds in mnist cifar; do python exp.py --dataset \"$ds\"; done\n"
              "for seed in 0 1 2\ndo\n  python exp.py --seed $seed\ndone\n"
              "python exp.py --out $OUT\n```\n")
    r = _repo(tmp_path, readme, {"exp.py": ""})
    cands = experiment_id.harvest_candidates(r)
    ds = next(c for c in cands if "--dataset" in c.argv)
    assert ds.argv[-1] == "$ds" and "done" not in ds.argv        # one-line loop closed
    assert experiment_id.instantiate(ds, "Fashion-MNIST accuracy")[0] is None   # no substring
    assert experiment_id.instantiate(ds, "accuracy on MNIST")[0].argv[-1] == "mnist"
    seed = next(c for c in cands if "--seed" in c.argv)
    assert seed.slots == {"$seed": ["0", "1", "2"]}               # multi-line `do` recognised
    assert experiment_id.instantiate(seed, "RMSE averaged over 1 seed")[0] is None
    hole = next(c for c in cands if "$OUT" in c.argv)
    assert experiment_id.instantiate(hole, "anything")[0] is None


# --- 3. a seed flag is passed only when the documented program defines one ---------------
class _Backend:
    name = "local"

    def __init__(self):
        self.flags = []

    def capability(self, acq, interpreter, harness_python, flag="seed"):
        self.flags.append(flag)
        return ExecCapability(established=True, reason_code="established")

    def commit_tree(self, path):
        return None


def _established(argv, seed_flag=""):
    cmd = CandidateCommand(argv=argv, source="readme", source_ref="README.md:1",
                           seed_flag=seed_flag)
    return (ExperimentIdentity(state="established", command=cmd),
            MetricIdentity(state="established", output_key="rmse"),
            ConfigurationIdentity(state="established"))


def _planned(monkeypatch, tmp_path, argv, seed_flag=""):
    backend = _Backend()
    monkeypatch.setattr(execute, "select_backend", lambda cfg: backend)
    monkeypatch.setattr(routes.repo_mod, "verify_commit",
                        lambda *a, **k: SimpleNamespace(state="verified"))
    (tmp_path / "main.py").write_text("print('x')", encoding="utf-8")
    acq = RepoAcquisition(status="cloned", path=str(tmp_path), entrypoint="other.py",
                          env_path="py", commit="c" * 40)
    e, m, c = _established(argv, seed_flag)
    spec = ProbeSpec(paper_id="p", experiment=e, metric_identity=m, configuration=c,
                     resources=ResourceCapability(state="satisfied"), commit="c" * 40)
    spec = routes.plan_execution(Config(allow_repo_exec=True), spec, acq)
    return spec, backend


def test_no_seed_flag_is_invented_for_a_seedless_program(monkeypatch, tmp_path):
    spec, backend = _planned(monkeypatch, tmp_path, ["python", "main.py"])
    assert backend.flags == [""], "the capability check must not demand --seed"
    assert spec.command == ["python", "main.py"] and len(spec.seeds) == 3


def test_a_defined_seed_flag_is_used_and_a_pinned_one_kept(monkeypatch, tmp_path):
    spec, backend = _planned(monkeypatch, tmp_path, ["python", "main.py"], "--rng_seed")
    assert backend.flags == ["rng_seed"] and spec.command[-2:] == ["--rng_seed", "{seed}"]
    spec, backend = _planned(monkeypatch, tmp_path, ["python", "main.py", "--rng_seed", "7"],
                             "--rng_seed")
    assert backend.flags == [""] and spec.command.count("--rng_seed") == 1


def test_seedless_protocol_is_the_repositorys_own(tmp_path):
    t = Table(table_idx=0, label="1", caption="RMSE on Alpha", header=["method", "RMSE"],
              rows=[["Ours", "0.10"]])
    cmd = CandidateCommand(argv=["python", "main.py"], bound_slots={"$d": "Alpha"})
    cfg_id = experiment_id.resolve_configuration(_doc("", [t]), "T0:r0:c1", cmd, 5)
    assert cfg_id.seed_policy_match is True


def test_deterministic_seedless_run_verifies_only_to_printed_precision():
    e, m, c = _established(["python", "main.py"])
    spec = ProbeSpec(paper_id="p", command=["python", "main.py"], claimed_cell_value="0.12",
                     table_ref="T0:r0:c1", provenance="repo_exec", experiment=e,
                     metric_identity=m, configuration=c)
    unbound = execute.reconcile(spec.model_copy(update={"experiment": None}),
                                [0.12, 0.12, 0.12], 0.0, [0, 1, 2])
    assert unbound.status == "INCONCLUSIVE", "binding is still required first"
    same = execute.reconcile(spec, [0.12, 0.12, 0.12], 0.0, [0, 1, 2])
    assert same.status == "RESOLVED_VERIFIED"
    off = execute.reconcile(spec, [0.19, 0.19, 0.19], 0.0, [0, 1, 2])
    assert off.status == "INCONCLUSIVE", "no noise band: a difference is never a failure"


# --- 4. only measurements become result targets -----------------------------------------
def test_specification_tables_and_index_rows_are_not_results():
    spec_t = Table(table_idx=0, label="3", caption="Table 3. Functions used to generate outcomes",
                   header=[], rows=[["1", "2", "3"], ["f", "0.5", "7"]])
    data_t = Table(table_idx=1, label="4", caption="Table 4. Summary of Datasets",
                   header=["Name", "n"], rows=[["A", "841"]])
    res_t = Table(table_idx=2, label="2", caption="Table 2. RMSE averaged over 9 runs",
                  header=[], rows=[["1", "2", "3"], ["5", "0.024±0.000", "0.041±0.000"]])
    assert paper.table_role(spec_t) == "specification"
    nums = paper.table_numbers([spec_t, data_t, res_t])
    assert not any(n.table_ref.startswith("T0") for n in nums)
    assert all(n.benchmark.startswith("[data statistic]") for n in nums if n.table_ref.startswith("T1"))
    res = [n for n in nums if n.table_ref.startswith("T2")]
    assert res and all(n.table_ref.split(":")[1] != "r0" for n in res)   # index row skipped
    assert all(n.metric.startswith("RMSE") for n in res)


def test_a_boxed_example_read_as_a_grid_is_text_not_results():
    boxed = Table(table_idx=0, label="2", caption="Table 2. A model that is calibrated but not",
                  header=[], rows=[["marginals violate calibration for both inputs", "", "2"],
                                   ["", "1", "2"], ["the predicted ranking distribution", "", ""]])
    assert paper.table_role(boxed) == "text" and not paper.table_numbers([boxed])


def test_spread_citation_years_and_indices_are_not_reported_quantities():
    assert locate.parse_quantity("0.024±0.000").value == 0.024
    assert locate.parse_quantity("as shown by Smith et al. (2021)") is None
    assert locate.parse_quantity("the N−1 remaining") is None
    assert locate.parse_quantity("reaches 76.4% accuracy").value == 76.4
    assert locate.measurement_context("reduces error by 12%")
    assert not locate.measurement_context("we use 3 layers")


def test_no_template_default_is_written_into_a_target_spec():
    spec = ProbeSpec(paper_id="p")
    assert (spec.metric, spec.dataset, spec.epochs) == ("", "", 0)
    body = execute.render_default(spec)
    assert "accuracy" in body and "digits" in body                    # the template's own
    bound = ProbeSpec(paper_id="p", finding_id="f", claim_ref="T0:r0:c1", table_ref="T0:r0:c1")
    doc = _doc("text")
    out = routes.synthesize_probe(Config(allow_synthesis=True), doc, bound,
                                  RepoAcquisition(status="unavailable"))
    assert out.script == "" and out.metric == "" and out.provenance != "synthesized"


# --- 5. metric and protocol bind by the paper's own words -------------------------------
def test_metric_binds_by_exact_name_only():
    t = Table(table_idx=0, label="1", caption="Results", header=["method", "PEHE"],
              rows=[["Ours", "0.31"]])
    doc = _doc("", [t])
    hit = CandidateCommand(argv=["python", "main.py"], named_keys=["PEHE", "AUC"])
    miss = CandidateCommand(argv=["python", "main.py"], named_keys=["RMSE"])
    assert experiment_id.resolve_metric(doc, "T0:r0:c1", hit).established
    assert not experiment_id.resolve_metric(doc, "T0:r0:c1", miss).established


def test_a_different_run_count_is_a_different_protocol(tmp_path):
    t = Table(table_idx=0, label="2", caption="Table 2. RMSE averaged over 100 simulations",
              header=["method", "RMSE"], rows=[["Ours", "0.10"]])
    doc = _doc("", [t])
    cmd = CandidateCommand(argv=["python", "main.py"], bound_slots={"$d": "sim"})
    (tmp_path / "config.yaml").write_text("num_exp: 30\nn_split: 3\n", encoding="utf-8")
    ident = experiment_id.resolve_configuration(doc, "T0:r0:c1", cmd, 1, repo=tmp_path)
    assert ident.state == "unsupported" and "num_exp = 30" in ident.reason
    assert "n_split" not in ident.reason
    (tmp_path / "config.yaml").write_text("num_exp: 100\n", encoding="utf-8")
    ident = experiment_id.resolve_configuration(doc, "T0:r0:c1", cmd, 1, repo=tmp_path)
    assert "different protocol" not in ident.reason


def test_a_baseline_row_is_never_bound_to_the_authors_command(tmp_path):
    r = _repo(tmp_path, "```bash\npython train.py --seed 0\n```\n",
              {"train.py": "from transformers import BertModel\nprint({'accuracy': 1})\n"
                           "class OurNet:\n    pass\n"})
    for caption in ("Accuracy on SST-2",
                    "Top-1 accuracy on CIFAR-10 and the other datasets used in prior work"):
        t = Table(table_idx=0, label="1", caption=caption, header=["method", "accuracy"],
                  rows=[["BERT-large (Devlin et al.)", "0.91"], ["BaselineNet", "0.88"],
                        ["OurNet-large (ours)", "0.93"]])
        doc = _doc("", [t])
        assert experiment_id.resolve_experiment(doc, r, "T0:r0:c1").state == "no_candidate"
        assert experiment_id.resolve_experiment(doc, r, "T0:r1:c1").state == "no_candidate"
        assert experiment_id.resolve_experiment(doc, r, "T0:r2:c1").state != "no_candidate"


def test_metric_name_prefers_the_caption_metric_and_ignores_escapes():
    t = Table(table_idx=0, label="1", caption="PEHE on two benchmarks", header=["m", "IHDP", "T"],
              rows=[["Ours", "0.5", "0.2"]])
    doc = _doc("", [t])
    loader = CandidateCommand(argv=["python", "main.py"], named_keys=["ihdp", "PEHE"])
    assert experiment_id.resolve_metric(doc, "T0:r0:c1", loader).output_key == "PEHE"


def test_only_identifier_shaped_literals_are_output_keys(tmp_path):
    (tmp_path / "main.py").write_text("sep = '\\t'\nmsg = 'Loading data now'\nd = {'PEHE': 1}\n",
                                      encoding="utf-8")
    cmd = experiment_id.describe_command(tmp_path, CandidateCommand(argv=["python", "main.py"]))
    assert cmd.named_keys == ["PEHE"]


def test_a_text_value_from_a_crashed_process_is_not_a_result():
    got = execute.parse_metric("epoch 1 loss: 0.93\n", "loss")
    assert got.tier == "text" and execute.usable_metric(got, 0)
    assert not execute.usable_metric(got, 1), "a line logged before a crash is not a result"
    js = execute.parse_metric('{"loss": 0.5}\n', "loss")
    assert execute.usable_metric(js, 1)          # a structured summary keeps its old rule


def test_seedless_repeats_never_fail_a_paper_aggregate():
    e, m, c = _established(["python", "main.py", "--seed", "42"])
    spec = ProbeSpec(paper_id="p", command=["python", "main.py", "--seed", "42"],
                     claimed_cell_value="0.12±0.01", table_ref="T0:r0:c1",
                     provenance="repo_exec", experiment=e, metric_identity=m, configuration=c)
    varied = execute.reconcile(spec, [0.30, 0.31, 0.29], 0.02, [0, 1, 2])
    assert varied.status == "INCONCLUSIVE"


def test_a_cached_primary_result_attaches_only_to_its_own_target(monkeypatch, tmp_path):
    from harness import state
    from harness.schema import ProbeResult, Reconciliation
    cfg = Config(projects_dir=tmp_path)
    root = state.project_dir(cfg, "p")
    state.write_json(state.control_dir(root) / "probe_results.json", ProbeResult(
        paper_id="p", verdict="done", reconciliation=Reconciliation(
            target_id="CERT", status="NO_VIOLATION_FOUND")).model_dump())
    objs = [_obj("EMP"), _obj("CERT", route="EXACT_CERTIFICATE")]
    ts = TargetSet(paper_id="p", objects=objs, plans=[_plan("EMP", "AUTHOR_CODE_EXECUTION"),
                                                      _plan("CERT", "EXACT_CERTIFICATE")])
    monkeypatch.setattr(discover, "load", lambda cfg, pid: ts)
    monkeypatch.setattr(discover, "targets_path_in", lambda root: tmp_path / "t.json",
                        raising=False)
    routes.resync_cached_outcomes(cfg, "p")
    emp = next(o for o in ts.outcomes if o.target_id == "EMP")
    assert emp.disposition == "NOT_ATTEMPTED" and "not for this one" in emp.reason


def test_result_tables_are_not_dropped_by_their_captions():
    for cap in ("Bias and coverage of the estimators under five data-generating processes",
                "FID of the models used to generate samples"):
        t = Table(table_idx=0, label="1", caption=cap, header=["m", "v"], rows=[["A", "0.1"]])
        assert paper.table_role(t) == "result", cap
    mixed = Table(table_idx=0, label="1", caption="Examples", header=["Model", "Data", "Score"],
                  rows=[["a long description of the model", "a long training corpus text", "71.2"],
                        ["another long description here", "another corpus description", "68.4"]])
    assert paper.table_role(mixed) == "result"
    assert locate.measurement_context("achieves an FID of 3.2")
    assert locate.measurement_context("reaches 0.83 NDCG@10")


def test_plain_text_metric_lines_are_read_and_ambiguity_refused():
    out = "(Exp 1)\tPEHE: 0.40\n(Exp 2)\tPEHE: 0.20\nPEHE      \t: 0.300 ± 0.050 /\n"
    got = execute.parse_metric(out, "PEHE")
    assert got.authoritative and got.value == 0.3 and got.tier == "text_aggregate"
    two = execute.parse_metric("PEHE : 0.3 ± 0.1\nPEHE : 0.5 ± 0.1\n", "PEHE")
    assert two.ambiguous and not two.authoritative


# --- 6. priority: printed results are not crowded out by proof fragments -----------------
def _obj(tid, centrality="SUPPORTING", route="AUTHOR_CODE_EXECUTION", parent=""):
    return DiscoveredObject(target_id=tid, centrality=centrality, routes=[route],
                            harness_addressable=True, parent_target=parent)


def _plan(tid, route):
    return PlanDecision(target_id=tid, action=decide._ACTION_FOR_ROUTE[route], route=route,
                        requires_execution=True)


def test_route_families_have_separate_budgets_and_empirical_goes_first(monkeypatch):
    objs = [_obj(f"C{i}", route="EXACT_CERTIFICATE") for i in range(5)] + [_obj("E1"), _obj("E2")]
    plans = [_plan(o.target_id, o.routes[0]) for o in objs]
    ts = TargetSet(paper_id="p", objects=objs, plans=plans)
    monkeypatch.setattr(discover, "load", lambda cfg, pid: ts)
    _ts, pursued, deferred = routes._executable_targets(Config(max_targets=3), "p")
    ids = [o.target_id for o, _ in pursued]
    assert ids[:2] == ["E1", "E2"] and sum(i.startswith("C") for i in ids) == 3
    assert len(deferred) == 2


def test_a_central_certificate_does_not_demote_a_supporting_printed_result():
    objs = [_obj("C", "CENTRAL", "EXACT_CERTIFICATE"), _obj("E")]
    plans = [_plan("C", "EXACT_CERTIFICATE"), _plan("E", "AUTHOR_CODE_EXECUTION")]
    out = discover._demote_when_a_central_target_is_being_pursued(objs, plans)
    assert out[1].requires_execution, "a theorem being certified says nothing about a run"


def test_a_proof_step_fragment_ranks_after_whole_claims():
    whole = _obj("W", "CENTRAL", "AUTHOR_CODE_EXECUTION")
    step = _obj("S", "CENTRAL", "EXACT_CERTIFICATE", parent="W0")
    assert [o.target_id for o in decide.order([step, whole])] == ["W", "S"]


# --- 7. a finished workflow is not a finished scientific check ---------------------------
def _ts(outcomes, objs):
    return TargetSet(paper_id="p", objects=objs, outcomes=outcomes)


def test_blocked_or_clean_sweeps_conclude_nothing_and_name_the_blocker():
    objs = [_obj("A", "CENTRAL"), _obj("B", "CENTRAL", "EXACT_CERTIFICATE")]
    sci = decide.scientific_outcome(_ts([
        TargetOutcome(target_id="A", disposition="IDENTITY_BLOCKED",
                      route="AUTHOR_CODE_EXECUTION", reason="no command emits PEHE"),
        TargetOutcome(target_id="B", disposition="NO_COUNTEREXAMPLE_FOUND",
                      route="EXACT_CERTIFICATE", provenance="cert_exec")], objs))
    assert sci["status"] == "NO_CONCLUSIVE_CHECK" and "PEHE" in sci["blocker"]
    assert sci["by_kind"]["finite_instance"] == {"NO_COUNTEREXAMPLE_FOUND": 1}


def test_outcome_kinds_stay_distinct():
    objs = [_obj("S", "CENTRAL", "EXACT_CERTIFICATE", parent="T"), _obj("R", "CENTRAL"),
            _obj("X", "CENTRAL")]
    sci = decide.scientific_outcome(_ts([
        TargetOutcome(target_id="S", disposition="COUNTEREXAMPLE_ESTABLISHED",
                      route="EXACT_CERTIFICATE", provenance="cert_exec"),
        TargetOutcome(target_id="X", disposition="REPRODUCED", route="AUTHOR_CODE_EXECUTION",
                      provenance="synthesized")], objs))
    assert sci["proof_step_defects"] == ["S"] and sci["status"] == "NO_CONCLUSIVE_CHECK"
    sci = decide.scientific_outcome(_ts([
        TargetOutcome(target_id="R", disposition="REPRODUCED", route="AUTHOR_CODE_EXECUTION",
                      provenance="repo_exec")], objs))
    assert sci["status"] == "CENTRAL_CLAIM_CHECKED" and sci["central_checked"] == ["R"]
