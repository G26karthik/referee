"""E1 — does the published experiment FIT, decided before anything is launched.

Three preconditions already stand in front of repository execution. Provenance asks whose
code it is, identity asks whether it answers the cited cell, capability asks whether this
machine can start it. None of them asks whether the experiment as published fits in the
hardware present, and that question cannot be answered by attempting it: an attempt
produces a CUDA OOM twenty minutes in, which is indistinguishable at the stderr level from
the authors' code being broken.

`python -m harness.resources` runs the self-check.

**Why a launchable process proves nothing.** `evaluate.py --seed 0` starts perfectly well
on an 8 GB card and dies loading a 7B checkpoint. By then it has printed banners, run for
minutes, and possibly emitted output lines — which is exactly the evidence
`reached_experiment` reads as "the experiment began". The resource check therefore has to
happen before, from declared numbers, or it does not happen at all.

**Where a requirement may come from.** Three kinds, ranked, and every one of them carries
a verbatim quote and a re-checkable `source_ref`:

  declared_requirement  the authors state what their method costs
                        "APT costs less than 24GB of memory when pruning 30% parameters"
  declared_hardware     the authors state the machine they used
                        "Both training and evaluation are conducted on a single A100 GPU"
  derived_floor         computed from a model scale the cited cell itself names
                        "LLaMA 2 7B" -> 7e9 params at fp16 -> ~13 GiB of weights alone

The distinction between the first two matters and is not cosmetic. A stated cost is a
requirement. A stated machine is an upper bound on what was available, not a statement of
need — an author with an 80 GB card may have used 6 GB of it. So `declared_hardware` is
recorded as evidence and is deliberately NOT converted into a VRAM requirement; treating
it as one would block experiments that fit comfortably, on the strength of the authors
owning good hardware. `derived_floor` is the opposite: a hard lower bound that no
configuration can go under, because the weights have to be somewhere.

**What is deliberately absent.** There is no function here that adapts a requirement
downward to fit a backend. No batch-size search, no precision fallback, no sequence-length
trim, no seed reduction, no smaller checkpoint. Every one of those makes something run,
and what runs is a different experiment. A number produced that way, reconciled against
the paper's cell, would be the most damaging output this harness could emit — a crash is
visibly wrong, and a confident figure from an altered protocol is not.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .schema import (GIB, PaperDoc, ResourceCapability, ResourceEvidence,
                     ResourceRequirement)

# --------------------------------------------------------------------------- #
# Reading a demand out of prose
# --------------------------------------------------------------------------- #
# There is deliberately no table here mapping "A100" to 40 GB. Such a table exists to
# convert declared HARDWARE into a VRAM requirement, and that conversion is the error
# this module is built to avoid: an author with a 40 GB card may have used 6 GB of it,
# so charging the experiment 40 GB would refuse runs that fit comfortably. The model is
# recorded as evidence and left as a name.
_GPU_NAMES = re.compile(
    r"\b(A100|H100|H200|A6000|A40|V100|P100|T4|L40S?|L4|RTX\s?3090|RTX\s?4090|A10|TITAN\s?\w*)\b",
    re.IGNORECASE)
# "8 A100 GPUs", "4x V100", "a single A100"
_GPU_COUNT = re.compile(
    r"\b(?:(\d{1,3})\s*[x×]?\s*|(single|one|two|four|eight))\s+"
    r"(?:NVIDIA\s+)?(A100|H100|H200|A6000|A40|V100|P100|T4|L40S?|L4|RTX\s?3090|RTX\s?4090|A10)\b",
    re.IGNORECASE)
_WORD_COUNT = {"single": 1, "one": 1, "two": 2, "four": 4, "eight": 8}

# A stated MEMORY COST. The qualifier is required: "costs less than 24GB of memory" is a
# requirement, whereas "24GB" floating in a sentence about a product could be anything.
_MEM_COST = re.compile(
    r"[^.]*?\b(?:cost[s]?|require[sd]?|use[sd]?|consum\w+|need[s]?|take[s]?|fit[s]?\s+in|"
    r"memory\s+footprint\s+of|peak\s+memory\s+of)\b[^.]{0,120}?"
    r"(\d+(?:\.\d+)?)\s*(GB|GiB|TB|MB)\b[^.]{0,60}?\bmemor\w+", re.IGNORECASE)
_MEM_COST_ALT = re.compile(
    r"[^.]*?\b(?:cost[s]?|require[sd]?|use[sd]?|consum\w+|need[s]?)\b[^.]{0,60}?"
    r"(\d+(?:\.\d+)?)\s*(GB|GiB|TB|MB)\b[^.]{0,80}?\bmemor\w+", re.IGNORECASE)
_UNIT = {"mb": 1024 ** 2, "gb": GIB, "gib": GIB, "tb": 1024 ** 4}

# A sentence that structurally signals "this figure is about a DIFFERENT method" — the
# paper contrasting its own cost against a baseline's, in either order. Matched by
# STRUCTURE (a comparison connective), never by any method's name, so it generalizes to
# every paper rather than the one it was found on.
_CONTRAST_CUE = re.compile(
    r"\b(in\s+contrast|compared\s+(?:to|with)|by\s+contrast|whereas|unlike|"
    r"on\s+the\s+other\s+hand|while\s+\w+\s+(?:costs?|requires?|uses?|needs?))\b",
    re.IGNORECASE)
# "30% of the 24GB" or "24GB (12%)" — the number carries a unit, but a percent sign in
# its immediate context means the sentence is stating a RATIO, not a standalone cost, and
# reading the absolute figure alone drops exactly the qualifier that made it a ratio.
_PERCENT_NEARBY = re.compile(r"%")

# A model scale named in a caption or a row label. Parameter counts are the published
# ones; the fp16 floor is 2 bytes each, which is the least any implementation can hold.
_MODEL_SCALE = re.compile(
    r"\b(?:(LLaMA|Llama|LLAMA|OPT|GPT-?J|GPT-?2|Mistral|Falcon|Pythia|T5|BERT|RoBERTa|DeBERTa)"
    r"[\s\-]?(?:2|3)?[\s\-]?)(\d+(?:\.\d+)?)\s?B\b")
_SMALL_MODELS = {
    "roberta-base": 125e6, "roberta-large": 355e6, "bert-base": 110e6, "bert-large": 340e6,
    "t5-base": 220e6, "t5-large": 770e6, "t5-small": 60e6, "deberta-base": 140e6,
}
_SMALL_RE = re.compile(r"\b(RoBERTa|BERT|T5|DeBERTa)[\s\-](base|large|small)\b", re.IGNORECASE)
_BYTES_PER_PARAM_FP16 = 2

# Declared runtime. "trained for 3 days", "about 40 GPU-hours".
_WALLTIME = re.compile(
    r"[^.]{0,80}?\b(\d+(?:\.\d+)?)\s*(GPU-?hours?|hours?|days?|minutes?)\b[^.]{0,80}", re.IGNORECASE)
_WALLTIME_S = {"minute": 60, "hour": 3600, "gpu-hour": 3600, "gpuhour": 3600, "day": 86400}

# PDF text is soft-hyphenated at line breaks: the real APT paper reads "costs less than
# 24GB of mem- ory", and a pattern anchored on `memor\w+` misses it entirely. That miss is
# not cosmetic — with the method's own 24GB sentence invisible, the only remaining match
# was the CONTRASTED BASELINE's "LLM-Pruner costs about 80GB memory", so the requirement
# came out as 80 GiB attributed to APT, with the baseline's sentence as its evidence.
# Exactly the misattribution `declared_memory_cost` documents itself as avoiding.
#
# So matching and verification both run on a normalized copy. The normalization is
# symmetric by construction — the same function produces the stored quote and prepares
# the corpus it is checked against — which keeps the quote re-verifiable rather than
# merely plausible.
_SOFT_HYPHEN = re.compile(r"(\w)-\s+(\w)")


def normalize(text: str) -> str:
    """Rejoin words the PDF broke across lines, then collapse whitespace."""
    return " ".join(_SOFT_HYPHEN.sub(lambda m: m.group(1) + m.group(2), text or "").split())


# --------------------------------------------------------------------------- #
# What the backend actually has
# --------------------------------------------------------------------------- #
def host_vram_bytes() -> tuple[int | None, str, int]:
    """(bytes on the largest visible GPU, its name, how many GPUs). Reads nvidia-smi only.

    A query, not a workload: `--query-gpu` prints the driver's own inventory and touches
    no CUDA context, so it stays safe to call with every execution gate shut.
    """
    if shutil.which("nvidia-smi") is None:
        return None, "", 0
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, "", 0
    if p.returncode != 0:
        return None, "", 0
    best, name, count = 0, "", 0
    for line in (p.stdout or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 2 or not parts[1].replace(".", "").isdigit():
            continue
        count += 1
        mib = int(float(parts[1]))
        if mib > best:
            best, name = mib, parts[0]
    return (best * 1024 * 1024 if best else None), name, count


def host_ram_bytes() -> int | None:
    """Total physical memory. No psutil in this environment, so ask the OS directly."""
    if sys.platform == "win32":
        class _Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = _Status()
        st.dwLength = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):   # type: ignore[attr-defined]
            return int(st.ullTotalPhys)
        return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def host_disk_bytes(path: str | Path = ".") -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# What the experiment demands
# --------------------------------------------------------------------------- #
def _section_ref(doc: PaperDoc, needle: str) -> str:
    needle = normalize(needle)
    for sec in doc.sections:
        if needle and needle in normalize(sec.text or ""):
            return f"p{sec.page_start}" if getattr(sec, "page_start", None) else "p?"
    return ""


def _sentence(corpus: str, at: int) -> str:
    lo = corpus.rfind(".", 0, at) + 1
    hi = corpus.find(".", at)
    return " ".join(corpus[lo:(hi + 1 if hi > 0 else len(corpus))].split())


def declared_memory_cost(doc: PaperDoc) -> tuple[int | None, ResourceEvidence | None]:
    """The smallest memory COST the paper states for its own method, with its sentence.

    Smallest, not largest, because a paper that reports several figures is usually
    contrasting its own cost against a baseline's — "APT costs less than 24GB ... LLM-Pruner
    costs about 80GB" — and charging the method with the baseline's number would block on
    a figure the authors were arguing against.

    Two figures are excluded from consideration entirely, rather than merely risked:

      - a sentence a CONTRAST connective marks as being about a different method
        ("in contrast", "compared to", "unlike", ...). Smallest-of-everything used to
        pick whichever number was numerically least, including a contrasted baseline's,
        when that baseline happened to be cheaper than the method under audit — "smallest"
        is a tbreak for THIS paper's ordering, not a rule that holds in general.
      - a figure with a '%' immediately around it: "30% of the 24GB budget" states a
        RATIO, and reading the absolute number out of it drops exactly the qualifier that
        made it one. The window is local to the number, not the whole sentence, so an
        unrelated percentage elsewhere in the same sentence ("... 24GB of memory when
        pruning 30% parameters ...") does not disqualify a genuine declared cost.

    If every remaining candidate is contrast-flagged — the paper states costs for other
    methods but never states its own — nothing is returned. Attributing a comparison
    figure to the method under audit would be the exact misattribution this function
    exists to avoid; silence is not evidence the true cost is small.
    """
    corpus = normalize("\n".join(s.text or "" for s in doc.sections))
    clean: list[tuple[int, str]] = []
    contrasted: list[tuple[int, str]] = []
    for rx in (_MEM_COST, _MEM_COST_ALT):
        for m in rx.finditer(corpus):
            unit = _UNIT.get(m.group(2).lower())
            if not unit:
                continue
            window = corpus[max(0, m.start(1) - 15):m.end(2) + 15]
            if _PERCENT_NEARBY.search(window):
                continue
            value = int(float(m.group(1)) * unit)
            sentence = _sentence(corpus, m.start())
            bucket = contrasted if _CONTRAST_CUE.search(sentence) else clean
            if not any(value == v for v, _ in bucket):
                bucket.append((value, sentence))
    candidates = clean or None
    if not candidates:
        return None, None
    best = min(candidates, key=lambda t: t[0])
    return best[0], ResourceEvidence(
        quote=best[1][:400], source_ref=_section_ref(doc, best[1][:40]) or "p?",
        kind="declared_requirement",
        note="the paper states this as the memory its own method costs")


def declared_hardware(doc: PaperDoc) -> tuple[str, int | None, ResourceEvidence | None]:
    """(model, count, evidence) for the accelerator the paper says it used.

    Recorded but NOT turned into a VRAM requirement. Owning an A100 is not evidence that
    the experiment needs 40 GB, and reading it that way would refuse experiments that fit.
    """
    corpus = normalize("\n".join(s.text or "" for s in doc.sections))
    m = _GPU_COUNT.search(corpus)
    if m:
        count = int(m.group(1)) if m.group(1) else _WORD_COUNT.get((m.group(2) or "").lower(), 1)
        return (m.group(3).upper().replace(" ", ""), count,
                ResourceEvidence(quote=_sentence(corpus, m.start())[:400],
                                 source_ref=_section_ref(doc, _sentence(corpus, m.start())[:40]) or "p?",
                                 kind="declared_hardware",
                                 note="the machine the authors used — an upper bound on what was "
                                      "available, not a statement of what the experiment needs"))
    m2 = _GPU_NAMES.search(corpus)
    if m2:
        return (m2.group(1).upper().replace(" ", ""), None,
                ResourceEvidence(quote=_sentence(corpus, m2.start())[:400],
                                 source_ref=_section_ref(doc, _sentence(corpus, m2.start())[:40]) or "p?",
                                 kind="declared_hardware", note="accelerator named in the text"))
    return "", None, None


def model_weight_floor(text: str, source_ref: str) -> tuple[int | None, str, ResourceEvidence | None]:
    """(bytes, scale, evidence) — the least memory the named model's weights can occupy.

    A hard lower bound and the one requirement no configuration can argue with: whatever
    the batch size, the precision above fp16, or the schedule, the parameters have to be
    resident. Computed at fp16, which is the floor for a model trained or evaluated in
    anything wider.
    """
    if not text:
        return None, "", None
    m = _MODEL_SCALE.search(text)
    if m:
        params = float(m.group(2)) * 1e9
        scale = " ".join(m.group(0).split())
        return (int(params * _BYTES_PER_PARAM_FP16), scale,
                ResourceEvidence(quote=scale, source_ref=source_ref, kind="derived_floor",
                                 note=f"{m.group(2)}B parameters at fp16 = 2 bytes each; the "
                                      f"weights must be resident regardless of configuration"))
    m2 = _SMALL_RE.search(text)
    if m2:
        key = f"{m2.group(1).lower()}-{m2.group(2).lower()}"
        params = _SMALL_MODELS.get(key)
        if params:
            return (int(params * _BYTES_PER_PARAM_FP16), m2.group(0),
                    ResourceEvidence(quote=m2.group(0), source_ref=source_ref, kind="derived_floor",
                                     note=f"{key} is {params/1e6:.0f}M parameters at fp16"))
    return None, "", None


def declared_walltime(doc: PaperDoc) -> tuple[int | None, ResourceEvidence | None]:
    """The longest runtime the paper declares, as seconds. The execution budget to beat."""
    corpus = normalize("\n".join(s.text or "" for s in doc.sections))
    best: tuple[int, str] | None = None
    for m in _WALLTIME.finditer(corpus):
        unit = m.group(2).lower().rstrip("s").replace("-", "")
        secs = _WALLTIME_S.get(unit)
        if not secs:
            continue
        total = int(float(m.group(1)) * secs)
        if best is None or total > best[0]:
            best = (total, _sentence(corpus, m.start()))
    if best is None:
        return None, None
    return best[0], ResourceEvidence(quote=best[1][:400], source_ref="p?",
                                     kind="declared_requirement",
                                     note="declared runtime for the published experiment")


def require_resources(doc: PaperDoc, table_ref: str = "",
                      cell_text: str = "", caption: str = "") -> ResourceRequirement:
    """What the CITED experiment demands, assembled from evidence and nothing else.

    The cell's own caption is consulted first for a model scale, because the requirement
    has to be about the experiment under audit rather than about the paper's largest one:
    a paper reporting both RoBERTa and LLaMA-2-7B has two very different demands, and
    charging a RoBERTa cell with the 7B floor would block a run that fits.
    """
    req = ResourceRequirement()

    vram, ev = declared_memory_cost(doc)
    if ev:
        req.vram_bytes, _ = vram, req.evidence.append(ev)

    gpu, count, hw_ev = declared_hardware(doc)
    if hw_ev:
        req.gpu_model, req.gpu_count = gpu, count
        req.evidence.append(hw_ev)

    floor, scale, floor_ev = model_weight_floor(caption or cell_text, table_ref or "p?")
    if floor_ev:
        req.model_scale = scale
        req.evidence.append(floor_ev)
        # The floor RAISES a stated cost but never lowers it: a paper claiming 24 GB for a
        # model whose weights alone are 26 GB has been misread, and the larger figure is
        # the one that cannot be wrong. But a floor that is the ONLY basis for the number —
        # no declared cost ever anchored it — is not a requirement, it is a lower bound:
        # weights only, no activations, no optimizer state, no gradients, no KV cache. That
        # distinction is recorded so `assess_resources` can refuse to call a floor alone
        # "satisfied" just because it happens to fit.
        if floor is not None:
            req.vram_bytes = floor if req.vram_bytes is None else max(req.vram_bytes, floor)
            req.vram_is_floor_only = req.vram_bytes == floor

    wall, wall_ev = declared_walltime(doc)
    if wall_ev:
        req.walltime_s = wall
        req.evidence.append(wall_ev)

    req.unstated = [name for name, value in (("vram", req.vram_bytes), ("ram", req.ram_bytes),
                                             ("disk", req.disk_bytes), ("cpu", req.cpu_count),
                                             ("walltime", req.walltime_s))
                    if value is None]
    return req


def verify_requirement(req: ResourceRequirement, doc: PaperDoc) -> list[ResourceEvidence]:
    """Evidence whose quote no longer appears in the paper. Empty means every quote holds.

    The same discipline S2 applies to a finding's `evidence_quote`, applied to a
    requirement: a requirement that blocks execution is an assertion about the paper, and
    it has to be re-checkable against the parsed corpus rather than trusted.

    Captions and cell contents are part of the searchable corpus, not just section prose.
    A `derived_floor` is read off the cited table's own caption — "Table 3: LLaMA 2 7B ..."
    — and a verifier that looked only at `doc.sections` would reject the one requirement
    anchored most tightly to the cell under audit.
    """
    parts = [s.text or "" for s in doc.sections]
    for t in doc.tables:
        parts.append(t.caption or "")
        parts.extend(" ".join(str(c) for c in row) for row in (t.rows or []))
    flat = normalize("\n".join(parts))
    bad = []
    for ev in req.evidence:
        q = normalize(ev.quote or "")
        if not q or q not in flat:
            bad.append(ev)
    return bad


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #
def _gib(n: int | None) -> str:
    return "unknown" if n is None else f"{n / GIB:.1f} GiB"


def assess_resources(req: ResourceRequirement | None, resources, backend: str = "",
                     walltime_budget_s: int | None = None) -> ResourceCapability:
    """Does the backend satisfy the requirement? `satisfied` is the only permitting state.

    Note what this function does NOT do when a requirement exceeds what is available: it
    does not look for a configuration that would fit. There is no such path, here or
    anywhere else in the module. The published experiment is the experiment.
    """
    cap = ResourceCapability(backend=backend, requirement=req)
    cap.available_vram_bytes = getattr(resources, "vram_bytes", None)
    cap.available_ram_bytes = getattr(resources, "ram_bytes", None)
    cap.available_disk_bytes = getattr(resources, "disk_bytes", None)
    cap.available_cpu_count = getattr(resources, "cpu_count", None)
    cap.available_gpu_count = getattr(resources, "gpu_count", None)
    cap.available_gpu_model = getattr(resources, "gpu_name", "") or ""

    if req is None:
        cap.state = "unassessed"
        cap.reason = "resource requirements were never examined for this experiment"
        return cap
    if not req.stated:
        cap.state = "unknown"
        cap.reason = (
            "neither the paper nor the repository states what this experiment costs, so it "
            "cannot be shown to fit. Silence about a demand is not evidence that the demand "
            "is small: an unbounded run that OOMs mid-training is indistinguishable, at the "
            "stderr, from the authors' code being broken.")
        return cap

    checks = (
        ("VRAM", req.vram_bytes, cap.available_vram_bytes, _gib),
        ("RAM", req.ram_bytes, cap.available_ram_bytes, _gib),
        ("disk", req.disk_bytes, cap.available_disk_bytes, _gib),
        ("CPU", req.cpu_count, cap.available_cpu_count, str),
        ("GPU count", req.gpu_count, cap.available_gpu_count, str),
    )
    unmeasured = []
    for label, need, have, fmt in checks:
        if need is None:
            continue
        if have is None:
            unmeasured.append(f"{label}: the experiment declares {fmt(need)} and the backend "
                              f"cannot report what it has")
            continue
        if need > have:
            cap.shortfalls.append(
                f"{label}: the published experiment requires {fmt(need)}, the '{backend}' "
                f"backend offers {fmt(have)}")

    if walltime_budget_s and req.walltime_s and req.walltime_s > walltime_budget_s:
        cap.shortfalls.append(
            f"walltime: the paper declares {req.walltime_s / 3600:.1f}h for this experiment "
            f"and the execution budget is {walltime_budget_s / 3600:.1f}h")

    if cap.shortfalls:
        cap.state = "insufficient"
        cap.reason = (
            "the experiment as published does not fit this backend: " + "; ".join(cap.shortfalls)
            + ". No reduced configuration is substituted — shrinking the model, batch, "
              "precision, sequence length, schedule or seed count would produce a different "
              "experiment, and its number would not be a reproduction of the cited cell.")
        return cap
    if not req.memory_stated:
        # AFTER the shortfall check on purpose: a demand that provably exceeds the backend
        # is a definite refusal and names itself, which is more useful than "we could not
        # establish the memory". This branch catches only the case where nothing ruled the
        # backend out — and where the reason nothing did is that the deciding quantity was
        # never established. `stated` was satisfied by some other field, which says
        # nothing at all about whether the experiment fits in memory.
        cap.state = "unknown"
        cap.reason = (
            "the sources establish some of this experiment's demands but not its memory: "
            f"{', '.join(n for n in req.unstated if n in ('vram', 'ram')) or 'vram, ram'} "
            "were never stated. Every other declared quantity fitting is not evidence that "
            "the experiment fits — an unstated memory demand is not a small one, and it is "
            "the demand that decides whether a run reaches its own first measurement.")
        return cap
    if unmeasured:
        cap.state = "unknown"
        cap.reason = ("the backend could not report the resources this experiment declares: "
                      + "; ".join(unmeasured))
        return cap
    if req.vram_bytes is not None and req.vram_is_floor_only:
        # C7 — a lower bound is not a requirement. The only number established is what the
        # model's weights occupy at fp16; everything else an actual run needs — activations,
        # optimizer state, gradients, a KV cache — was never stated, and fitting the floor
        # proves nothing about fitting THAT. Reported `unknown`, the same state an unstated
        # memory demand gets, because that is what this effectively is: a demand nobody
        # measured, with a minimum nobody can go under standing in its place.
        cap.state = "unknown"
        cap.reason = (
            f"only a lower bound was established for VRAM — {_gib(req.vram_bytes)}, the "
            f"weights of {req.model_scale or 'the cited model'} at fp16 — with no declared "
            f"cost to anchor it. The backend meeting that floor is not evidence the actual "
            f"experiment fits: activations, optimizer state, gradients and any KV cache are "
            f"not counted in it, and are not stated anywhere.")
        return cap

    cap.state = "satisfied"
    cap.reason = ("every declared requirement fits the backend: "
                  + ", ".join(f"{lbl} {fmt(need)} <= {fmt(have)}"
                              for lbl, need, have, fmt in checks if need is not None)
                  or "no requirement exceeds the backend")
    return cap


if __name__ == "__main__":  # self-check: python -m harness.resources
    from .schema import Section, Table

    # --- host probing -----------------------------------------------------------------
    vram, gpu, n = host_vram_bytes()
    print(f"host: vram={_gib(vram)} gpu={gpu or '(none)'} x{n} "
          f"ram={_gib(host_ram_bytes())} disk={_gib(host_disk_bytes())} cpu={os.cpu_count()}")
    assert host_disk_bytes() and os.cpu_count()

    # --- extraction, on APT's own sentences -------------------------------------------
    apt = PaperDoc(paper_id="apt", title="APT", sections=[Section(
        section_idx=1, title="Setup", page_start=7,
        text=("Both training and evaluation are conducted on a single A100 GPU. "
              "Also, APT costs less than 24GB of memory when pruning 30% parameters in "
              "LLaMA2-7B models before tuning. In contrast, LLM-Pruner costs about 80GB "
              "memory when pruning the LLaMA 7B model."))])
    apt.tables = [Table(table_idx=2, page=7,
                        caption="Table 3: LLaMA 2 7B 30% sparsity pruning results",
                        rows=[["Method", "TrainMem"], ["LLMPruner", "253.6%"]])]
    req = require_resources(apt, "T2:r3:c11", caption=apt.tables[0].caption)
    assert req.gpu_model == "A100" and req.gpu_count == 1, (req.gpu_model, req.gpu_count)
    assert req.vram_bytes is not None
    # The 7B weight floor (~13 GiB) is below the stated 24 GB cost, so the stated cost wins.
    assert abs(req.vram_bytes - 24 * GIB) < GIB, _gib(req.vram_bytes)
    assert req.model_scale.replace(" ", "") in ("LLaMA27B", "LLaMA2 7B".replace(" ", ""))
    assert not verify_requirement(req, apt), "every quote must be findable in the paper"
    kinds = {e.kind for e in req.evidence}
    assert {"declared_requirement", "declared_hardware", "derived_floor"} <= kinds, kinds

    # The baseline's 80GB must NOT be charged to the method.
    assert req.vram_bytes < 80 * GIB, "the contrasted baseline figure is not the method's cost"

    class _R:
        vram_bytes, ram_bytes, disk_bytes = 8 * GIB, 16 * GIB, 200 * GIB
        cpu_count, gpu_count, gpu_name = 16, 1, "RTX 4060 Laptop GPU"

    cap = assess_resources(req, _R(), backend="local")
    assert cap.state == "insufficient", cap.reason
    assert not cap.established and any("VRAM" in s for s in cap.shortfalls), cap.shortfalls
    assert "24.0 GiB" in cap.reason and "8.0 GiB" in cap.reason, cap.reason

    class _Big(_R):
        vram_bytes = 40 * GIB

    assert assess_resources(req, _Big(), backend="stub").state == "satisfied"

    # --- silence never authorizes ------------------------------------------------------
    quiet = PaperDoc(paper_id="q", title="Q", sections=[Section(
        section_idx=0, title="S", page_start=1, text="We train a model and report accuracy.")])
    bare = require_resources(quiet)
    assert not bare.stated
    unk = assess_resources(bare, _R(), backend="local")
    assert unk.state == "unknown" and not unk.established, unk.reason
    assert assess_resources(None, _R()).state == "unassessed"

    # --- the derived floor stands on its own -------------------------------------------
    floor, scale, ev = model_weight_floor("Table 3: LLaMA 2 70B results", "T3:r0:c0")
    assert floor is not None and floor > 100 * GIB
    assert ev is not None and ev.kind == "derived_floor"
    small, sscale, sev = model_weight_floor("Table 2: RoBERTa-base pruning", "T2:r0:c0")
    assert small is not None and small < GIB, "a base-sized encoder is not a 7B floor"

    # --- walltime budget ----------------------------------------------------------------
    slow = ResourceRequirement(walltime_s=72 * 3600,
                               evidence=[ResourceEvidence(quote="72 hours", kind="declared_requirement")])
    over = assess_resources(slow, _Big(), backend="stub", walltime_budget_s=1800)
    assert over.state == "insufficient" and any("walltime" in s for s in over.shortfalls)

    # --- there is no adaptation path ----------------------------------------------------
    for banned in ("shrink", "reduce_to_fit", "fit_batch", "downscale", "adapt_requirement"):
        assert banned not in globals(), f"{banned} must not exist: an altered experiment is not a reproduction"

    print("resources self-check OK")
