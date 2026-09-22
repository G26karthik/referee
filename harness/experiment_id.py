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

import re
from pathlib import Path

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


def _advertised_line(raw: str) -> str:
    """The command a line advertises, or '' — direct form first, then launcher-prefixed.
    Returns the command VERBATIM where already plain, or the program invocation a
    launcher was wrapping. Nothing is synthesised."""
    if m := _CMD_LINE.match(raw):
        return m.group(1)
    if m := _LAUNCHED.match(raw):
        if p := _PROGRAM.search(m.group(1)):
            prog = p.group(1)
            return prog if prog.split()[0] in ("python", "python3", "bash", "sh") else f"python {prog}"
    return ""
_SEED_FLAG = re.compile(r"--([a-z_]*seed[a-z_]*)(?:[=\s]+(\S+))?", re.I)
# A column of ratios always carries its own normaliser: some row reads exactly 100%.
_HUNDRED = re.compile(r"^\s*100(?:\.0+)?\s*%")


def _quantity_of(text: str) -> str:
    """The semantic quantity a header/caption/key names, or '' when it names none."""
    low = (text or "").lower()
    for quantity, words in _QUANTITY_WORDS:
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return quantity
    return ""


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
    if col < len(table.header):
        return table.header[col]
    return table.caption or ""


# === Candidate discovery — from what the repository advertises, never from filenames ===
def harvest_candidates(repo: Path, limit: int = 400) -> list[CandidateCommand]:
    """Every command the repository tells a reader to run, with its source location.
    Filenames are not evidence: `evaluate.py` existing says nothing about what it does; a
    README line saying "to finetune X, run Y" does."""
    out: list[CandidateCommand] = []

    readme = repo / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8", errors="replace")
        for block in _FENCE.finditer(text):
            line_no = text[:block.start()].count("\n") + 1
            for i, raw in enumerate(block.group(1).splitlines()):
                if advertised := _advertised_line(raw):
                    out.append(CandidateCommand(
                        argv=advertised.split(), source="readme",
                        source_ref=f"README.md:{line_no + i + 1}"))

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
    for token in cmd.argv[1:]:
        candidate = repo / token
        if candidate.is_file():
            return candidate
    return None


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
    if depth > 0 and path.suffix == ".sh":
        for m in re.finditer(r"\b(?:python3?|bash)\s+(\S+\.(?:py|sh))", text):
            nested = repo / m.group(1)
            if nested.is_file():
                inner = describe_command(repo, CandidateCommand(argv=["python", m.group(1)]),
                                         depth - 1)
                emits += inner.emits
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
    ident.cell_quantity = _quantity_of(header) or _quantity_of(table.caption or "")
    ident.cell_basis, ident.cell_unit = cell_basis(table, col)
    ident.evidence.append(IdentityEvidence(
        quote=header or (table.caption or "")[:120], source_ref=table_ref,
        note=f"cell quantity '{ident.cell_quantity or 'unknown'}', basis '{ident.cell_basis}'"))

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
    candidates = [describe_command(repo, c) for c in harvest_candidates(repo)]
    ident.considered = len(candidates)
    fitting = [c for c in candidates
               if any(q == "count" for k, q in _OUTPUT_QUANTITY if k in c.emits)]
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

    # The row names a method. If the repository does not implement it, no command in the
    # repository can produce the row, however capable the machine is.
    if ident.row_method and not repo_implements(repo, ident.row_method):
        ident.state = "no_candidate"
        ident.reason = (f"the cited row reports {ident.row_method!r}, and no .py or .sh file in "
                        f"this checkout implements it — the cell is a third-party baseline the "
                        f"authors cited rather than a result their code produces")
        return ident

    candidates = [describe_command(repo, c) for c in harvest_candidates(repo)]
    ident.considered = len(candidates)
    cell_quantity = _quantity_of(_header_for(table, int(m.group(3)))) or _quantity_of(table.caption or "")
    fitting = [c for c in candidates
               if cell_quantity and any(q == cell_quantity for k, q in _OUTPUT_QUANTITY if k in c.emits)]
    for c in candidates:
        if c not in fitting:
            ident.rejected.append(f"{c.source_ref}: emits {c.label or 'unknown'}")

    if not fitting:
        ident.state = "no_candidate"
        ident.reason = (f"none of the {len(candidates)} advertised command(s) emits "
                        f"{cell_quantity or 'the cell’s quantity'}")
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
                note=f"the only one of {len(fitting)} candidates emitting {cell_quantity} "
                     f"whose declared configuration matches the cited row ({fields_str})"))
            for field, ref in expected_evidence.items():
                ident.evidence.append(IdentityEvidence(
                    quote=f"{field}={expected[field]}", source_ref=ref,
                    note="the cited row's own configuration, matched against the chosen "
                         "command's declared configuration"))
            ident.reason = (
                f"{len(fitting)} advertised commands could produce {cell_quantity}; "
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
        ident.reason = (f"{len(fitting)} advertised commands could produce {cell_quantity}; "
                        f"choosing between them would be a guess, so none is chosen "
                        f"({', '.join(c.source_ref for c in fitting[:4])})."
                        f"{narrowing_note}")
        return ident

    ident.command = fitting[0]
    ident.state = "established"
    ident.evidence.append(IdentityEvidence(
        quote=" ".join(ident.command.argv), source_ref=ident.command.source_ref,
        note=f"the only advertised command emitting {cell_quantity}"))
    ident.reason = f"one advertised command emits {cell_quantity}: {' '.join(ident.command.argv)}"
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


def resolve_configuration(doc: PaperDoc, table_ref: str, cmd: CandidateCommand | None,
                          harness_seeds: int = 5, claim_ref: str = "",
                          claim_quote: str = "") -> ConfigurationIdentity:
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
    # `sparsity` is REPORTED when the caption states it, never REQUIRED (it exists only
    # in pruning papers). Model and dataset identify a configuration in general.
    for field in ("model", "dataset"):
        if field not in ident.matched:
            ident.unrecoverable.append(field)

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
    ident.seed_policy_match = not (single_repo_seed and harness_seeds > 1 and not reported)
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
                                          claim_ref, claim_quote)
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
