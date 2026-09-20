"""S3c — the Autonomous Probe Planner: author a targeted probe with no human in the loop.

The rest of S3 can acquire a paper's code and read it. It cannot *test an idea*, because
the repository's own entrypoint reproduces the paper's whole benchmark — hours of compute
and a dataset download — to answer a question that is usually much narrower than that.

This module closes that gap from the other side. It reads the paper's published
formulation, reimplements the mechanism at toy scale, and runs it against a control on
local hardware in under a minute. What comes back is a real measurement with a real
noise band.

**What a synthesized probe is, and what it is not.** It is our reimplementation of the
paper's stated algorithm, on synthetic data, at a scale the authors never used. That
makes it evidence about the MECHANISM — does the thing the paper says happens actually
happen, and is it bigger than seed noise — and it is not, and can never be, a
reproduction of a printed benchmark number. The two are separated in the artifact by
`ProbeSpec.provenance`, and `local_exec.reconcile` refuses to let a synthesized probe
reach `RESOLVED_VERIFIED` or `FAILED_REPRODUCTION` against a table cell. A toy MLP on
4,096 synthetic points is not entitled to convict an ImageNet number, and a harness
whose whole purpose is catching other people's unearned inferences must not make one.

**Pre-registration.** Every constant a template uses is fixed in its source with a
comment naming where in the paper it came from. Nothing here is tuned against its own
output — sweeping a knob until the probe agrees with a conclusion is the failure mode
this harness exists to detect, and it would be no better committed here.

`python -m harness.probe_synth` runs the self-check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import PaperDoc, RepoAcquisition


@dataclass
class Plan:
    """One authored probe, ready to be written to `runs/<pid>/probe.py`."""

    mechanism: str
    script: str
    rationale: str
    arms: list[str] = field(default_factory=lambda: ["baseline", "treatment"])
    metric: str = "metric"
    aux_metrics: list[str] = field(default_factory=list)
    matched_on: str = ""
    claim: str = ""
    keeps_finding: bool = True
    """Whether the probe actually tests the audit finding it was dispatched from.

    Kept as a field because a probe that measures the paper's core algorithm is usually
    not measuring the finding that happened to rank first, and filing one under the other
    is a provenance error of exactly the kind these reports exist to catch. Every probe
    the planner now emits is the placebo control, which calibrates the claimed gain in the
    finding it came from — so it keeps it.
    """


# --------------------------------------------------------------------------- #
# Corpus used for template matching
# --------------------------------------------------------------------------- #
def corpus(doc: PaperDoc, acq: RepoAcquisition | None = None, limit: int = 40) -> str:
    """Title, advertised repo and the opening sections, lowercased.

    Only the front of the paper: a template must match on what the work IS, not on a
    related-work paragraph that happens to name someone else's method.
    """
    parts = [doc.title or "", (acq.url if acq else "") or doc.repo_url or ""]
    parts += [f"{sec.title} {sec.text}" for sec in (doc.sections or [])[:limit]]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


# --------------------------------------------------------------------------- #
# The placebo control — the only template, and paper-independent by construction
# --------------------------------------------------------------------------- #
PLACEBO_SCRIPT = '''\
"""Auto-synthesized mechanism probe — placebo-controlled auxiliary term.

Claim under audit: __CLAIM__

The question this isolates: when a paper reports a small gain from adding an auxiliary
objective, how large a gain does an auxiliary objective that targets NOTHING produce
under the same budget? The treatment arm here adds a penalty on the projection of the
representation onto a FIXED RANDOM direction — a term with a gradient but no hypothesis.
If a term with no hypothesis moves the metric by as much as the paper's claimed delta,
then the claimed delta is not evidence for the paper's mechanism; it is evidence that
the metric moves when you perturb the objective at all.

Reported: test accuracy per arm, plus the seed spread, which is the number the paper
under audit does not report at all.

WHAT THIS IS NOT: a reproduction of the paper's benchmark. It is a calibration of how
much an arbitrary auxiliary term buys, on this hardware, at this seed count.

Contract (parsed by harness/local_exec.py — keep these lines):
    SH_DEVICE <cuda|mps|cpu>
    SH_METRIC arm=<name> seed=<int> value=<float>
    SH_AUX key=<name> arm=<name> seed=<int> value=<float>
"""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", type=str, required=True)
args = ap.parse_args()

import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

DEVICE = ("cuda" if torch.cuda.is_available()
          else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
          else "cpu")
print(f"SH_DEVICE {DEVICE}", flush=True)

LAMBDA = 0.01      # placebo strength
STEPS  = __STEPS__
BATCH  = 128
HIDDEN = 128

torch.manual_seed(args.seed)
if DEVICE == "cuda":
    torch.cuda.manual_seed_all(args.seed)

X, y = load_digits(return_X_y=True)
# Split BEFORE scaling: fitting the scaler on the full set would leak test statistics
# into training, which is one of the patterns this harness's static audit flags.
Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=args.seed, stratify=y)
scaler = StandardScaler().fit(Xtr)
Xtr, Xte = scaler.transform(Xtr), scaler.transform(Xte)

xt = torch.tensor(Xtr, dtype=torch.float32, device=DEVICE)
yt = torch.tensor(ytr, dtype=torch.long, device=DEVICE)
xv = torch.tensor(Xte, dtype=torch.float32, device=DEVICE)
yv = torch.tensor(yte, dtype=torch.long, device=DEVICE)

feat = nn.Sequential(nn.Linear(xt.shape[1], HIDDEN), nn.ReLU(),
                     nn.Linear(HIDDEN, HIDDEN), nn.ReLU()).to(DEVICE)
head = nn.Linear(HIDDEN, int(y.max()) + 1).to(DEVICE)

# The placebo direction is fixed per seed and points nowhere in particular.
u = F.normalize(torch.randn(HIDDEN, device=DEVICE), dim=0)
USE_PLACEBO = args.arm.strip().lower() in ("placebo", "treatment")

opt = torch.optim.Adam(list(feat.parameters()) + list(head.parameters()), lr=1e-3)
last_ce = 0.0
for _ in range(STEPS):
    idx = torch.randint(0, xt.shape[0], (BATCH,), device=DEVICE)
    h = feat(xt[idx])
    ce = F.cross_entropy(head(h), yt[idx])
    loss = ce + (LAMBDA * (h @ u).pow(2).mean() if USE_PLACEBO else 0.0)
    opt.zero_grad()
    loss.backward()
    opt.step()
    last_ce = float(ce.detach())

feat.eval()
head.eval()
with torch.no_grad():
    logits = head(feat(xv))
    acc = float((logits.argmax(1) == yv).float().mean())
    nll = float(F.cross_entropy(logits, yv))

print(f"SH_METRIC arm={args.arm} seed={args.seed} value={acc:.6f}", flush=True)
print(f"SH_AUX key=test_nll arm={args.arm} seed={args.seed} value={nll:.6f}", flush=True)
print(f"SH_AUX key=train_ce arm={args.arm} seed={args.seed} value={last_ce:.6f}", flush=True)
'''

PLACEBO_RATIONALE = (
    "No mechanism-specific template matched this paper, so the planner fell back to the "
    "control that is informative for the finding class these audits actually produce: a "
    "small reported gain, from a single run per arm, with no variance stated anywhere. "
    "The placebo arm adds an auxiliary term with a gradient and no hypothesis. What comes "
    "back is how much 'gain' perturbing the objective buys for free, together with the "
    "seed spread on it — which is the quantity the paper under audit omits and the one an "
    "editor needs in order to read its table at all."
)


# --------------------------------------------------------------------------- #
# Matching and planning
# --------------------------------------------------------------------------- #
def plan(doc: PaperDoc, claim: str = "", acq: RepoAcquisition | None = None,
         steps: int = 0) -> Plan:
    """Choose a template and render it. Always returns a runnable plan.

    Falls back to the placebo control rather than to nothing: a paper with a claimed
    gain and no reported variance is exactly the case where the generic control is
    worth more than silence.
    """
    text = corpus(doc, acq)

    # There is deliberately no mechanism dispatch here any more. A hardcoded template for
    # one pilot paper used to be selected by matching that paper's method name — or two of
    # its topic keywords — anywhere in a 40-section corpus, which for a short paper is the
    # whole text, so a single related-work citation of someone else's method was enough to
    # trigger it. That is paper-specific logic in executable production code: the harness
    # behaved differently for one paper in the evaluation corpus than for any other, which
    # is exactly what invariant 10 forbids, and a template written from one paper cannot be
    # evidence about a different one.
    #
    # The generic control is what remains. It measures what an auxiliary term with no
    # hypothesis buys, which is a real and paper-independent quantity, and the provenance
    # ceiling caps whatever it produces at INCONCLUSIVE regardless. Abstaining is the
    # correct outcome for a mechanism this harness cannot author from the paper alone.
    script = PLACEBO_SCRIPT.replace("__STEPS__", str(steps or 4000))
    script = script.replace("__CLAIM__", (claim or "(none supplied)").replace('"""', "'''")[:500])
    return Plan(mechanism="placebo", script=script, rationale=PLACEBO_RATIONALE,
                arms=["baseline", "placebo"], metric="accuracy",
                aux_metrics=["test_nll", "train_ce"], matched_on="no mechanism template matched",
                keeps_finding=True,
                claim=("how large a gain does an auxiliary term with no hypothesis buy, "
                       "against: " + (claim or "(no claim supplied)")[:300]))


if __name__ == "__main__":  # self-check: python -m harness.probe_synth
    import ast
    import pathlib

    from .schema import Section

    def _doc(title: str, body: str) -> PaperDoc:
        return PaperDoc(paper_id="t", title=title, source_path="x", n_pages=1,
                        sections=[Section(section_idx=0, title="Intro", text=body)])

    # No paper-specific dispatch exists any more, so EVERY paper gets the generic
    # control. A template selected by one paper's method name made the harness behave
    # differently for one member of its own evaluation corpus than for any other.
    for title, body in (("LDReg: Local Dimensionality Regularized SSL", "we regularize LID"),
                        ("APT: Adaptive Pruning and Tuning", "we prune language models"),
                        ("SAPG: Split and Aggregate Policy Gradients", "we split and aggregate"),
                        ("Something else", "intrinsic dimensionality and dimensional collapse"),
                        ("A Unified Diverse Weather Generator", "we generate LiDAR point clouds")):
        p_ = plan(_doc(title, body))
        assert p_.mechanism == "placebo", (title, p_.mechanism)
        assert p_.arms == ["baseline", "placebo"]

    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    for token in ("ldreg", "LDReg", "SAPG", "CoFi"):
        assert token not in src.split('if __name__')[0], f"{token} in production code"

    gen = plan(_doc("A Unified Diverse Weather Generator", "we generate LiDAR point clouds"))
    assert gen.keeps_finding, "the placebo probe calibrates the finding's own claimed gain"

    for candidate in (gen,):
        ast.parse(candidate.script)              # every emitted script must be valid Python
        assert "SH_DEVICE" in candidate.script and "SH_METRIC" in candidate.script
        assert "__STEPS__" not in candidate.script, "every placeholder must be substituted"
        for key in candidate.aux_metrics:
            assert f"key={key}" in candidate.script, f"declares {key} but never prints it"
    print("probe_synth self-check OK")
