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

from .artifacts import PaperDoc, RepoAcquisition


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

    A mechanism template tests the paper's CORE ALGORITHM, which is usually not the
    finding that happened to rank first — LDReg's probe measures intrinsic
    dimensionality, and the top-ranked settleable finding for that paper is about
    wall-clock hours. Carrying the finding id through anyway would file a measurement
    of one thing under a claim about another, which is a provenance error of exactly
    the kind these reports exist to catch. The placebo template is the opposite case:
    it calibrates the claimed gain in the finding, so it keeps it.
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
# Template 1 — LDReg (ICLR 2024), Algorithms 1 and 2
# --------------------------------------------------------------------------- #
LDREG_SCRIPT = '''\
"""Auto-synthesized mechanism probe — LDReg, Algorithms 1 and 2.

Reimplements, from the paper's own formulation:

  Algorithm 1  lid_mom_est(data, reference, k)  — method-of-moments LID estimator,
               LID = m / (w - m), where m is the mean of the k nearest neighbour
               distances and w is the k-th (largest of those) distance.
  Algorithm 2  L_L1 = -beta * (1/N) * sum_i ln LID_i, added to the NT-Xent objective
               and applied to the REPRESENTATION, not the projector output.

Question under test (two parts, both answered on stdout):
  1. Does LDReg actually prevent local intrinsic dimensionality collapse?
     -> arm `ldreg` mean LID vs arm `baseline` mean LID, on held-out data.
  2. Is that shift larger than this machine's 2-sigma seed noise?
     -> the harness computes the paired seed spread and answers it.

WHAT THIS IS NOT: this is a toy contrastive setup on synthetic data. It cannot and does
not reproduce any ImageNet number in the paper. It tests whether the stated mechanism
does the thing the paper says it does.

Every constant below is pre-registered from the paper. Nothing was tuned against the
output of this script.

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

DEVICE = ("cuda" if torch.cuda.is_available()
          else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
          else "cpu")
print(f"SH_DEVICE {DEVICE}", flush=True)

# --- pre-registered constants ---------------------------------------------------
BETA        = __BETA__   # paper's LDReg strength for SimCLR
K_NN        = __K__      # paper sweeps k in {32,64,128,256}; 32 is the smallest, and the
                         # only one a 256-sample batch can support
TEMP        = 0.5        # NT-Xent temperature, SimCLR default
BATCH       = 256
STEPS       = __STEPS__
D_INTRINSIC = 12         # the true manifold dimension the estimator should recover
D_AMBIENT   = 64
N_TRAIN     = 4096
N_EVAL      = 1024
N_CLASS     = 8

torch.manual_seed(args.seed)
if DEVICE == "cuda":
    torch.cuda.manual_seed_all(args.seed)
gen = torch.Generator().manual_seed(args.seed)

# --- data: a D_INTRINSIC manifold embedded nonlinearly in D_AMBIENT dims ----------
W1 = torch.randn(D_INTRINSIC, 32, generator=gen)
W2 = torch.randn(32, D_AMBIENT, generator=gen)
centers = torch.randn(N_CLASS, D_INTRINSIC, generator=gen) * 2.5


def manifold(n):
    y = torch.randint(0, N_CLASS, (n,), generator=gen)
    z = centers[y] + torch.randn(n, D_INTRINSIC, generator=gen)
    return (torch.tanh(z @ W1) @ W2).to(DEVICE), y.to(DEVICE)


x_train, y_train = manifold(N_TRAIN)
x_eval, y_eval = manifold(N_EVAL)


def augment(x):
    """Positive-pair view: additive noise plus random feature dropout."""
    return (x + torch.randn_like(x) * 0.35) * (torch.rand_like(x) > 0.15).float()


# --- Algorithm 1 -----------------------------------------------------------------
def lid_mom_est(data, reference, k):
    """Method-of-moments LID. `data` and `reference` equal => column 0 is the self
    distance and is skipped, which is what the paper's batch-as-reference does."""
    k = min(k, reference.shape[0] - 2)
    dist = torch.cdist(torch.flatten(data, 1), torch.flatten(reference, 1), p=2)
    a, _ = torch.sort(dist, dim=1)
    m = a[:, 1:k].mean(dim=1)
    w = a[:, k]
    return m / (w - m + 1e-12)


# --- Algorithm 2 -----------------------------------------------------------------
def ldreg_loss(h, k, beta):
    """L_L1 = -beta * (1/N) * sum ln LID. Minimizing it pushes LID up."""
    return -beta * torch.log(lid_mom_est(h, h, k).clamp_min(1e-6)).mean()


def nt_xent(z1, z2, temp):
    b = z1.shape[0]
    z = F.normalize(torch.cat([z1, z2], 0), dim=1)
    sim = z @ z.t() / temp
    sim.fill_diagonal_(-1e9)
    target = torch.cat([torch.arange(b, 2 * b), torch.arange(0, b)]).to(z.device)
    return F.cross_entropy(sim, target)


backbone = nn.Sequential(nn.Linear(D_AMBIENT, 256), nn.ReLU(),
                         nn.Linear(256, 256), nn.ReLU(),
                         nn.Linear(256, 128)).to(DEVICE)
projector = nn.Sequential(nn.Linear(128, 128), nn.ReLU(), nn.Linear(128, 64)).to(DEVICE)

USE_LDREG = args.arm.strip().lower() in ("ldreg", "treatment")
opt = torch.optim.Adam(list(backbone.parameters()) + list(projector.parameters()), lr=1e-3)

last_ntx = 0.0
for step in range(STEPS):
    idx = torch.randint(0, N_TRAIN, (BATCH,), device=DEVICE)
    xb = x_train[idx]
    h1, h2 = backbone(augment(xb)), backbone(augment(xb))
    loss_ntx = nt_xent(projector(h1), projector(h2), TEMP)
    loss = loss_ntx + (ldreg_loss(torch.cat([h1, h2], 0), K_NN, BETA) if USE_LDREG else 0.0)
    opt.zero_grad()
    loss.backward()
    opt.step()
    last_ntx = float(loss_ntx.detach())

# --- evaluation: LID of the held-out REPRESENTATION -------------------------------
backbone.eval()
with torch.no_grad():
    h_eval = backbone(x_eval)
    h_train_f = backbone(x_train)
    lid = lid_mom_est(h_eval, h_eval, K_NN)
    lid_mean = float(lid.mean())
    lid_median = float(lid.median())

# --- linear probe on the frozen representation ------------------------------------
probe = nn.Linear(h_train_f.shape[1], N_CLASS).to(DEVICE)
popt = torch.optim.Adam(probe.parameters(), lr=1e-2)
for _ in range(300):
    popt.zero_grad()
    F.cross_entropy(probe(h_train_f.detach()), y_train).backward()
    popt.step()
with torch.no_grad():
    acc = float((probe(h_eval).argmax(1) == y_eval).float().mean())

print(f"SH_METRIC arm={args.arm} seed={args.seed} value={lid_mean:.6f}", flush=True)
print(f"SH_AUX key=lid_median arm={args.arm} seed={args.seed} value={lid_median:.6f}", flush=True)
print(f"SH_AUX key=nt_xent_loss arm={args.arm} seed={args.seed} value={last_ntx:.6f}", flush=True)
print(f"SH_AUX key=linear_probe_acc arm={args.arm} seed={args.seed} value={acc:.6f}", flush=True)
print(f"SH_AUX key=true_intrinsic_dim arm={args.arm} seed={args.seed} value={float(D_INTRINSIC):.6f}",
      flush=True)
'''

LDREG_RATIONALE = (
    "The paper's central mechanism is that maximizing the geometric mean of local "
    "intrinsic dimensionality prevents dimensional collapse in self-supervised "
    "representations. That is a claim about the ALGORITHM, and it is separable from the "
    "ImageNet linear-probe numbers the paper reports: it can be tested at toy scale in "
    "seconds by implementing Algorithm 1 and Algorithm 2 as printed and running the "
    "regularized objective against the unregularized one on a manifold whose true "
    "intrinsic dimension is known by construction. This probe therefore answers the "
    "mechanistic question the paper's tables cannot, precisely because the tables report "
    "single runs with no seed spread: it measures the LID shift AND the seed noise on "
    "that shift, so the effect can be graded against a real detectability band."
)


# --------------------------------------------------------------------------- #
# Template 2 — the placebo control, for any paper claiming a small gain
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
def _ldreg_match(text: str) -> str:
    """A conservative match. Two independent signals, not one keyword."""
    if "ldreg" in text:
        return "the paper names LDReg"
    lid = ("local intrinsic dimension" in text or "intrinsic dimensionality" in text)
    if lid and "dimensional collapse" in text:
        return "local intrinsic dimensionality + dimensional collapse"
    return ""


def plan(doc: PaperDoc, claim: str = "", acq: RepoAcquisition | None = None,
         steps: int = 0) -> Plan:
    """Choose a template and render it. Always returns a runnable plan.

    Falls back to the placebo control rather than to nothing: a paper with a claimed
    gain and no reported variance is exactly the case where the generic control is
    worth more than silence.
    """
    text = corpus(doc, acq)

    if why := _ldreg_match(text):
        script = LDREG_SCRIPT
        for key, value in (("__BETA__", "0.01"), ("__K__", "32"),
                           ("__STEPS__", str(steps or 3000))):
            script = script.replace(key, value)
        return Plan(mechanism="ldreg", script=script, rationale=LDREG_RATIONALE,
                    arms=["baseline", "ldreg"], metric="mean_lid",
                    aux_metrics=["lid_median", "nt_xent_loss", "linear_probe_acc",
                                 "true_intrinsic_dim"],
                    matched_on=why, keeps_finding=False,
                    claim=("LDReg's stated mechanism: does adding L_L1 = -beta*(1/N)*sum ln LID "
                           "to NT-Xent raise the local intrinsic dimensionality of the learned "
                           "representation, and is that shift larger than seed noise?"))

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

    from .artifacts import Section

    def _doc(title: str, body: str) -> PaperDoc:
        return PaperDoc(paper_id="t", title=title, source_path="x", n_pages=1,
                        sections=[Section(section_idx=0, title="Intro", text=body)])

    ld = plan(_doc("LDReg: Local Dimensionality Regularized SSL", "we regularize LID"))
    assert ld.mechanism == "ldreg", ld.matched_on
    assert ld.arms == ["baseline", "ldreg"]
    assert "lid_mom_est" in ld.script and "ln LID" in ld.script

    two = plan(_doc("Something else", "we study intrinsic dimensionality and dimensional collapse"))
    assert two.mechanism == "ldreg", "two independent signals should match without the name"

    gen = plan(_doc("A Unified Diverse Weather Generator", "we generate LiDAR point clouds"))
    assert gen.mechanism == "placebo", gen.matched_on
    assert gen.arms == ["baseline", "placebo"]

    near = plan(_doc("On intrinsic dimensionality", "we measure intrinsic dimensionality only"))
    assert near.mechanism == "placebo", "one signal alone must not match a specific template"

    assert not ld.keeps_finding, "the LID probe does not test whichever finding ranked first"
    assert gen.keeps_finding, "the placebo probe calibrates the finding's own claimed gain"
    assert "LID" in ld.claim and ld.claim != "", "a mechanism probe states its own claim"

    for candidate in (ld, gen):
        ast.parse(candidate.script)              # every emitted script must be valid Python
        assert "SH_DEVICE" in candidate.script and "SH_METRIC" in candidate.script
        assert "__STEPS__" not in candidate.script, "every placeholder must be substituted"
        for key in candidate.aux_metrics:
            assert f"key={key}" in candidate.script, f"declares {key} but never prints it"
    print("probe_synth self-check OK")
