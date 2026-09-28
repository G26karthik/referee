"""S3 identity resolution — WHICH experiment, WHICH quantity, WHICH configuration.

`ExecCapability` asks whether this machine can run the repository; it does not ask
whether the repository is being asked the right question. A capable run of the wrong
program is more dangerous than a crash: a crash is visible, a confident irrelevant number
is not (e.g. an entrypoint heuristic selecting a FLOPs/latency profiler and reconciling
its output against a cell that holds a baseline's relative training memory).

Three identities, each categorical, with deliberately no confidence score: a threshold
would silently convert "we are unsure which experiment this is" into an execution
authorization. Every established identity carries `IdentityEvidence` with a `source_ref`
(a file/line in the checkout, or a cell address) so the mapping can be re-checked.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path

from .decide import abstract_section_idx
from .schema import (CandidateCommand, ConfigurationIdentity, ExperimentIdentity,
                     IdentityEvidence, MetricIdentity, PaperDoc, Table)

# Quantity vocabulary. Deliberately small and explicit: the point is to REFUSE a
# comparison between different quantities, which needs only enough resolution to tell
# them apart.
_QUANTITY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flops",    ("flops", "macs", "gflops", "gmacs")),
    ("memory",   ("mem", "memory", "vram", "footprint")),
    ("latency",  ("latency", "time", "runtime", "speed", "throughput", "ms", "tta")),
    ("params",   ("param", "parameters", "size")),
    ("loss",     ("loss", "perplexity", "ppl", "nll")),
    ("accuracy", ("acc", "accuracy", "score", "f1", "em", "bleu", "rouge", "map", "miou",
                  "ap", "auc", "top1", "top-1", "psnr", "fpd", "pass@", "success")),
)
# Output keys a repository plausibly emits, mapped to the quantity they carry. Used to
# label a candidate command by what it actually writes, not by what it is called.
_OUTPUT_QUANTITY: tuple[tuple[str, str], ...] = (
    ("model_flops", "flops"), ("model_macs", "flops"), ("flops", "flops"), ("macs", "flops"),
    ("peak_memory", "memory"), ("memory", "memory"), ("mem", "memory"),
    ("latency", "latency"), ("throughput", "latency"), ("runtime", "latency"),
    ("eval_loss", "loss"), ("loss", "loss"), ("perplexity", "loss"),
    ("eval_accuracy", "accuracy"), ("accuracy", "accuracy"), ("eval_f1", "accuracy"),
    ("f1", "accuracy"), ("exact_match", "accuracy"),
    # A COUNT. Reachable only from the prose path below, because `_quantity_of` — which is
    # what a cell's quantity comes from — never returns "count": a table column headed
    # "N" is not evidence that the column reports the same population a sentence counts.
    ("n_cases", "count"), ("num_cases", "count"), ("n_examples", "count"),
    ("num_examples", "count"), ("n_samples", "count"), ("num_samples", "count"),
    ("n_items", "count"), ("total_cases", "count"), ("n_total", "count"),
    ("count", "count"), ("total", "count"),
)

# What a PROSE span counts, when it states a composition. Deliberately a separate table
# from `_QUANTITY_WORDS`: a sentence saying "58 topics x 5 templates x 10 instances =
# 2,900 test cases" names a population, and a population is a different kind of quantity
# from an accuracy. Keeping them apart means adding this cannot change how any cell is
# read, which is the property that let it be added at all.
_COUNT_WORDS = ("case", "cases", "instance", "instances", "example", "examples",
                "sample", "samples", "question", "questions", "problem", "problems",
                "item", "items", "pair", "pairs", "prompt", "prompts", "task", "tasks",
                "scenario", "scenarios", "record", "records", "test", "tests")


def prose_quantity(text: str) -> str:
    """'count' when a prose span states how many of something there are, else ''. Narrow
    by construction: a composition claim (the one prose shape `harness.claims
    .parse_quantity` admits) can only carry a count. An accuracy stated in prose has no
    column header, basis or baseline row, and is never read here."""
    low = (text or "").lower()
    return "count" if any(re.search(rf"\b{re.escape(w)}\b", low) for w in _COUNT_WORDS) else ""
# ANY info string, not only the four shell-ish ones: a README opening a ```python block
# before its ```-only command block would otherwise shift every fence boundary by one,
# hiding the command block between fences. A non-shell block is harmless since its lines
# still have to look like a command to be picked up.
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", re.S)
# Launchers a repository puts IN FRONT of the command it is actually advertising
# ("srun python3 main.py", "accelerate launch train.py") — recognised as discovery, not
# invention: the argv kept is still verbatim what the repository wrote, minus the
# scheduler/multi-GPU wrapper this harness cannot honour on a single local machine.
_LAUNCHERS = ("srun", "torchrun", "accelerate launch", "deepspeed", "mpirun", "horovodrun")
_CMD_LINE = re.compile(r"^\s*(?:\$\s*)?((?:bash|sh|python|python3|make)\s+\S.*)$", re.M)
_LAUNCHED = re.compile(
    r"^\s*(?:\$\s*)?(?:" + "|".join(re.escape(l) for l in _LAUNCHERS) + r")\s+(.*)$", re.M)
# The launcher's own flags sit between it and the program; the program is the first token
# that names a file the repository could run.
_PROGRAM = re.compile(r"(?:^|\s)((?:python3?|bash|sh)\s+\S+\.(?:py|sh)|\S+\.(?:py|sh))(?=\s|$)")
# An environment runner executes the rest of the line unchanged inside the project's own
# environment, so — unlike a launcher — the command after it is kept whole, flags included.
_ENV_RUNNER = re.compile(
    r"^\s*(?:\$\s*)?(?:uv\s+run|poetry\s+run|pipenv\s+run|pdm\s+run|hatch\s+run|pixi\s+run|"
    r"conda\s+run(?:\s+-n\s+\S+)?)\s+(.*)$")
_FOR_LOOP = re.compile(r"^for\s+(\w+)\s+in\s+(.+?)\s*$")
_PLACEHOLDER = re.compile(r"^<([^<>]+)>$")


def _argv(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


def _fenced_commands(block: str) -> list[tuple[int, list[str], dict[str, list[str]]]]:
    """(line offset, argv, slots) for each command a fenced block advertises. A backslash
    continuation is one command; a `for v in a b c; do ... done` loop documents the values
    its `$v` takes; a `<a|b|c>` token documents its alternatives and a bare `<name>` none.
    A slot is only a hole with its DOCUMENTED values — it is filled later, from the cited
    cell's own text, or the command is refused."""
    lines, starts, buf, first = [], [], "", None
    for i, raw in enumerate(block.splitlines()):
        first = i if first is None else first
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        lines.append(buf + raw)
        starts.append(first)
        buf, first = "", None
    out: list[tuple[int, list[str], dict[str, list[str]]]] = []
    loops: list[tuple[str, list[str]]] = []          # active, innermost last
    pending: tuple[str, list[str]] | None = None      # `for ...` seen, `do` not yet
    for i, line in zip(starts, lines):
        # One line may hold several statements: "for d in a b; do python x.py $d; done".
        for stmt in (s.strip() for s in line.split(";")):
            if m := _FOR_LOOP.match(stmt):
                pending = (m.group(1), _argv(m.group(2)))
                continue
            if m := re.match(r"^do\b\s*(.*)$", stmt):
                if pending is not None:
                    loops.append(pending)
                pending, stmt = None, m.group(1)
            if re.match(r"^done\b", stmt):
                if loops:
                    loops.pop()
                continue
            advertised = _advertised_line(stmt)
            if not advertised:
                continue
            argv, slots = [], {}
            for token in _argv(advertised):
                loop = next((lp for lp in reversed(loops)
                             if token in (f"${lp[0]}", f"${{{lp[0]}}}")), None)
                if loop is not None:
                    slots[token] = list(loop[1])
                elif p := _PLACEHOLDER.match(token):
                    slots[token] = ([v for v in p.group(1).split("|") if v]
                                    if "|" in p.group(1) else [])
                argv.append(token)
            out.append((i, argv, slots))
    return out


def _advertised_line(raw: str) -> str:
    """The command a line advertises, or '' — direct form first, then launcher-prefixed.
    Returns the command VERBATIM where already plain, or the program invocation a
    launcher was wrapping. Nothing is synthesised."""
    if m := _ENV_RUNNER.match(raw):
        raw = m.group(1)           # "uv run python x.py --a b" runs "python x.py --a b"
    if m := _CMD_LINE.match(raw):
        return m.group(1)
    if m := _LAUNCHED.match(raw):
        if p := _PROGRAM.search(m.group(1)):
            prog = p.group(1)
            return prog if prog.split()[0] in ("python", "python3", "bash", "sh") else f"python {prog}"
    return ""
_SEED_FLAG = re.compile(r"--([a-z_]*seed[a-z_]*)(?:[=\s]+(\S+))?", re.I)

# A README that names its programs in prose -- "to train, execute `main.py`", "`/eval_{a/b}
# .py`: the evaluation code for our results" -- documents them as surely as a fenced command
# block does. A brace lists alternatives, each matched exactly; a glob is matched against the
# files that EXIST. A README typo names nothing. Prose names rank below spelled-out commands.
_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_PROGRAM_NAME = re.compile(r"\.?/?[\w.\-/{}*,]+\.(?:py|sh)")
_NOT_SOURCE = {".git", "env", ".env", ".venv", "venv", "site-packages", "node_modules",
               "__pycache__"}


_README_LINK = re.compile(r"\]\(\s*\.?/?([\w.\-/]+?)/?(?:README\.md)?\s*\)")


def _documented_readmes(repo: Path) -> list[Path]:
    """The root README, then every README of a directory it links to ("the harness that
    produced the paper's numbers is in [`benchmarks/`](benchmarks/)"): one hop only."""
    root = repo / "README.md"
    if not root.is_file():
        return []
    out = [root]
    text = root.read_text(encoding="utf-8", errors="replace")
    for m in _README_LINK.finditer(text):
        nested = repo / m.group(1) / "README.md"
        if nested.is_file() and nested not in out and repo in nested.resolve().parents:
            out.append(nested)
    return out[:6]  # ponytail: a root README linking more sub-guides than this is an index


def _program_files(repo: Path, limit: int = 5000) -> list[str]:
    out: list[str] = []
    for path in repo.rglob("*"):
        if len(out) >= limit:  # ponytail: a README names programs near the top of a repo
            break
        if path.suffix in (".py", ".sh") and not _NOT_SOURCE & set(path.relative_to(repo).parts):
            out.append(path.relative_to(repo).as_posix())
    return sorted(out)


def _named_programs(repo: Path, text: str, limit: int = 60) -> list[tuple[int, list[str]]]:
    """(README line, argv) for every program the README's prose names by an inline code
    span, and every command an inline span spells out ("`python main.py --cfg a.yaml`")."""
    blanked = _FENCE.sub(lambda b: "\n" * b.group(0).count("\n"), text)
    files: list[str] | None = None
    out: list[tuple[int, list[str]]] = []
    for m in _CODE_SPAN.finditer(blanked):
        span, line = m.group(1).strip(), blanked[:m.start()].count("\n") + 1
        if advertised := _advertised_line(span):
            out.append((line, _argv(advertised)))
            continue
        if not _PROGRAM_NAME.fullmatch(span):
            continue
        # A glob with no name of its own ("all `*.py` files are formatted") names nothing.
        if len(re.sub(r"[*?/{}.,]|\.(?:py|sh)$", "", span)) < 3:
            continue
        files = _program_files(repo) if files is None else files
        for pattern in _expand_braces(span.lstrip("./")):
            for rel in files:
                if fnmatch.fnmatchcase(rel, pattern) or (
                        "/" not in pattern
                        and fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)):
                    out.append((line, ["bash" if rel.endswith(".sh") else "python", rel]))
    return out[:limit]  # ponytail: a README naming more programs than this is a package index


def _expand_braces(pattern: str, limit: int = 64) -> list[str]:
    """`calc_{gt/es}{A,B}.py` -> the four names it lists, exactly as spelled."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for alt in re.split(r"[/,|]", m.group(1)):
        out += _expand_braces(pattern[:m.start()] + alt.strip() + pattern[m.end():], limit)
    return out[:limit]


# A column of ratios always carries its own normaliser: some row reads exactly 100%.
_HUNDRED = re.compile(r"^\s*100(?:\.0+)?\s*%")


def _quantity_of(text: str) -> str:
    """The semantic quantity a header/caption/key names, or '' when it names none. Whole
    words (a plural allowed): "MSE" is not "ms", "embedding" is not "em". A time unit counts
    only as a unit, "(ms)", never the dataset "MS MARCO"."""
    low = (text or "").lower()
    for quantity, words in _QUANTITY_WORDS:
        if any(re.search(rf"\b{re.escape(w)}" + (r"(?:e?s)?(?!\w)" if w[-1].isalnum() else ""), low)
               for w in words if w != "ms"):
            return quantity
        if quantity == "latency" and re.search(r"\(\s*(?:ms|s|sec|min|h)\s*\)|\bms/", low):
            return quantity
    return ""


def cell_quantity(table: Table, col: int) -> str:
    """The quantity ONE column reports: its header's, else the caption's metric when that
    is the table's single metric (`paper.metric_name`'s rule) — never the caption's first
    quantity word applied to every column. A header naming a metric this vocabulary does
    not know ("ECE", "Top-5") reports that metric, not the caption's."""
    from .paper import _METRIC_WORD, column_header, metric_name  # deferred: paper imports this
    header = column_header(table, col)
    if q := _quantity_of(header):
        return q
    if _METRIC_WORD.search(header):
        return ""
    return _quantity_of(metric_name(table, col))


def cell_basis(table: Table, col: int) -> tuple[str, str]:
    """Is this column absolute, or normalised to a baseline row? Returns (basis, unit).
    Structural rather than lexical: a column of ratios contains its own normaliser, a row
    reading exactly 100% — far more reliable than hunting for "relative" in a caption."""
    values = [table.cell(r, col) for r in range(len(table.rows))]
    if any(_HUNDRED.match(v or "") for v in values):
        return "relative_to_baseline", "%"
    if any("%" in (v or "") for v in values):
        return "absolute", "%"
    return "absolute", ""


def _header_for(table: Table, col: int) -> str:
    from .paper import column_header  # deferred: paper imports decide, which is heavier
    return column_header(table, col) or (table.caption or "")


_GREEK = {"τ": "tau", "ρ": "rho", "σ": "sigma", "μ": "mu", "α": "alpha", "β": "beta",
          "λ": "lambda", "ε": "epsilon", "δ": "delta", "κ": "kappa"}


def _key(text: str) -> str:
    """A name compared as a name: case, punctuation, spacing and Greek spelling ignored."""
    low = (text or "").lower()
    for glyph, name in _GREEK.items():
        low = low.replace(glyph, name)
    return re.sub(r"[^a-z0-9]", "", low)


def _metric_name(table: Table, col: int) -> str:
    from .paper import metric_name  # deferred: paper imports decide, which is heavier
    return metric_name(table, col)


def _metric_key(table: Table, col: int) -> str:
    return _key(_metric_name(table, col))


# WHOSE CELL. A documented command runs the paper's own method; a label that cites its source
# ("BERT (Devlin et al., 2019)", "LoRA [9]", "[12]") reports someone else's number. A cell
# that is not the paper's own is bound only when the command's own METHOD argument names it
# (`--model bert`) — otherwise which cell the command produces is a guess.
_CITED = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|\bet\s+al\b|"
                    r"\(\s*[A-Z][^()]*?\b(?:19[5-9]\d|20[0-3]\d)[a-z]?\s*\)")
_OURS = re.compile(r"\b(?:ours|proposed|this\s+work)\b", re.I)
_PROPOSE = re.compile(r"\b(?i:we\s+(?:propose|introduce|present|develop|call|term|name)|"
                      r"dubbed|termed|called|named)\b")
# Where the named thing ends: the first clause that compares, qualifies or starts a sentence.
_NP_END = re.compile(r"[.;:]\s|\b(?:which|that|who|outperform\w*|improv\w*|than|over|"
                     r"compar\w*|against|versus|vs\.?|beat\w*|surpass\w*|and\s+show\w*)\b", re.I)
_ACRONYM = re.compile(r"\(\s*([A-Z][A-Za-z0-9\-]*[A-Z0-9][A-Za-z0-9\-]*)\s*\)")
# A name the paper gives opens its phrase: name-shaped (FooNet, GPT-4), or a capitalised
# word standing alone as an appositive ("Kite, a fast optimizer"). "Bayesian optimization"
# names a field; "GNN-based" describes.
_NAME_SHAPED = re.compile(r"[A-Z][A-Za-z0-9]*[A-Z0-9][\w\-]*")
_DESCRIPTOR = re.compile(r"-(?:based|like|style|driven|aware|guided|free|agnostic)$", re.I)
_ARTICLES = {"a", "an", "the", "our", "new", "novel"}


def own_names(doc: PaperDoc) -> set[str]:
    """The names the paper gives its OWN method, where it gives them: a title led by one
    ("FooNet: ..."), and — in the front matter through the abstract — the phrase right after
    "we propose/introduce ..." up to its first comparison or clause: an acronym defined
    there ("GRAdient-based Causal tree Ensembles (GRACE)"), else its first given name
    ("Kite, a fast optimizer"; "a GNN-based model, FooNet"). "FooNet, which improves on
    Low-Rank Adaptation (LoRA)" stops at "which": LoRA is the comparison, not theirs."""
    idx = abstract_section_idx(doc)
    upto = [s for s in doc.sections if idx < 0 or s.section_idx <= idx][:max(3, idx + 1)]
    front = re.sub(r"(\w)-\s+(\w)", r"\1\2", " ".join(s.text or "" for s in upto))[:8000]
    names = set()
    if head := re.match(r"\s*([A-Z][\w\-]{2,})\s*:", doc.title or ""):
        names.add(head.group(1))
    for m in _PROPOSE.finditer(front):
        rest = front[m.end():m.end() + 300]
        phrase = rest[:end.start()] if (end := _NP_END.search(rest)) else rest
        for segment in phrase.split(",")[:2]:        # "a GNN-based model, FooNet"
            if acronyms := _ACRONYM.findall(segment):
                names |= set(acronyms)
                break
            words = [w.strip("()\"'") for w in segment.split()]
            while words and words[0].lower() in _ARTICLES:
                words.pop(0)
            first = words[0] if words else ""
            if (len(first) >= 3 and first[0].isupper() and not _DESCRIPTOR.search(first)
                    and (_NAME_SHAPED.fullmatch(first) or len(words) == 1)):
                names.add(first)
                break
    return {_key(n) for n in names if len(_key(n)) >= 3}


def own_method(doc: PaperDoc, label: str) -> bool:
    """Does a table label name the paper's OWN method? Never when it carries a citation."""
    label = (label or "").strip()
    if not label or _CITED.search(label):
        return False
    if _OURS.search(label):
        return True
    names = own_names(doc)
    whole = re.sub(r"\([^)]*\)|[*†‡§]", "", label).strip()
    return _key(re.split(r"[\s_/\-(]+", whole)[0]) in names or _key(whole) in names


# A flag that SELECTS the method a run executes; a dataset flag never does.
_METHOD_FLAG = re.compile(r"^--?(?:model|method|arch(?:itecture)?|algo(?:rithm)?|baseline|variant|"
                          r"net(?:work)?|approach|estimator|learner|backbone)s?(?:[_-]?(?:name|type))?$",
                          re.I)


def cell_method(doc: PaperDoc, table: Table, row: int, col: int) -> tuple[list[str], bool, str]:
    """(labels, own, refusal): the label(s) naming the method a cell reports, whether that
    method is the paper's own, and why nothing can be said. The table's own layout decides
    the axis: if some ROW names the paper's method the methods are rows; else if some column
    header does they are columns; else either label may name it and only a documented method
    argument can select it."""
    from .paper import _METRIC_WORD, column_header  # deferred: paper imports this module
    row_label = (table.cell(row, 0) or "").strip()
    head = column_header(table, col)
    for label in (row_label, head):
        if label and _CITED.search(label):
            return [label], False, (f"the cell reports {label!r}, which carries a citation: a "
                                    f"baseline the authors cite, not a result their code produces")
    width = max((len(r) for r in table.rows), default=0)
    if any(own_method(doc, (table.cell(r, 0) or "")) for r in range(len(table.rows))):
        return [row_label], own_method(doc, row_label), ""
    if any(own_method(doc, column_header(table, c)) for c in range(1, width)):
        return [head], own_method(doc, head), ""
    if not re.search(r"[^\W\d_]{2,}", row_label):     # a sample size, a dimension
        if not head:
            return [], False, ("the cited row is labelled by a number and extraction recovered "
                               "no header for its column, so which method the cell reports is unknown")
        if _METRIC_WORD.search(head) or _quantity_of(head):
            return [], True, ""                      # "n | RMSE": a sweep of one method
        return [head], False, ""
    return [row_label, head], False, ""


def selects(cmd: CandidateCommand, labels: list[str]) -> str:
    """The documented METHOD argument value that names one of `labels`, or ''."""
    words = {w.lower() for label in labels for w in re.split(r"[\s,;:()\[\]]+", label) if w}
    for value in cmd.bound_slots.values():
        at = cmd.argv.index(value) if value in cmd.argv else -1
        if at > 0 and _METHOD_FLAG.match(cmd.argv[at - 1]) and value.lower() in words:
            return value
    return ""


def harness_seed_flag(cmd: CandidateCommand | None) -> str:
    """The seed flag the harness may append to a documented command, or '' when it may not:
    the program takes none, the README pins one ("--seed 42", "--seed=42") — the documented
    protocol — or the command is a shell/make wrapper, which need not forward argv to the
    program whose flag was read."""
    if cmd is None or not cmd.seed_flag:
        return ""
    argv = cmd.argv
    if any(t == cmd.seed_flag or t.startswith(cmd.seed_flag + "=") for t in argv):
        return ""
    program = next((t for t in argv if t.endswith((".py", ".sh")) or t in ("make", "-m")), "")
    return "" if program.endswith(".sh") or program == "make" else cmd.seed_flag


def _prefer_fenced(fitting: list[CandidateCommand]) -> list[CandidateCommand]:
    """A command the README spells out outranks a program its prose merely names: prose
    names are consulted only when no spelled-out command fits, so they never turn a
    unique binding into an ambiguous one."""
    return [c for c in fitting if c.source != "readme_named"] or fitting


def cell_context(table: Table, row: int, col: int) -> str:
    """What the paper itself says a cell is about: its row label, its column header, the
    table's header row and the caption. Slot values and configuration are read from this
    text only — never from the harness's idea of what the experiment probably was."""
    from .paper import column_header
    caption = re.sub(r"^\s*Table\s+\S+[.:]?\s*", "", table.caption or "", flags=re.I)
    return " ".join([table.cell(row, 0) or "", column_header(table, col), caption])


def instantiate(cmd: CandidateCommand, context: str) -> tuple[CandidateCommand | None, str]:
    """The documented command with every slot filled by the ONE documented value the cited
    cell names, or (None, why). A free placeholder, or a slot the cell names zero or several
    values for, is refused: choosing would be the harness inventing the experiment."""
    # Whole tokens of the cell's own text only: "Fashion-MNIST" does not name "mnist", and
    # "trained" does not name "train".
    words = {w.lower() for w in re.split(r"[\s,;:()\[\]]+", context) if w}
    bound: dict[str, str] = {}
    for token, values in cmd.slots.items():
        flag = cmd.argv[cmd.argv.index(token) - 1] if cmd.argv.index(token) > 0 else ""
        if "seed" in flag.lower() or "seed" in token.lower():
            return None, (f"slot {token} is a seed; which seed a printed aggregate came from is "
                          f"not something the paper's text can name")
        if not values:
            return None, f"placeholder {token} has no documented values to choose from"
        named = [v for v in values
                 if re.search(r"[^\W\d_]", v) and v.lower() in words]   # never a bare number
        if len(named) != 1:
            return None, (f"slot {token} documents {len(values)} value(s) and the cited cell names "
                          f"{len(named)} of them ({', '.join(named[:4]) or 'none'})")
        bound[token] = named[0]
    argv = [bound.get(t, t) for t in cmd.argv]
    # A hole inside a token counts too: `configs/<dataset>.yaml`, `--data=<path>`, `{task}`,
    # and a README's own `{seed}`, which pins nothing (the harness appends its seed flag).
    if hole := next((t for t in argv if re.search(r"\$\{?\w|<[^<>\s]+>|\{\w+\}", t)
                     or _PLACEHOLDER.match(t)), ""):
        return None, f"the documented command still has an unfilled hole {hole!r}"
    if not bound:
        return cmd, ""
    filled = cmd.model_copy(deep=True)
    filled.argv = argv
    filled.bound_slots, filled.slots = bound, {}
    return filled, ""


# === Candidate discovery — from what the repository advertises, never from filenames ===
def harvest_candidates(repo: Path, limit: int = 400) -> list[CandidateCommand]:
    """Every command the repository tells a reader to run, with its source location.
    Filenames are not evidence: `evaluate.py` existing says nothing about what it does; a
    README line saying "to finetune X, run Y" does."""
    out: list[CandidateCommand] = []

    for readme in _documented_readmes(repo):
        rel = readme.relative_to(repo).as_posix()
        text = readme.read_text(encoding="utf-8", errors="replace")
        for block in _FENCE.finditer(text):
            line_no = text[:block.start()].count("\n") + 1
            for i, argv, slots in _fenced_commands(block.group(1)):
                out.append(CandidateCommand(
                    argv=argv, slots=slots, source="readme",
                    source_ref=f"{rel}:{line_no + i + 1}"))
        for line_no, argv in _named_programs(repo, text):
            out.append(CandidateCommand(argv=argv, source="readme_named",
                                        source_ref=f"{rel}:{line_no}"))

    run_sh = repo / "run.sh"
    if run_sh.is_file():
        for i, raw in enumerate(run_sh.read_text(encoding="utf-8", errors="replace").splitlines()):
            if advertised := _advertised_line(raw):
                out.append(CandidateCommand(argv=advertised.split(), source="run_script",
                                            source_ref=f"run.sh:{i + 1}"))

    for script in sorted((repo / "scripts").rglob("*.sh"))[:limit]:
        rel = script.relative_to(repo).as_posix()
        out.append(CandidateCommand(argv=["bash", rel], source="scripts_dir",
                                    source_ref=f"{rel}:1"))

    makefile = repo / "Makefile"
    if makefile.is_file():
        for i, raw in enumerate(makefile.read_text(encoding="utf-8", errors="replace").splitlines()):
            if m := re.match(r"^([A-Za-z0-9_.\-]+):\s*(?:#.*)?$", raw):
                out.append(CandidateCommand(argv=["make", m.group(1)], source="makefile",
                                            source_ref=f"Makefile:{i + 1}"))

    seen, unique = set(), []
    for c in out:
        key = " ".join(c.argv)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique[:limit]


def target_file(repo: Path, cmd: CandidateCommand) -> Path | None:
    """The file on disk a candidate command actually invokes, or None. Public because
    `harness.alignment` needs the same lookup — a script's declared configuration lives
    in the FILE this resolves to, not in `cmd.argv`."""
    argv = cmd.argv[1:]
    if len(argv) >= 2 and argv[0] == "-m":          # `python -m pkg.mod` runs pkg/mod.py
        module = repo / Path(*argv[1].split("."))
        for candidate in (module.with_suffix(".py"), module / "__main__.py"):
            if candidate.is_file():
                return candidate
        return None
    for token in argv:
        candidate = repo / token
        # A config or data file an argument names is read BY the program, not the program.
        if candidate.is_file() and candidate.suffix not in (
                ".yaml", ".yml", ".json", ".toml", ".txt", ".csv", ".cfg", ".ini", ".md"):
            return candidate
    return None


_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w, ]+)|import\s+([\w.]+))", re.M)


def _local_imports(repo: Path, program: Path, limit: int = 12) -> list[Path]:
    """Repository files `program` imports directly (absolute from the repo root, or relative
    to its own package), never installed packages."""
    try:
        text = program.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Path] = []
    for m in _IMPORT.finditer(text):
        module = m.group(1) if m.group(1) is not None else m.group(3)
        dots = len(module) - len(module.lstrip("."))
        base = program.parent
        for _ in range(max(0, dots - 1)):
            base = base.parent
        roots = [base] if dots else [repo, repo / "src"]
        names = [module.lstrip(".")] if module.lstrip(".") else []
        if m.group(2) and dots:
            names += [f"{module.lstrip('.')}.{n.strip()}".lstrip(".") for n in m.group(2).split(",")]
        for root in roots:
            for name in names:
                p = root / Path(*name.split("."))
                for cand in (p.with_suffix(".py"), p / "__init__.py"):
                    if cand.is_file() and cand not in out and cand != program:
                        out.append(cand)
    return out[:limit]  # ponytail: one hop of a program's own helpers, not the package


def describe_command(repo: Path, cmd: CandidateCommand, depth: int = 2) -> CandidateCommand:
    """Label a command by the output keys its code actually writes. Follows one hop from
    a shell script into the python it invokes, since a script's own text says little."""
    path = target_file(repo, cmd)
    if path is None:
        return cmd
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return cmd

    if m := _SEED_FLAG.search(text):
        cmd.seed_flag = f"--{m.group(1)}"
        cmd.seed_values = [v for v in {mm.group(2) for mm in _SEED_FLAG.finditer(text) if mm.group(2)}]

    emits: list[str] = []
    for key, _ in _OUTPUT_QUANTITY:
        if re.search(rf"['\"]{re.escape(key)}['\"]", text):
            emits.append(key)
    # The keys a program writes are often spelled in the repository modules it imports (a
    # metrics helper), so those are read too — one hop, repository files only.
    sources = [text] + [p.read_text(encoding="utf-8", errors="replace")
                        for p in _local_imports(repo, path)] if path.suffix == ".py" else [text]
    # Only identifier-shaped literals ("PEHE", "rel_crps", "pass@1") can be output keys; a
    # message, a format string or an escape ('\t') is not one.
    cmd.named_keys = sorted({s for src in sources
                             for s in (lit.strip("'\"") for lit in _STRING_LITERAL.findall(src))
                             if re.fullmatch(r"[A-Za-z][\w.@\-]{1,39}", s)})[:600]
    if depth > 0 and path.suffix == ".sh":
        for m in re.finditer(r"\b(?:python3?|bash)\s+(\S+\.(?:py|sh))", text):
            nested = repo / m.group(1)
            if nested.is_file():
                inner = describe_command(repo, CandidateCommand(argv=["python", m.group(1)]),
                                         depth - 1)
                emits += inner.emits
                cmd.named_keys = sorted(set(cmd.named_keys) | set(inner.named_keys))
                cmd.seed_flag = cmd.seed_flag or inner.seed_flag
                cmd.seed_values = cmd.seed_values or inner.seed_values
    cmd.emits = sorted(set(emits))
    quantities = sorted({q for k, q in _OUTPUT_QUANTITY if k in cmd.emits})
    cmd.label = ", ".join(quantities) or "unknown"

    # Two additive passes over the SAME candidate; import deferred because
    # `harness.alignment` reads `target_file` from this module, so importing it at module
    # load time would be circular.
    from .alignment import candidates as alignment_candidates
    from .alignment import evaluator as alignment_evaluator
    alignment_evaluator.corroborate(repo, cmd)
    alignment_candidates.declared_configuration(repo, cmd)
    return cmd


_STRING_LITERAL = re.compile(r"'''.*?'''|\"\"\".*?\"\"\"|'[^'\n]*'|\"[^\"\n]*\"", re.S)


def repo_implements(repo: Path, method: str) -> bool:
    """Does the repository CONTAIN an implementation of the named method? Code and
    scripts only, string literals stripped before searching: a name occurring only inside
    quoted text (e.g. a matplotlib label, a citation of a baseline) is something the code
    TALKS ABOUT; a name in a path or identifier is something the code IS."""
    token = re.sub(r"[^a-z0-9]", "", (method or "").lower())
    if len(token) < 3:
        return False
    for path in list(repo.rglob("*.py"))[:2000] + list(repo.rglob("*.sh"))[:2000]:
        if token in re.sub(r"[^a-z0-9]", "", path.as_posix().lower()):
            return True
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code_only = _STRING_LITERAL.sub(" ", source)
        if token in re.sub(r"[^a-z0-9]", "", code_only.lower()):
            return True
    return False


def repo_defines(repo: Path, name: str) -> bool:
    """Does the checkout DEFINE `name` — a class, a function, or a module/package directory
    of its own? Imports and string mentions do not count."""
    token = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if len(token) < 3:
        return False
    define = re.compile(r"^\s*(?:class|def)\s+(\w+)", re.M)
    for path in list(repo.rglob("*.py"))[:2000]:
        if _NOT_SOURCE & set(path.relative_to(repo).parts):
            continue
        if any(re.sub(r"[^a-z0-9]", "", part.lower().removesuffix(".py")) == token
               for part in path.relative_to(repo).parts):
            return True
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(re.sub(r"[^a-z0-9]", "", d.lower()) == token for d in define.findall(text)):
            return True
    return False


# === Resolution ========================================================================
def _prose_metric(quote: str, cmd: CandidateCommand | None, ref: str) -> MetricIdentity:
    """Bind an executed COUNT to a prose-stated total, or refuse for a named reason. The
    prose analogue of the cell path below and no weaker: both require a command emitting
    the SAME quantity from the same table of output keys, and refuse identically when
    none does."""
    ident = MetricIdentity(cell_basis="absolute")
    ident.cell_quantity = prose_quantity(quote)
    ident.evidence.append(IdentityEvidence(
        quote=(quote or "")[:160], source_ref=ref,
        note=f"prose quantity '{ident.cell_quantity or 'unknown'}', basis 'absolute'"))
    if not ident.cell_quantity:
        ident.state = "unmapped"
        ident.reason = ("the prose states a total but does not name a population this harness "
                        "can identify, and a quantity that cannot be named cannot be bound to "
                        "an executed output")
        return ident
    if cmd is None:
        ident.state, ident.reason = "no_candidate", "no command was identified to emit a count"
        return ident
    matches = [(k, q) for k, q in _OUTPUT_QUANTITY if k in cmd.emits and q == "count"]
    if not matches:
        ident.state = "no_candidate"
        ident.output_quantity = cmd.label
        ident.reason = (f"the command emits {cmd.label or 'nothing recognisable'} "
                        f"({', '.join(cmd.emits) or 'no known keys'}) and the claim reports a "
                        f"count; these are different quantities")
        return ident
    ident.output_key, ident.output_quantity, ident.output_basis = matches[0][0], "count", "absolute"
    ident.state = "established"
    ident.reason = (f"the claim states an absolute count and the command emits "
                    f"'{ident.output_key}', the same quantity on the same basis")
    ident.evidence.append(IdentityEvidence(
        quote=ident.output_key, source_ref=cmd.source_ref,
        note=f"command emits '{ident.output_key}', quantity 'count'"))
    return ident


def resolve_metric(doc: PaperDoc, table_ref: str, cmd: CandidateCommand | None,
                   claim_ref: str = "", claim_quote: str = "") -> MetricIdentity:
    """Bind the executed output to the cell's quantity AND basis, or refuse."""
    ident = MetricIdentity()
    m = re.fullmatch(r"T(\d+):r(\d+):c(\d+)", (table_ref or "").strip())
    if not m:
        if claim_quote and (claim_ref or "").startswith("P"):
            return _prose_metric(claim_quote, cmd, claim_ref)
        ident.state, ident.reason = "unmapped", "no cell address to bind a metric to"
        return ident
    table = next((t for t in doc.tables if t.table_idx == int(m.group(1))), None)
    if table is None:
        ident.state, ident.reason = "no_candidate", f"table {m.group(1)} is not in the parsed paper"
        return ident

    col = int(m.group(3))
    header = _header_for(table, col)
    ident.cell_quantity = cell_quantity(table, col)
    ident.cell_basis, ident.cell_unit = cell_basis(table, col)
    ident.evidence.append(IdentityEvidence(
        quote=header or (table.caption or "")[:120], source_ref=table_ref,
        note=f"cell quantity '{ident.cell_quantity or 'unknown'}', basis '{ident.cell_basis}'"))

    # By NAME: the paper names the metric ("PEHE", "Kendall's τ") and the invoked program's
    # own source writes a key spelled exactly that. Any quantity, not only the vocabulary
    # above — but only an exact name, never a family resemblance.
    named = _metric_key(table, col)
    by_name = next((k for k in (cmd.named_keys if cmd else []) if named and _key(k) == named), "")
    matched_by_class = bool(cmd and ident.cell_quantity and any(
        q == ident.cell_quantity for k, q in _OUTPUT_QUANTITY if k in cmd.emits))
    # A name never overrides a quantity the cell was already identified by: 'accuracy'
    # written by the program is not a 'Time (s)' column, however the caption reads.
    if by_name and ident.cell_quantity and _quantity_of(by_name) not in ("", ident.cell_quantity):
        by_name = ""
    if by_name and not matched_by_class:
        ident.cell_quantity = ident.cell_quantity or _metric_name(table, col)
        ident.output_key, ident.output_quantity = by_name, _metric_name(table, col)
        ident.evidence.append(IdentityEvidence(
            quote=by_name, source_ref=cmd.source_ref,
            note=f"the invoked program names the key '{by_name}', the metric the paper names"))
        if ident.cell_basis == "relative_to_baseline":
            ident.state = "unsupported"
            ident.reason = ("the cell is normalised to a baseline run and the program reports "
                            f"'{by_name}' on its own scale; one run cannot supply both arms")
            return ident
        ident.output_basis, ident.state = "absolute", "established"
        ident.reason = (f"the cell reports {_metric_name(table, col)!r} and the program writes "
                        f"the identically named key '{by_name}'")
        return ident

    if not ident.cell_quantity:
        ident.state = "unmapped"
        merged = len(re.findall(r"\(\s*[⇓⇑]\s*\)|\bmem\b|\btime\b", header, re.I)) > 1
        ident.reason = (
            f"the column header {header!r} "
            + ("collapses several columns into one during extraction, so which quantity this "
               "cell reports cannot be determined"
               if merged else
               "does not name a quantity this harness can identify")
            + ", and a metric that cannot be named cannot be bound to an executed output")
        return ident
    if cmd is None:
        ident.state, ident.reason = "no_candidate", "no command was identified to emit a metric"
        return ident

    matches = [(k, q) for k, q in _OUTPUT_QUANTITY if k in cmd.emits and q == ident.cell_quantity]
    if not matches:
        ident.state = "no_candidate"
        ident.output_quantity = cmd.label
        ident.reason = (f"the command emits {cmd.label or 'nothing recognisable'} "
                        f"({', '.join(cmd.emits) or 'no known keys'}) and the cell reports "
                        f"{ident.cell_quantity}; these are different quantities")
        return ident

    ident.output_key, ident.output_quantity = matches[0]
    ident.evidence.append(IdentityEvidence(
        quote=ident.output_key, source_ref=cmd.source_ref,
        note=f"command emits '{ident.output_key}', quantity '{ident.output_quantity}'"))

    if ident.cell_basis == "relative_to_baseline":
        ident.output_basis = "absolute"
        ident.requires_arms = ["baseline", "method"]
        ident.state = "unsupported"
        ident.reason = (f"the cell is normalised to a baseline run (its column contains a 100% "
                        f"row), and the command emits an absolute {ident.output_quantity}. "
                        f"Reconciling them needs both arms measured under this harness, which is "
                        f"not something a single run can supply")
        return ident

    ident.output_basis = "absolute"
    ident.state = "established"
    ident.reason = (f"the cell reports absolute {ident.cell_quantity} and the command emits "
                    f"'{ident.output_key}', the same quantity on the same basis")
    return ident


def _prose_experiment(repo: Path, quote: str, ref: str, finding_id: str) -> ExperimentIdentity:
    """Which advertised command produces a prose-stated COUNT — or that none does. The
    same refusal ladder as the cell path (harvest, keep only what emits the right
    quantity, refuse on none or ambiguity), minus the ROW METHOD check: a prose total
    names a population, not a method, so there is no method to look for."""
    ident = ExperimentIdentity(finding_id=finding_id, table_ref=ref)
    if not prose_quantity(quote):
        ident.state, ident.reason = "unmapped", (
            "the prose claim does not name a population, so there is no experiment to "
            "identify for it")
        return ident
    candidates = [describe_command(repo, c) for c in harvest_candidates(repo)
                  if instantiate(c, "")[0] is not None]     # no command with an unfilled hole
    ident.considered = len(candidates)
    fitting = _prefer_fenced([c for c in candidates
                              if any(q == "count" for k, q in _OUTPUT_QUANTITY if k in c.emits)])
    for c in candidates:
        if c not in fitting:
            ident.rejected.append(f"{c.source_ref}: emits {c.label or 'unknown'}")
    if not fitting:
        ident.state = "no_candidate"
        ident.reason = (f"none of the {len(candidates)} advertised command(s) emits a count, so "
                        f"nothing in this checkout produces the total the paper states")
        return ident
    if len({" ".join(c.argv) for c in fitting}) > 1:
        ident.state, ident.command = "ambiguous", None
        ident.reason = (f"{len(fitting)} advertised commands could produce the count; choosing "
                        f"between them would be a guess, so none is chosen "
                        f"({', '.join(c.source_ref for c in fitting[:4])})")
        return ident
    ident.command = fitting[0]
    ident.state = "established"
    ident.evidence.append(IdentityEvidence(
        quote=" ".join(ident.command.argv), source_ref=ident.command.source_ref,
        note="the only advertised command emitting a count"))
    ident.reason = f"one advertised command emits a count: {' '.join(ident.command.argv)}"
    return ident


def resolve_experiment(doc: PaperDoc, repo: Path, table_ref: str,
                       finding_id: str = "", claim_ref: str = "",
                       claim_quote: str = "") -> ExperimentIdentity:
    """Which advertised command produces the cited cell — or that none does."""
    ident = ExperimentIdentity(finding_id=finding_id, table_ref=table_ref)
    m = re.fullmatch(r"T(\d+):r(\d+):c(\d+)", (table_ref or "").strip())
    if not m:
        if claim_quote and (claim_ref or "").startswith("P"):
            return _prose_experiment(repo, claim_quote, claim_ref, finding_id)
        ident.state, ident.reason = "unmapped", "no cell address to identify an experiment for"
        return ident
    table = next((t for t in doc.tables if t.table_idx == int(m.group(1))), None)
    if table is None:
        ident.state, ident.reason = "no_candidate", "the cited table is not in the parsed paper"
        return ident

    row = int(m.group(2))
    ident.row_method = (table.cell(row, 0) or "").strip()
    ident.evidence.append(IdentityEvidence(
        quote=ident.row_method, source_ref=f"T{m.group(1)}:r{row}:c0",
        note="the method whose result this cell reports"))

    # WHICH method the cell reports, and whether it is the paper's own (`cell_method`).
    from .paper import table_role  # deferred: see `_metric_name`
    col = int(m.group(3))
    labels, own, refusal = cell_method(doc, table, row, col)
    if refusal:
        ident.state, ident.reason = "no_candidate", refusal
        return ident
    label = labels[0] if labels else ""
    # A method the paper NAMES as its own must be implemented here (the code IS it, not a
    # string about it); a variant ("FooNet-large") counts when the checkout DEFINES its
    # leading name. A label marked "ours" names no identifier to look for.
    method = re.sub(r"\([^)]*\)|[*†‡§]", "", label).strip(" -_") if own else ""
    lead = re.split(r"[\s\-_/]+", method)[0] if method else ""
    if method and not _OURS.search(label) and not repo_implements(repo, method) and not (
            len(lead) >= 4 and repo_defines(repo, lead)):
        ident.state = "no_candidate"
        ident.reason = (f"the cell reports {label!r}, and no .py or .sh file in this "
                        f"checkout implements it")
        return ident

    context = cell_context(table, row, col)
    candidates = []
    for c in harvest_candidates(repo):
        filled, why = instantiate(c, context)
        if filled is None:
            ident.rejected.append(f"{c.source_ref}: {why}")
            continue
        # A cell that is not the paper's own method is produced only by a command whose own
        # documented METHOD argument names it (`--model {bert,ours}` filled with 'bert'); a
        # dataset argument never selects a method.
        if not own and not selects(filled, labels):
            ident.rejected.append(
                f"{c.source_ref}: takes no documented method argument naming "
                f"{' / '.join(repr(x) for x in labels if x)}, which the paper does not name as "
                f"its own" + (" (a dataset row of a data-statistics table)"
                              if table_role(table) == "data_statistics" else ""))
            continue
        candidates.append(describe_command(repo, filled))
    ident.considered = len(candidates) + len(ident.rejected)
    if not own and not candidates:
        ident.state = "no_candidate"
        ident.reason = (
            f"the cited row names {label!r}, a dataset rather than a method, and no advertised "
            f"program in this checkout is bound to its statistics"
            if table_role(table) == "data_statistics" else
            f"the cell reports {' / '.join(repr(x) for x in labels if x)}, which the paper does "
            f"not name as its own method, and no advertised command takes a documented method "
            f"argument naming it — which cell a command produces would be a guess")
        return ident
    quantity = cell_quantity(table, col)
    named = _metric_key(table, col)
    fitting = _prefer_fenced([c for c in candidates
               if (quantity and any(q == quantity for k, q in _OUTPUT_QUANTITY
                                         if k in c.emits))
               or (named and named in {_key(k) for k in c.named_keys})])
    for c in candidates:
        if c not in fitting:
            ident.rejected.append(f"{c.source_ref}: emits {c.label or 'unknown'}")

    if not fitting:
        ident.state = "no_candidate"
        ident.reason = (f"none of the {ident.considered} advertised command(s) emits "
                        f"{quantity or _metric_name(table, col) or 'the cell’s quantity'}"
                        + ("" if _metric_name(table, col) else
                           " (the paper names no metric for this column, so none can be bound)"))
        return ident
    if len({" ".join(c.argv) for c in fitting}) > 1:
        # Step 7 — before refusing, ask whether the fitting candidates say WHICH
        # configuration each one runs under, and whether the cited row asks for a
        # specific one. Deferred import: see `describe_command`'s note on the cycle.
        from .alignment import configuration as alignment_configuration

        expected, expected_evidence = alignment_configuration.expected_fields(
            table, row, int(m.group(3)))
        narrowed, dropped = ({}, {}) if not expected else \
            alignment_configuration.narrow(fitting, expected)
        if expected and len(narrowed) == 1:
            chosen = narrowed[0]
            fields_str = ", ".join(f"{k}={v}" for k, v in expected.items())
            ident.command = chosen
            ident.state = "established"
            ident.evidence.append(IdentityEvidence(
                quote=" ".join(chosen.argv), source_ref=chosen.source_ref,
                note=f"the only one of {len(fitting)} candidates emitting {quantity} "
                     f"whose declared configuration matches the cited row ({fields_str})"))
            for field, ref in expected_evidence.items():
                ident.evidence.append(IdentityEvidence(
                    quote=f"{field}={expected[field]}", source_ref=ref,
                    note="the cited row's own configuration, matched against the chosen "
                         "command's declared configuration"))
            ident.reason = (
                f"{len(fitting)} advertised commands could produce {quantity}; "
                f"configuration matching narrowed them to one by {fields_str}"
                + (f" (excluded: {'; '.join(dropped.values())})" if dropped else ""))
            return ident

        ident.state = "ambiguous"
        ident.command = None
        narrowing_note = (
            f" Configuration matching narrowed {len(fitting)} to {len(narrowed)} "
            f"({', '.join(f'{k}={v}' for k, v in expected.items())}) but could not reach "
            f"one; {'; '.join(dropped.values()) or 'none excluded'}."
            if expected and len(narrowed) < len(fitting) else "")
        ident.reason = (f"{len(fitting)} advertised commands could produce {quantity}; "
                        f"choosing between them would be a guess, so none is chosen "
                        f"({', '.join(c.source_ref for c in fitting[:4])})."
                        f"{narrowing_note}")
        return ident

    ident.command = fitting[0]
    ident.state = "established"
    ident.evidence.append(IdentityEvidence(
        quote=" ".join(ident.command.argv), source_ref=ident.command.source_ref,
        note=f"the only advertised command emitting {quantity}"))
    ident.reason = (f"one advertised command emits {quantity or _metric_name(table, col)}: "
                    f"{' '.join(ident.command.argv)}")
    return ident


def _prose_configuration(quote: str, cmd: CandidateCommand | None,
                         ref: str) -> ConfigurationIdentity:
    """The configuration a prose-stated COUNT is produced under. The seed-policy question
    that dominates the cell path below has no purchase here: a count has no seed-to-seed
    variance, so there is no protocol to match and `seed_policy_match` stays None rather
    than True. What IS required is the composition itself, already re-verified by
    `harness.claims.parse_quantity`."""
    ident = ConfigurationIdentity()
    if cmd is None or not prose_quantity(quote):
        ident.state = "unmapped"
        ident.reason = "no command, or no population named, to match a configuration against"
        return ident
    ident.matched["quantity"] = "count"
    ident.seed_policy_paper = "a stated total; not a seed-dependent measurement"
    ident.seed_policy_repo = (f"{cmd.seed_flag} accepted" if cmd.seed_flag
                              else "no seed argument found")
    ident.seed_policy_match = None
    ident.evidence.append(IdentityEvidence(
        quote=(quote or "")[:160], source_ref=ref,
        note="the composition the paper printed is the configuration under test"))
    ident.state = "established"
    ident.reason = ("the claim states how many items a generator produces; the configuration "
                    "is the composition the paper itself printed, and a count has no "
                    "seed-dependent protocol to match")
    return ident


_RUN_COUNT = re.compile(
    r"\b(?:averaged|average|mean|median)\s+(?:over|across|of)\s+(\d+)\s+"
    r"(?:independent\s+|random\s+)?(?:runs?|seeds?|simulations?|trials?|repetitions?|"
    r"experiments?|replications?|folds?|splits?)\b", re.I)
_RUN_KEY = re.compile(
    r"^\s*[\"']?((?:num|n|number_of)_?(?:exp|exps|experiments?|runs?|seeds?|trials?|"
    r"simulations?|sims?|reps?|repeats?|repetitions?|replications?|folds?))[\"']?"
    r"\s*[:=]\s*(\d+)\s*(?:#.*)?,?\s*$", re.I | re.M)


def declared_run_counts(repo: Path) -> dict[str, int]:
    """`file:key` -> the run count a repository's configuration files declare at top level."""
    out: dict[str, int] = {}
    for pattern in ("*.yaml", "*.yml", "*.json", "*.toml", "config*.py", "configs/*.yaml",
                    "config/*.yaml"):
        for path in sorted(repo.glob(pattern))[:40]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _RUN_KEY.finditer(text):
                out[f"{path.relative_to(repo).as_posix()}:{m.group(1)}"] = int(m.group(2))
    return out


def resolve_configuration(doc: PaperDoc, table_ref: str, cmd: CandidateCommand | None,
                          harness_seeds: int = 5, claim_ref: str = "",
                          claim_quote: str = "", repo: Path | None = None) -> ConfigurationIdentity:
    """Dataset/model/schedule and, above all, the seed policy — matched or explicitly not."""
    ident = ConfigurationIdentity()
    m = re.fullmatch(r"T(\d+):r(\d+):c(\d+)", (table_ref or "").strip())
    table = (next((t for t in doc.tables if t.table_idx == int(m.group(1))), None) if m else None)
    if table is None or cmd is None:
        if claim_quote and (claim_ref or "").startswith("P"):
            return _prose_configuration(claim_quote, cmd, claim_ref)
        ident.state = "unmapped"
        ident.reason = "no cell or no command to match a configuration against"
        return ident

    caption = table.caption or ""
    for field, pattern in (("model", r"(LLaMA[\s\-]?2?[\s\-]?\d+B|RoBERTa\w*|T5\w*|BERT\w*|GPT-?J)"),
                           ("dataset", r"(Alpaca[\w\-]*|GLUE|SST-?2|CIFAR-?\d+|ImageNet|SQuAD\w*|CNN/?DM)"),
                           ("sparsity", r"(\d+%\s*sparsity)")):
        if hit := re.search(pattern, caption, re.I):
            ident.matched[field] = hit.group(1)
            ident.evidence.append(IdentityEvidence(quote=hit.group(1), source_ref=table_ref,
                                                   note=f"{field} recovered from the caption"))
    # Outside the named benchmarks above, the cell's own text still identifies its setting:
    # a documented value the README's command takes and the cell names (the slot filled by
    # `instantiate`) is the dataset; a row label naming a method the checkout implements
    # (checked by `resolve_experiment`) is the method. Nothing else is inferred.
    row, col = int(m.group(2)), int(m.group(3))
    labels, own, _refusal = cell_method(doc, table, row, col)
    if value := selects(cmd, labels):          # the command's own method argument selects it
        at = cmd.argv.index(value)
        ident.matched["model"] = value
        ident.evidence.append(IdentityEvidence(
            quote=f"{cmd.argv[at - 1]} {value}", source_ref=cmd.source_ref,
            note=f"the README documents {value!r} as a method argument, and the cited cell names it"))
    elif own:
        # A command with no argument naming the method runs the paper's OWN method; the cell
        # is that configuration only when the paper names it as its own.
        ident.matched["model"] = labels[0] if labels and labels[0] else "the paper's own method"
        ident.evidence.append(IdentityEvidence(
            quote=ident.matched["model"], source_ref=table_ref,
            note="the cited cell is the paper's own method, which the documented command runs"))
    for slot, value in cmd.bound_slots.items():
        at = cmd.argv.index(value) if value in cmd.argv else -1
        flag = cmd.argv[at - 1].lower() if at > 0 else ""
        # A slot is the dataset only when a FLAG says so; a positional's predecessor is the
        # program file, which says nothing.
        if ("dataset" in ident.matched or not flag.startswith("-")
                or not re.search(r"data|task|benchmark|corpus", flag)):
            continue
        ident.matched["dataset"] = value
        ident.evidence.append(IdentityEvidence(
            quote=f"{flag} {value}", source_ref=cmd.source_ref,
            note=f"the README documents {value!r} for {slot}, and the cited cell names it"))
    # `sparsity` is REPORTED when the caption states it, never REQUIRED (it exists only
    # in pruning papers). Model and dataset identify a configuration in general.
    for field in ("model", "dataset"):
        if field not in ident.matched:
            ident.unrecoverable.append(field)

    # Protocol: how many runs the printed number aggregates, against how many the
    # repository's own configuration declares. A different count is a different experiment.
    paper_runs = _RUN_COUNT.search(caption)
    declared = declared_run_counts(repo) if repo is not None else {}
    if paper_runs and declared:
        wanted = int(paper_runs.group(1))
        ident.evidence.append(IdentityEvidence(quote=paper_runs.group(0), source_ref=table_ref,
                                               note="the paper's aggregation protocol"))
        off = {where: n for where, n in declared.items() if n != wanted}
        if off and len(off) == len(declared):
            ident.state = "unsupported"
            ident.reason = (f"the paper's cell aggregates {paper_runs.group(0)!r} and the "
                            f"repository declares " + ", ".join(f"{w} = {n}" for w, n in off.items())
                            + "; running it would reproduce a different protocol"
                            + (f" (also unrecoverable from the cell: {', '.join(ident.unrecoverable)})"
                               if ident.unrecoverable else ""))
            return ident

    # Seed policy. The paper's own variance reporting decides what protocol the cell
    # represents; substituting a different one silently is the failure to avoid.
    reported = [n for n in doc.reported_numbers
                if (n.table_ref or "") == table_ref and (n.seeds_or_variance or "").strip()]
    ident.seed_policy_paper = (f"variance reported ({reported[0].seeds_or_variance})" if reported
                               else "single run, no variance reported")
    if cmd.seed_flag and cmd.seed_values:
        ident.seed_policy_repo = f"fixed {cmd.seed_flag} {'/'.join(sorted(cmd.seed_values))}"
    elif cmd.seed_flag:
        ident.seed_policy_repo = f"{cmd.seed_flag} accepted, no value pinned"
    else:
        ident.seed_policy_repo = "no seed argument found"

    single_repo_seed = len(cmd.seed_values) <= 1
    # A command with no seed argument is run exactly as documented (`routes.plan_execution`
    # repeats it only to observe determinism), so it passes ONE seed policy: the repo's own.
    harness_distinct = harness_seeds if harness_seed_flag(cmd) else 1
    ident.seed_policy_match = not (single_repo_seed and harness_distinct > 1 and not reported)
    if not ident.seed_policy_match:
        ident.state = "unsupported"
        ident.reason = (f"the paper's cell is {ident.seed_policy_paper} and the repository pins "
                        f"{ident.seed_policy_repo}, but this harness would run {harness_seeds} "
                        f"seeds — a different protocol from the one that produced the cell")
        return ident
    if ident.unrecoverable:
        ident.state = "unmapped"
        ident.reason = ("configuration fields could not be recovered and will not be guessed: "
                        + ", ".join(ident.unrecoverable))
        return ident

    ident.state = "established"
    ident.reason = ("model, dataset and sparsity recovered from the cell's own caption, and the "
                    "seed protocol is compatible")
    return ident


def resolve(doc: PaperDoc, repo: Path, table_ref: str, finding_id: str = "",
            harness_seeds: int = 5, claim_ref: str = "",
            claim_quote: str = "") -> tuple[ExperimentIdentity, MetricIdentity,
                                            ConfigurationIdentity]:
    """The whole chain. Each link is independent and each may abstain on its own.
    `claim_ref`/`claim_quote` open the PROSE path (a `P<i>:<a>-<b>` address minted by
    `harness.claims`); each link takes that path only when there is no cell address."""
    experiment = resolve_experiment(doc, repo, table_ref, finding_id, claim_ref, claim_quote)
    metric = resolve_metric(doc, table_ref, experiment.command, claim_ref, claim_quote)
    configuration = resolve_configuration(doc, table_ref, experiment.command, harness_seeds,
                                          claim_ref, claim_quote, repo=repo)
    return experiment, metric, configuration


def identities_established(experiment: ExperimentIdentity | None, metric: MetricIdentity | None,
                           configuration: ConfigurationIdentity | None) -> tuple[bool, str, str]:
    """(ok, failure_class, reason). The single place the three links are ANDed together."""
    for ident, cls, label in ((experiment, "experiment_unidentified", "experiment"),
                              (metric, "metric_unbound", "metric"),
                              (configuration, "configuration_unmatched", "configuration")):
        if ident is None:
            return False, cls, f"{label} identity was never assessed"
        if not ident.established:
            return False, cls, f"{label} identity is '{ident.state}': {ident.reason}"
    return True, "none", "experiment, metric and configuration identity all established"
