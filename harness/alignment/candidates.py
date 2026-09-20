"""What configuration a candidate command declares itself, in three tiers of evidence.

`python -m harness.alignment.candidates` runs the self-check.

**Three sources, most-specific-first, never a filename.** A `scripts_dir` candidate's
`cmd.argv` is only `["bash", "scripts/prune_ratio50.sh"]` — the configuration a script
actually runs under is HARDCODED INSIDE the file, not in the invocation this harness
sees, exactly as `experiment_id.describe_command` already reads a script's own text to
find its output keys:

  1. a `--flag value` pair the script's OWN TEXT hardcodes — the strongest signal, since
     it is what that specific invocation will actually pass;
  2. a value in a config file the command references (`alignment.configs`);
  3. an `argparse` DEFAULT for a flag the script never overrides
     (`alignment.argparse_surface`) — the weakest of the three, since a default is what
     runs only when nothing else set it, but still a fact the entrypoint's own source
     states rather than a guess.

**A candidate's FILENAME is deliberately never a source.** `scripts/run_experiment_2.sh`
does not mean "configuration 2" — a name is not evidence and every identity claim this
harness makes needs a quotable source_ref (invariant-shaped, the same discipline
`experiment_id`'s evidence pointers already follow). Filename-based inference would be the
one place in this module that could silently mislabel a script's configuration, so it is
not built.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import experiment_id as experiment_id_mod
from ..schema import CandidateCommand
from . import argparse_surface, configs
from .configuration import FIELD_FLAG_HINTS

# `--flag value`, `--flag=value`, or `--flag "value"` — a generic capture, filtered
# afterwards against `FIELD_FLAG_HINTS` so an unrelated flag (`--epochs`, `--lr`) is never
# mistaken for a configuration field.
_FLAG_VALUE = re.compile(r"--([\w-]+)(?:[= ]+(\"[^\"]*\"|'[^']*'|\S+))?")

# A shell variable REFERENCE, not a literal — `$model_name`, `${model_name}`,
# `./${model_dir}/x`. Real APT-shaped repositories parametrise scripts this way at scale
# (162 candidates examined, 76 of them passing `--model_name_or_path "${model_name}"`),
# and the value that ends up there is set ELSEWHERE — a line above, a parent script, an
# outer loop — not something this module resolves. Measured before this filter existed:
# every one of those 76 candidates was read as declaring the LITERAL string
# `'${model_name}'`, which then CONTRADICTED a cited row's real model name and eliminated
# every one of them, on evidence that was never real. A value containing `$` is treated
# as unresolved — exactly like a candidate that declares nothing for that field — rather
# than guessed at or, worse, treated as a literal that happens to disagree.
_UNRESOLVED_VAR = re.compile(r"\$")


def _field_for_flag(flag: str) -> str:
    low = flag.lower()
    for field, hints in FIELD_FLAG_HINTS.items():
        if any(h in low for h in hints):
            return field
    return ""


def script_text_fields(text: str) -> dict[str, str]:
    """Every configuration field a `--flag value` pair in `text` sets, first per field.

    A flag with no value (`--verbose`) is skipped — it declares nothing about a field
    this module compares.
    """
    out: dict[str, str] = {}
    for m in _FLAG_VALUE.finditer(text or ""):
        flag, value = m.group(1), m.group(2)
        if not value:
            continue
        # A shell line-continuation backslash directly abutting the value (no space
        # before it, e.g. `--sparsity 0.06\` at an end of line) is punctuation, not part
        # of the number — stripped so `0.06\` and `0.06` compare identically instead of
        # differing on an artifact of how the script wraps its own invocation.
        value = value.strip("'\"").rstrip("\\")
        if _UNRESOLVED_VAR.search(value):
            continue                     # a shell variable reference, not a literal
        field = _field_for_flag(flag)
        if field and field not in out:
            out[field] = value
    return out


def argparse_default_fields(specs) -> dict[str, str]:
    """Every field an `argparse` DEFAULT sets, for a flag that maps to one."""
    out: dict[str, str] = {}
    for spec in specs:
        if not spec.default:
            continue
        field = _field_for_flag(spec.flag)
        if field and field not in out:
            out[field] = spec.default
    return out


def declared_configuration(repo: Path, cmd: CandidateCommand) -> CandidateCommand:
    """`cmd`, with `declared_args` / `declared_args_evidence` filled from its own text.

    Mutates and returns `cmd`, exactly as `experiment_id.describe_command` does for
    `emits`/`label`/`seed_flag` — the two are meant to run back to back over the same
    candidate. Idempotent: a field `cmd.declared_args` already carries (from an earlier
    call, or written by a caller directly for a test) is never overwritten.
    """
    path = experiment_id_mod.target_file(repo, cmd)
    text = ""
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""

    # Tier 1 — the script's own hardcoded flags. Strongest: what THIS invocation runs.
    for field, value in script_text_fields(text).items():
        if field not in cmd.declared_args:
            cmd.declared_args[field] = value
            cmd.declared_args_evidence[field] = f"{path}" if path else cmd.source_ref

    # Tier 2 — a config file THIS command references, and only that: the conventional-
    # directory sweep is excluded here (`include_conventional=False`), or every candidate
    # sharing a repository would inherit the same repo-wide config regardless of whether
    # it references it, which would make several unrelated scripts look identically
    # configured.
    cfg_fields, cfg_evidence = configs.declared_fields(
        repo, cmd, script_text=text, include_conventional=False)
    for field, value in cfg_fields.items():
        if field not in cmd.declared_args:
            cmd.declared_args[field] = value
            cmd.declared_args_evidence[field] = cfg_evidence[field]

    # Tier 3 — an argparse default for a flag the script never overrides. Only reachable
    # when the command's OWN file is itself the entrypoint (a `.py` file) or wraps one —
    # `argparse_surface` reads the file `target_file` resolved, which for a `.sh` wrapper
    # is the shell script, not the python it calls; `describe_command` already follows
    # that one hop for output keys; this module does not repeat that hop for defaults,
    # since a default only matters when nothing more specific overrides it, and a
    # shell wrapper that hardcodes a flag would have already supplied Tier 1.
    if path is not None and path.suffix == ".py":
        for field, value in argparse_default_fields(argparse_surface.parse_source(
                text, source_name=str(path))).items():
            if field not in cmd.declared_args:
                cmd.declared_args[field] = value
                cmd.declared_args_evidence[field] = f"{path} (argparse default)"

    return cmd


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    # --- flag/default field mapping -----------------------------------------------------
    assert script_text_fields(
        "python eval.py --sparsity 0.5 --base_model llama-7b --epochs 10") == {
        "sparsity": "0.5", "model": "llama-7b"}
    assert script_text_fields("python eval.py --verbose") == {}
    assert script_text_fields("") == {}

    # --- THE REGRESSION: an unresolved shell variable is never a declared literal ------
    # Measured on a real corpus: 76 of 162 candidates on one paper pass
    # `--model_name_or_path "${model_name}"`, and reading that as the LITERAL string
    # '${model_name}' made every one of them appear to CONTRADICT the cited row's real
    # model name — narrowing 84 candidates to 0 real evidence and a wall of false
    # contradictions in the record.
    assert script_text_fields('python eval.py --model_name_or_path "${model_name}"') == {}
    assert script_text_fields("python eval.py --sparsity $ratio") == {}
    assert script_text_fields("python eval.py --model ./${model_dir}/ckpt") == {}
    # a REAL literal alongside an unresolved one: only the literal is kept
    assert script_text_fields(
        'python eval.py --sparsity 0.5 --model_name_or_path "${model_name}"') == {
        "sparsity": "0.5"}

    # a shell line-continuation backslash abutting the value is stripped, not read as
    # part of the number
    assert script_text_fields("python eval.py --sparsity 0.06\\\n  --seed 1") == {
        "sparsity": "0.06"}

    from ..schema import ArgSpec
    specs = [ArgSpec(flag="--pruning_ratio", default="0.2"),
            ArgSpec(flag="--epochs", default="10")]
    assert argparse_default_fields(specs) == {"sparsity": "0.2"}

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()

        # --- Tier 1: the script's own hardcoded flag wins over everything -------------
        (repo / "scripts" / "run.sh").write_text(
            "#!/bin/bash\npython eval.py --sparsity 0.5 --base_model llama-7b\n",
            encoding="utf-8")
        cmd = CandidateCommand(argv=["bash", "scripts/run.sh"], source_ref="scripts/run.sh:1")
        out = declared_configuration(repo, cmd)
        assert out is cmd, "mutates and returns the SAME object"
        assert cmd.declared_args["sparsity"] == "0.5"
        assert cmd.declared_args["model"] == "llama-7b"
        assert "run.sh" in cmd.declared_args_evidence["sparsity"]

        # --- Tier 2: a config file supplies a field the script's own text does not ----
        (repo / "configs").mkdir()
        (repo / "configs" / "x.yaml").write_text("dataset: alpaca\n", encoding="utf-8")
        (repo / "scripts" / "run2.sh").write_text(
            "python eval.py --config configs/x.yaml --sparsity 0.8\n", encoding="utf-8")
        cmd2 = CandidateCommand(argv=["bash", "scripts/run2.sh"], source_ref="scripts/run2.sh:1")
        declared_configuration(repo, cmd2)
        assert cmd2.declared_args["sparsity"] == "0.8", "tier 1 still wins where it applies"
        assert cmd2.declared_args["dataset"] == "alpaca", "tier 2 fills what tier 1 left open"
        assert cmd2.declared_args_evidence["dataset"].endswith("x.yaml")

        # --- Tier 3: an argparse default, only for a genuine .py entrypoint ------------
        (repo / "eval.py").write_text(
            "import argparse\np = argparse.ArgumentParser()\n"
            "p.add_argument('--sparsity', default='0.3')\n", encoding="utf-8")
        cmd3 = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
        declared_configuration(repo, cmd3)
        assert cmd3.declared_args["sparsity"] == "0.3"
        assert "argparse default" in cmd3.declared_args_evidence["sparsity"]

        # --- idempotent: a field already set is never overwritten ---------------------
        cmd4 = CandidateCommand(argv=["bash", "scripts/run.sh"], source_ref="scripts/run.sh:1",
                                declared_args={"sparsity": "PRESET"})
        declared_configuration(repo, cmd4)
        assert cmd4.declared_args["sparsity"] == "PRESET"

        # --- a candidate whose file cannot be read yields nothing, never a crash ------
        ghost = CandidateCommand(argv=["bash", "scripts/missing.sh"], source_ref="x:1")
        declared_configuration(repo, ghost)
        assert ghost.declared_args == {}

        # --- NO FILENAME-BASED INFERENCE: a suggestive name alone declares nothing ----
        named = CandidateCommand(argv=["bash", "scripts/sparsity50.sh"], source_ref="x:1")
        (repo / "scripts" / "sparsity50.sh").write_text("python eval.py --epochs 10\n",
                                                        encoding="utf-8")
        declared_configuration(repo, named)
        assert "sparsity" not in named.declared_args, (
            "the number in the FILENAME must never become a declared field")
    print("harness.alignment.candidates self-check ok")


if __name__ == "__main__":
    _self_check()
