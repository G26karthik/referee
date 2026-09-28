"""Regressions from the cold review of 5413ba6: each is a way the new binding could hold the
authors' run against a number their code did not produce (a cited baseline, another metric,
another protocol), attribute someone else's repository, or run a command with a hole in it.

Synthetic documents and repositories only — no paper names, numbers or expected verdicts.
Run: `python -m pytest tests/test_binding_review.py -q`
"""
from __future__ import annotations

from pathlib import Path

from harness import discover, experiment_id, paper, repo
from harness.schema import CandidateCommand, PaperDoc, Section, Table

_ABSTRACT = "We propose FooNet, a small model. FooNet outperforms BERT and GPT-4 (Table 1)."


def _doc(tables: list[Table], title: str = "FooNet: Small Models for Something Specific",
         front: str = "John Smith  Jane Doe\nNew York University") -> PaperDoc:
    return PaperDoc(paper_id="p", title=title, tables=tables, sections=[
        Section(section_idx=0, title="", text=front),
        Section(section_idx=1, title="Abstract", text=_ABSTRACT)])


def _repo(tmp_path: Path, readme: str, files: dict[str, str]) -> Path:
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    return tmp_path


_MAIN = ("from transformers import BertModel\nimport argparse\n"
         "p = argparse.ArgumentParser(); p.add_argument('--seed', type=int)\n"
         "def mean(x):\n    return sum(x) / len(x)\nclass FooNet:\n    pass\n"
         "print({'accuracy': 0.9})\n")


# --- 1. a cited or unnamed baseline row is never the authors' run --------------------------
def test_baseline_rows_are_not_bound_to_the_authors_command(tmp_path):
    r = _repo(tmp_path, "```bash\npython main.py\n```\n", {"main.py": _MAIN, "test.py": ""})
    t = Table(table_idx=0, label="1", caption="Test accuracy on SST-2 (mean ± std over 5 seeds)",
              header=["Method", "Accuracy"],
              rows=[["BERT (Devlin et al., 2019)", "91.2"], ["Mean Teacher", "90.1"],
                    ["Test-time training [12]", "89.9"], ["BERT", "90.5"], ["FooNet (ours)", "92.0"]])
    doc = _doc([t])
    for row in range(4):
        e = experiment_id.resolve_experiment(doc, r, f"T0:r{row}:c1")
        assert e.state == "no_candidate", (t.rows[row][0], e.state, e.reason)
    own = experiment_id.resolve_experiment(doc, r, "T0:r4:c1")
    assert own.state == "established", own.reason
    cfg = experiment_id.resolve_configuration(doc, "T0:r1:c1", own.command, 5, repo=r)
    assert cfg.matched.get("model") != "Mean Teacher", "the command takes no argument naming it"


def test_a_numeric_row_takes_its_method_from_the_column(tmp_path):
    r = _repo(tmp_path, "```bash\npython main.py\n```\n", {"main.py": _MAIN})
    t = Table(table_idx=0, label="2", caption="SST-2 accuracy of a BERT backbone by data size",
              header=["n", "FooNet", "LoRA [9]"], rows=[["100", "80.1", "79.0"], ["1000", "88.2", "87.5"]])
    doc = _doc([t])
    assert experiment_id.resolve_experiment(doc, r, "T0:r0:c2").state == "no_candidate"
    assert experiment_id.resolve_experiment(doc, r, "T0:r0:c1").state == "established"


def test_a_row_the_command_selects_by_argument_is_bound(tmp_path):
    r = _repo(tmp_path, "```bash\nfor m in bert foonet; do python main.py --model \"$m\"; done\n```\n",
              {"main.py": _MAIN})
    t = Table(table_idx=0, label="1", caption="Accuracy on SST-2", header=["Method", "Accuracy"],
              rows=[["BERT", "90.5"]])
    e = experiment_id.resolve_experiment(_doc([t]), r, "T0:r0:c1")
    assert e.state == "established" and e.command.argv[-1] == "bert", e.reason


def test_the_central_claim_is_the_papers_own_row_not_a_baseline_the_abstract_names():
    doc = _doc([])
    assert discover._own_row(doc, "FooNet")
    for baseline in ("BERT (Devlin et al., 2019)", "GPT-4 [3]", "BERT", "Mean Teacher"):
        assert not discover._own_row(doc, baseline), baseline


# --- 2. only first-person, availability-stated code is the paper's own --------------------
def test_third_party_code_mentions_attribute_nothing():
    for sentence in (
            "For the baselines, our pipeline uses the implementation provided by the original "
            "authors (https://github.com/smith/baseline).",
            "Our baselines use the code released by their authors: https://github.com/a/b here.",
            "Our method uses the repository of Chen et al. https://github.com/chen/tool now."):
        doc = PaperDoc(paper_id="p", sections=[Section(section_idx=0, title="M", text=sentence)])
        assert repo.official_repo_url(doc) == "", sentence
    own = PaperDoc(paper_id="p", sections=[Section(section_idx=0, title="M", text=(
        "Our code is publicly available at https://github.com/us/ours for everyone."))])
    assert repo.official_repo_url(own) == "https://github.com/us/ours"


# --- 3. a README about the paper is not the authors' README ------------------------------
def test_readme_attribution_needs_a_first_person_statement_outside_citations():
    doc = _doc([], title="FooNet Small Models for Something Specific")
    for foreign in (
            "# FooNet\nImplementation of FooNet Small Models for Something Specific, in Pytorch\n"
            "```bibtex\n@inproceedings{x, author={Smith, John and Doe, Jane},\n"
            "title={FooNet Small Models for Something Specific}}\n```\n",
            "# FooNet Small Models for Something Specific\nThe official code has not been "
            "released yet, so this is my implementation.",
            "# FooNet Small Models for Something Specific\nMade in New York by John Smith."):
        assert not repo.readme_attributes(foreign, doc), foreign
    assert repo.readme_attributes("# FooNet Small Models for Something Specific\nOfficial "
                                  "PyTorch implementation of our paper.", doc)


# --- 4. a hole inside a token, and a positional's program file, are not settings ---------
def test_holes_inside_tokens_are_refused_and_positionals_name_no_dataset():
    for argv in (["python", "run.py", "--config", "configs/<dataset>.yaml"],
                 ["python", "run.py", "--data_dir=<path/to/data>"],
                 ["python", "run.py", "--task", "{task_name}"]):
        filled, why = experiment_id.instantiate(CandidateCommand(argv=argv), "anything")
        assert filled is None and "hole" in why, argv
    t = Table(table_idx=0, label="1", caption="Accuracy of FooNet", header=["m", "Accuracy"],
              rows=[["llama", "0.5"]])
    cmd = CandidateCommand(argv=["python", "eval_task.py", "llama"], bound_slots={"<m>": "llama"})
    cfg = experiment_id.resolve_configuration(_doc([t]), "T0:r0:c1", cmd, 1)
    assert cfg.matched.get("dataset") != "llama"


# --- 6. a metric name never overrides the quantity the column reports --------------------
def test_by_name_binding_respects_the_columns_own_quantity():
    t = Table(table_idx=0, label="1", caption="Accuracy and wall-clock time on CIFAR-10 "
              "(mean ± std, 5 seeds)", header=["Method", "Acc.", "Time (s)", "Top-5"],
              rows=[["FooNet", "91.2", "310", "99.0"]])
    cmd = CandidateCommand(argv=["python", "main.py"], emits=["accuracy"], named_keys=["accuracy"])
    doc = _doc([t])
    for col in (2, 3):
        m = experiment_id.resolve_metric(doc, f"T0:r0:c{col}", cmd)
        assert not m.established, (t.header[col], m.reason)
    assert experiment_id.resolve_metric(doc, "T0:r0:c1", cmd).established
    nometric = Table(table_idx=1, label="2", caption="Results on two benchmarks",
                     header=["m", "IHDP", "Ours"], rows=[["x", "0.5", "0.2"]])
    assert paper.metric_name(nometric, 1) == ""


def test_a_metric_word_inside_a_specification_caption_measures_nothing():
    spec = Table(table_idx=0, label="5", caption="Hyperparameters used for training the reward "
                 "model on Alpaca", header=["Parameter", "Value"],
                 rows=[["learning rate", "1e-4"], ["batch size", "32"]])
    assert paper.table_role(spec) == "specification"
    sweep = Table(table_idx=1, label="6", caption="RMSE under different hyper-parameters",
                  header=["lr", "0.1", "0.01"], rows=[["Ours", "0.3", "0.2"]])
    assert paper.table_role(sweep) == "result"


def test_an_outcome_in_setting_units_is_still_a_reported_number():
    from harness import locate
    assert locate.measurement_context("the method converges in 12 iterations")
    assert locate.measurement_context("training takes 2 hours on one GPU")
    assert not locate.measurement_context("we train for 30 epochs with 3 layers")


# --- 7. a pinned seed and a shell wrapper are run as documented ---------------------------
def test_pinned_or_wrapped_commands_get_no_harness_seed():
    pinned = CandidateCommand(argv=["python", "main.py", "--seed=42"], seed_flag="--seed")
    wrapped = CandidateCommand(argv=["bash", "scripts/reproduce.sh"], seed_flag="--seed")
    free = CandidateCommand(argv=["python", "main.py"], seed_flag="--seed")
    assert experiment_id.harness_seed_flag(pinned) == ""
    assert experiment_id.harness_seed_flag(wrapped) == ""
    assert experiment_id.harness_seed_flag(free) == "--seed"
    templated = CandidateCommand(argv=["python", "main.py", "--seed", "{seed}"])
    assert experiment_id.instantiate(templated, "x")[0] is None, "a README {seed} pins nothing"


# --- second review (cb95922): layout, names, quantities, cues ----------------------------
def test_methods_as_columns_bind_only_the_papers_own_column(tmp_path):
    r = _repo(tmp_path, "```bash\nfor d in mnli sst2; do python main.py --data $d; done\n```\n",
              {"main.py": _MAIN})
    t = Table(table_idx=0, label="1", caption="Accuracy on GLUE", header=["Task", "BERT", "FooNet"],
              rows=[["MNLI", "84.0", "86.1"]])
    doc = _doc([t])
    assert experiment_id.resolve_experiment(doc, r, "T0:r0:c1").state == "no_candidate"
    own = experiment_id.resolve_experiment(doc, r, "T0:r0:c2")
    assert own.state == "established", own.reason
    cfg = experiment_id.resolve_configuration(doc, "T0:r0:c2", own.command, 5, repo=r)
    assert cfg.matched.get("model") == "FooNet", "a --data slot names the dataset, never the model"
    cited = Table(table_idx=1, label="2", caption="Fine-tuning BERT-base on SST-2",
                  header=["m", "Accuracy"], rows=[["[12]", "90.1"]])
    assert experiment_id.resolve_experiment(_doc([cited]), r, "T1:r0:c1").state == "no_candidate"


def test_own_names_come_from_the_phrase_the_paper_introduces():
    def names(abstract, title="A Study of Something"):
        return experiment_id.own_names(PaperDoc(paper_id="p", title=title, sections=[
            Section(section_idx=0, title="Abstract", text=abstract)]))
    assert names("We propose Kite, a fast optimizer. Kite outperforms AdamW.") == {"kite"}
    assert names("We introduce FooNet, which improves on Low-Rank Adaptation (LoRA).") == {"foonet"}
    assert names("We propose a GNN-based model, FooNet, for graphs.") == {"foonet"}
    assert names("We propose Bayesian optimization with priors.") == set()
    assert names("x", title="Kite: A Fast Optimizer") == {"kite"}
    assert names("We intro- duce GRAdient-based Causal tree Ensembles (GRACE), a model.") == {"grace"}
    assert experiment_id.own_method(_doc([]), "FooNet (d=2048)")


def test_quantities_are_whole_words_and_a_column_keeps_its_own():
    q = experiment_id._quantity_of
    assert q("MSE") == "" and q("MS MARCO") == "" and q("embedding dim") == ""
    assert q("Latency (ms)") == "latency" and q("Time (s)") == "latency" and q("pass@1") == "accuracy"
    cmd = CandidateCommand(argv=["python", "main.py"], emits=["accuracy"], named_keys=["accuracy", "ECE"])
    ece = Table(table_idx=0, label="1", caption="Accuracy and calibration on CIFAR-10",
                header=["Method", "Acc.", "ECE"], rows=[["FooNet", "91.2", "0.03"]])
    assert experiment_id.resolve_metric(_doc([ece]), "T0:r0:c2", cmd).output_key != "accuracy"
    top5 = Table(table_idx=0, label="1", caption="Accuracy on ImageNet", header=["Method", "Top-5"],
                 rows=[["FooNet", "99.0"]])
    assert not experiment_id.resolve_metric(_doc([top5]), "T0:r0:c1", cmd).established
    sized = Table(table_idx=0, label="1", caption="ImageNet accuracy of FooNet variants of "
                  "increasing size", header=["Model", "ImageNet"], rows=[["FooNet-S", "80.1"]])
    assert experiment_id.cell_quantity(sized, 1) == "accuracy"
    marco = Table(table_idx=0, label="1", caption="NDCG@10 on MS MARCO", header=["m", "MS MARCO"],
                  rows=[["FooNet", "0.41"]])
    assert paper.metric_name(marco, 1) == "NDCG"


def test_a_numeric_row_takes_its_configuration_from_the_own_column():
    t = Table(table_idx=0, label="2", caption="Accuracy by data size", header=["n", "FooNet"],
              rows=[["100", "80.1"]])
    cmd = CandidateCommand(argv=["python", "main.py"])
    assert experiment_id.resolve_configuration(_doc([t]), "T0:r0:c1", cmd, 1).matched["model"] == "FooNet"


def test_impersonal_cues_attributed_to_others_and_ranking():
    def url(text):
        return repo.official_repo_url(PaperDoc(paper_id="p", sections=[
            Section(section_idx=0, title="M", text=text)]))
    assert url("For comparison the baseline implementation is available at "
               "https://github.com/smith/baseline today.") == ""
    assert url("The baseline code is available at https://github.com/smith/b here. Our code is "
               "available at https://github.com/us/ours now.") == "https://github.com/us/ours"
    assert url("Code is available at https://github.com/us/ours now.") == "https://github.com/us/ours"


def test_readme_negations_forks_and_unrelated_implementations():
    doc = _doc([], title="FooNet Small Models for Something Specific")
    t = "# FooNet Small Models for Something Specific\n"
    assert not repo.readme_attributes(t + "This is not the official code.", doc)
    assert not repo.readme_attributes(t + "A community fork of the official code.", doc)
    assert repo.readme_attributes(t + "Official code. It uses the implementation of "
                                  "FlashAttention; checkpoints have not been released yet.", doc)
    import time
    start = time.time()
    repo.readme_attributes("@a{ " * 20000, doc)
    assert time.time() - start < 2, "a pathological README must stay linear"


def test_read_ranges_stay_under_the_cap_on_dense_text(tmp_path):
    from harness import tasks
    p = tmp_path / "dense.md"
    p.write_text("\n".join("- [T5:r7:c3] 0.123 ± 0.004 | 12,345 | 9.87e-3" for _ in range(1200)),
                 encoding="utf-8")
    ranges = tasks.read_ranges(p)
    lines = p.read_text(encoding="utf-8").split("\n")
    assert sum(n for _, n in ranges) == len(lines) and ranges[0][0] == 1
    assert all(sum(len(x) + 1 for x in lines[o - 1:o - 1 + n]) <= tasks.READ_CHUNK_CHARS + 60
               for o, n in ranges)


def test_anchor_is_kept_when_the_part_shows_only_the_start_of_it():
    from harness.paper import anchors, plan, render_part_with_anchor
    doc = PaperDoc(paper_id="p", title="T", sections=[
        Section(section_idx=0, title="Abstract", page_start=1, text=("claim500 " + "a " * 3000)),
        Section(section_idx=1, title="Method", page_start=2, text="m " * 3000),
        Section(section_idx=2, title="Conclusion", page_start=3, text="done.")])
    p = plan(doc, 5000)
    first = render_part_with_anchor(p.parts[0], anchors(doc))
    assert first.count("claim500") >= 1
    body_has_all = any(i == 0 and a == 0 and b >= len(doc.sections[0].text)
                       for i, a, b in p.parts[0].slices)
    assert body_has_all or "## Abstract  [section 0]" in first
