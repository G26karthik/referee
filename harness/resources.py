"""E1 — does the published experiment FIT, decided before anything is launched, from
declared numbers rather than by attempting it (an attempted OOM twenty minutes in is
indistinguishable at the stderr level from the authors' code being broken).

A requirement comes from three ranked kinds, each carrying a verbatim quote and a
re-checkable `source_ref`: `declared_requirement` (the authors state what their method
costs), `declared_hardware` (the authors state the machine they used — an upper bound on
what was available, not a statement of need, so never converted into a VRAM requirement),
and `derived_floor` (computed from a named model scale — a hard lower bound no
configuration can go under, since the weights have to be somewhere).

Nothing here adapts a requirement downward to fit a backend: no batch-size search, no
precision fallback, no sequence-length trim, no smaller checkpoint. Any of those would
make a different experiment run, and a number reconciled from that would be the most
damaging output this harness could emit.

`python -m harness.resources` runs the self-check.
"""
from __future__ import annotations

import os
import re

from .schema import GIB, PaperDoc, ResourceEvidence, ResourceRequirement

# === Reading a demand out of prose =====================================================
# Deliberately no table mapping "A100" to 40 GB: that conversion is the error this module
# avoids (a 40 GB card may run at 6 GB). The model is recorded as evidence, left as a name.
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

# Signals "this figure is about a DIFFERENT method" — matched by comparison STRUCTURE,
# never by any method's name, so it generalizes across papers.
_CONTRAST_CUE = re.compile(
    r"\b(in\s+contrast|compared\s+(?:to|with)|by\s+contrast|whereas|unlike|"
    r"on\s+the\s+other\s+hand|while\s+\w+\s+(?:costs?|requires?|uses?|needs?))\b",
    re.IGNORECASE)
# "30% of the 24GB": a percent sign near the number means the sentence states a RATIO,
# not a standalone cost.
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

# PDF text is soft-hyphenated at line breaks ("mem- ory"), which would otherwise hide a
# cost sentence from a pattern anchored on `memor\w+`. Matching and verification both run
# on a normalized copy, produced by the same function, so a stored quote stays
# re-verifiable rather than merely plausible.
_SOFT_HYPHEN = re.compile(r"(\w)-\s+(\w)")


def normalize(text: str) -> str:
    """Rejoin words the PDF broke across lines, then collapse whitespace."""
    return " ".join(_SOFT_HYPHEN.sub(lambda m: m.group(1) + m.group(2), text or "").split())


# === What the experiment demands. Host MEASUREMENT (as opposed to demand EXTRACTION)
# lives in `harness.execute`'s backends, the only live callers. =========================
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
    Smallest, not largest: a paper reporting several figures is usually contrasting its own
    cost against a baseline's. Two figures are excluded entirely: a CONTRAST-marked
    sentence about a different method, and a figure with a '%' immediately around it
    ("30% of the 24GB budget" states a RATIO, not an absolute cost). If every remaining
    candidate is contrast-flagged, nothing is returned: silence is not evidence of a small
    cost."""
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
    """(model, count, evidence) for the accelerator the paper says it used. Recorded but
    NOT turned into a VRAM requirement: owning an A100 is not evidence the experiment
    needs 40 GB."""
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
    A hard lower bound no configuration can argue with, computed at fp16 (the floor for a
    model trained or evaluated in anything wider)."""
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
    """What the CITED experiment demands, assembled from evidence and nothing else. The
    cell's own caption is consulted for a model scale so the requirement is about the
    experiment under audit, not the paper's largest one."""
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
        # The floor RAISES a stated cost but never lowers it. A floor that is the ONLY
        # basis for the number is not a requirement, only a lower bound (weights only, no
        # activations/optimizer state/gradients/KV cache) — recorded so `assess_resources`
        # refuses to call it "satisfied" just because it happens to fit.
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
    """Evidence whose quote no longer appears in the paper. Empty means every quote holds:
    a requirement that blocks execution is an assertion about the paper and must be
    re-checkable against the parsed corpus. Captions and cell contents are part of the
    searchable corpus, not just section prose, since a `derived_floor` is read off the
    cited table's own caption."""
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


# === The comparison. `assess_resources` (E1's COMPARISON half) lives in `harness.execute`,
# the only live caller (via `routes.py`); this module keeps only the EXTRACTION half. =====
def _gib(n: int | None) -> str:
    return "unknown" if n is None else f"{n / GIB:.1f} GiB"


if __name__ == "__main__":  # self-check: python -m harness.resources
    from . import execute as _execute
    from .schema import Section, Table
    assess_resources = _execute.assess_resources
    host_vram_bytes = _execute.host_vram_bytes
    host_ram_bytes = _execute.host_ram_bytes
    host_disk_bytes = _execute.host_disk_bytes

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
