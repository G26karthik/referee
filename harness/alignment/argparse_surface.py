"""What CLI flags a Python entrypoint actually declares, read from its own source.

`python -m harness.alignment.argparse_surface` runs the self-check.

**Why this exists rather than reusing `experiment_id._SEED_FLAG`.** That regex finds ANY
token shaped like `--*seed*`, which is enough to know a seed flag exists but nothing about
what else the script accepts. Disambiguating APT's 84 candidate scripts needs the SAME
kind of question asked of every field a paper's table can vary by — sparsity, model,
dataset — and asking it once per field with a bespoke regex is how the field vocabulary in
`experiment_id.resolve_configuration` stayed at exactly three patterns. Parsing the
`argparse.add_argument(...)` calls themselves gives every declared flag in one pass,
independent of which fields anyone happened to think to look for.

**Static, not executed.** This module never imports or runs the target file — it pattern-
matches source text. `alignment.trial` is the OPTIONAL, GATED module that confirms a
surface read here against the real program; nothing here executes third-party code, so
nothing here needs the isolation boundary Step 0 requires for that.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..artifacts import ArgSpec

# `add_argument("--flag", ...)` or `add_argument('-f', '--flag', ...)`. The flag captured
# is the LAST long-form token (`--foo-bar`), which is the one a real invocation writes;
# a short form alone (`-f` with no long form) is also accepted, since some scripts declare
# only that.
_ADD_ARGUMENT = re.compile(
    r"add_argument\(\s*((?:['\"]-[^'\"]*['\"]\s*,\s*)*['\"](--?[\w-]+)['\"])"
    r"((?:[^()]|\([^()]*\))*?)\)", re.S)
_LONG_FLAG = re.compile(r"['\"](--[\w-]+)['\"]")
_DEFAULT = re.compile(r"default\s*=\s*([^,)]+)")
_CHOICES = re.compile(r"choices\s*=\s*\[([^\]]*)\]")
_TYPE = re.compile(r"\btype\s*=\s*(\w+)")
_REQUIRED = re.compile(r"required\s*=\s*True")
_ACTION_STORE_TRUE = re.compile(r"action\s*=\s*['\"]store_true['\"]")


def parse_source(text: str, source_name: str = "") -> list[ArgSpec]:
    """Every `add_argument` call in `text`, in the order the source declares them.

    Pure over the text; no file access. `source_name` is echoed into `source_ref` with a
    1-indexed line number, so a matched flag can be re-checked the way every other
    evidence pointer in this harness can.
    """
    out: list[ArgSpec] = []
    for m in _ADD_ARGUMENT.finditer(text):
        names, first_flag, body = m.group(1), m.group(2), m.group(3)
        long_flags = _LONG_FLAG.findall(names)
        flag = long_flags[-1] if long_flags else first_flag
        line = text[:m.start()].count("\n") + 1
        spec = ArgSpec(flag=flag, source_ref=f"{source_name}:{line}" if source_name else str(line))
        if d := _DEFAULT.search(body):
            spec.default = d.group(1).strip().strip("'\"")
        if c := _CHOICES.search(body):
            spec.choices = [c.strip().strip("'\"") for c in c.group(1).split(",") if c.strip()]
        if t := _TYPE.search(body):
            spec.type = t.group(1)
        spec.required = bool(_REQUIRED.search(body))
        spec.is_flag = bool(_ACTION_STORE_TRUE.search(body))
        out.append(spec)
    return out


def parse_file(path: Path) -> list[ArgSpec]:
    """`parse_source` over a file on disk, or `[]` when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_source(text, source_name=str(path))


def by_flag(specs: list[ArgSpec]) -> dict[str, ArgSpec]:
    """The specs keyed by flag, first declaration winning on a duplicate.

    A script that calls `add_argument("--seed")` twice (once in a shared parser, once in
    an override) is choosing its FIRST declaration as the one that matters to a reader
    following the source top to bottom.
    """
    out: dict[str, ArgSpec] = {}
    for s in specs:
        out.setdefault(s.flag, s)
    return out


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    src = '''
import argparse
p = argparse.ArgumentParser()
p.add_argument("--sparsity", type=float, default=0.5, choices=[0.2, 0.5, 0.8],
               help="pruning ratio")
p.add_argument("--base_model", type=str, required=True)
p.add_argument("-s", "--seed", type=int, default=42)
p.add_argument("--verbose", action="store_true")
'''
    specs = parse_source(src, source_name="eval.py")
    assert len(specs) == 4
    by = by_flag(specs)
    assert set(by) == {"--sparsity", "--base_model", "--seed", "--verbose"}

    sparsity = by["--sparsity"]
    assert sparsity.type == "float" and sparsity.default == "0.5"
    assert sparsity.choices == ["0.2", "0.5", "0.8"]
    assert sparsity.source_ref == "eval.py:4"

    assert by["--base_model"].required is True
    assert by["--seed"].flag == "--seed", "the LONG form is kept, not the short -s"
    assert by["--verbose"].is_flag is True and by["--verbose"].default == ""

    # a duplicate declaration keeps the FIRST
    dup = parse_source(
        'p.add_argument("--x", default=1)\np.add_argument("--x", default=2)\n')
    assert by_flag(dup)["--x"].default == "1"

    # nothing crashes on a file that cannot be read, or one with no add_argument at all
    assert parse_file(Path("this/path/does/not/exist.py")) == []
    assert parse_source("print('hello')") == []

    # every field is a plain string/bool/list[str] — no nested object a caller could
    # mistake for something this module verified by running the program
    for s in specs:
        assert isinstance(s.flag, str) and isinstance(s.choices, list)
        assert isinstance(s.required, bool) and isinstance(s.is_flag, bool)
    print("harness.alignment.argparse_surface self-check ok")


if __name__ == "__main__":
    _self_check()
