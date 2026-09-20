"""Configuration files a script loads, and the flat field values they declare.

`python -m harness.alignment.configs` runs the self-check.

**No YAML dependency.** This project's dependency policy is conservative — every entry in
`requirements.txt` carries its own justification, and none of the existing stages need a
YAML parser. A repository's config file is read here for FLAT scalar fields only
(`sparsity: 0.5`, `model: llama-7b`), which a deliberately narrow line-oriented parser
recovers without one; nested structures, anchors and multi-document files are outside what
this module claims to parse; and a file this cannot read yields no fields rather than a
guess. JSON is parsed with the standard library, needing nothing extra.

**Only files the repository ITSELF points at, or a canonical location.** A `--config`
flag naming `configs/llama7b_50.yaml` is the repository saying "this is my configuration";
`configs/`, `conf/` and `config/` directories are conventional enough to enumerate on their
own, so a script referencing a config by a relative name that resolves under one of them
is still found. Nothing here searches the whole checkout for files that merely look like
configuration.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..schema import CandidateCommand

_CONFIG_EXTS = (".yaml", ".yml", ".json")
_CONFIG_DIRS = ("configs", "conf", "config")

# `--config path/to/x.yaml` or `--cfg=path/to/x.yaml`. The flag name itself is the signal
# — this harness does not know a repository's own flag vocabulary beyond that a flag
# whose name CONTAINS "config" or "cfg" is, by the overwhelming convention of the
# ecosystems this reviews, a path to one.
_CONFIG_FLAG = re.compile(r"--?\w*(?:config|cfg)\w*[= ]+([^\s'\"]+)", re.I)

# `key: value` — one per line, no nesting, no lists, no anchors. A line this cannot parse
# (a nested mapping, a list item, a blank line, a comment) is skipped rather than guessed.
_YAML_FLAT_LINE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:\s*(.+?)\s*(?:#.*)?$")


def referenced_config_paths(cmd: CandidateCommand, script_text: str = "") -> list[str]:
    """Every config-shaped path this command's own argv or script text names.

    Checks BOTH `cmd.argv` (a `run_script`/`readme` candidate's command line may name the
    config directly) and the script's own text (a `scripts_dir` candidate's config flag,
    if any, is hardcoded inside the `.sh` file rather than in `cmd.argv`, exactly as a
    `--sparsity` value is).
    """
    joined = " ".join(cmd.argv) + " " + (script_text or "")
    return [m.group(1) for m in _CONFIG_FLAG.finditer(joined)]


def discover(repo: Path, cmd: CandidateCommand | None = None,
            script_text: str = "", *, include_conventional: bool = True) -> list[Path]:
    """Config files this candidate references, plus the conventional config directories.

    Referenced paths come first — they are the strongest signal, a repository naming its
    own configuration file for THIS command — followed by every config-shaped file under
    a conventional directory, deduplicated, existing files only.

    `include_conventional=False` restricts this to ONLY files the command's own argv or
    text actually names. `harness.alignment.candidates.declared_configuration` uses that:
    attributing a repo-wide `configs/whatever.yaml` to every candidate regardless of
    whether IT references that file would make several unrelated candidates appear to
    share a configuration none of them actually declared, which is the opposite of what
    per-candidate matching needs. The unrestricted default remains for a caller that
    wants "what configuration does this repository carry", not "what does THIS command
    declare".
    """
    out: list[Path] = []
    seen: set[Path] = set()
    if cmd is not None:
        for rel in referenced_config_paths(cmd, script_text):
            p = (repo / rel).resolve()
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    if not include_conventional:
        return out
    for d in _CONFIG_DIRS:
        base = repo / d
        if not base.is_dir():
            continue
        for ext in _CONFIG_EXTS:
            for p in sorted(base.rglob(f"*{ext}"))[:100]:
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
    return out


def parse_flat(text: str) -> dict[str, str]:
    """Every top-level `key: value` line in `text`, as plain strings.

    Deliberately incapable of nesting: a config file whose fields sit under a `model:`
    block indented two spaces yields nothing for those fields rather than a value read
    from the wrong section. That is the honest limit stated in the module docstring, not
    a bug — a caller matching against `parse_flat`'s output is matching against what this
    harness could recover, and a miss here is reported as such rather than guessed past.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if line[:1].isspace():
            continue                      # nested — outside what this parser claims
        if m := _YAML_FLAT_LINE.match(stripped):
            out[m.group(1)] = m.group(2).strip().strip("'\",")
    return out


def parse_file(path: Path) -> dict[str, str]:
    """`parse_flat` for YAML-shaped text, `json.loads` (flattened one level) for JSON.

    A JSON file is parsed properly rather than line-matched, since the standard library
    already does it correctly; only its TOP-LEVEL scalar and one-level-nested values are
    kept, for the same reason `parse_flat` stays flat — a value several levels deep is a
    section this harness does not claim to have read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                out[str(k)] = str(v)
        return out
    return parse_flat(text)


def declared_fields(repo: Path, cmd: CandidateCommand | None = None,
                    script_text: str = "", *,
                    include_conventional: bool = True) -> tuple[dict[str, str], dict[str, str]]:
    """(fields, evidence) merged over every config file this candidate references.

    A field named by more than one file keeps the FIRST source that named it — the same
    first-wins rule `argparse_surface.by_flag` applies to a duplicate declaration. Only
    files `discover` actually finds are consulted; a candidate with no config at all
    yields nothing here, honestly, rather than falling back to a directory-wide guess
    (see `include_conventional`, forwarded to `discover`).
    """
    fields: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for path in discover(repo, cmd, script_text, include_conventional=include_conventional):
        for k, v in parse_file(path).items():
            if k not in fields:
                fields[k] = v
                evidence[k] = str(path)
    return fields, evidence


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    assert parse_flat("sparsity: 0.5\nmodel: llama-7b\n# a comment\n") == {
        "sparsity": "0.5", "model": "llama-7b"}
    assert parse_flat("dataset:\n  name: alpaca\n") == {}, (
        "a nested field is outside what this parser claims — no guessing past it"
    )
    assert parse_flat("- a list item\nfoo: bar") == {"foo": "bar"}
    assert parse_flat("") == {}

    assert json.loads('{"a": 1}')                       # sanity: stdlib available

    cmd = CandidateCommand(argv=["bash", "scripts/prune.sh"])
    assert referenced_config_paths(cmd, "python eval.py --config configs/llama7b_50.yaml") \
        == ["configs/llama7b_50.yaml"]
    assert referenced_config_paths(cmd, "python eval.py --cfg=conf/x.json") == ["conf/x.json"]
    assert referenced_config_paths(cmd, "python eval.py --seed 3") == []

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "configs").mkdir()
        (repo / "configs" / "llama7b_50.yaml").write_text(
            "sparsity: 0.5\nbase_model: llama-7b\n", encoding="utf-8")
        (repo / "conf").mkdir()
        (repo / "conf" / "x.json").write_text('{"dataset": "alpaca", "seed": 3}',
                                              encoding="utf-8")

        found = discover(repo, cmd, "python eval.py --config configs/llama7b_50.yaml")
        assert any(p.name == "llama7b_50.yaml" for p in found)

        fields, evidence = declared_fields(
            repo, cmd, "python eval.py --config configs/llama7b_50.yaml")
        assert fields["sparsity"] == "0.5" and fields["base_model"] == "llama-7b"
        assert evidence["sparsity"].endswith("llama7b_50.yaml")

        # the conventional directories are ALSO swept, even unreferenced
        conv = discover(repo)
        assert any(p.name == "x.json" for p in conv)

        # a config file this repo does not have yields nothing, never a fabricated value
        empty_repo = Path(td) / "nope"
        assert discover(empty_repo) == []
        assert declared_fields(empty_repo) == ({}, {})
    print("harness.alignment.configs self-check ok")


if __name__ == "__main__":
    _self_check()
