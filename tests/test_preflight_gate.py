"""A batch's paper count is checked BEFORE it is spent — invariant 35.

For two revisions this was honest and half-closed. `harness/preflight.py` could answer,
from the PDFs' bytes, whether an N-file request was N distinct documents; it was reachable
only as `run.py preflight`, and `controller.review_papers` never called it. So a batch
submitted straight to `review` was not refused: `allocate_paper_id` correctly resumed the
duplicate's project — right per paper and invisible in aggregate — and an eight-file
request that was seven documents produced seven reviews and a claim about eight papers.

`corpus.account`'s conservation law cannot catch that. It asserts every REQUESTED paper
reaches exactly one terminal state, and the duplicate never becomes a second case to
conserve; it is the same case, reached twice, agreeing with itself.

What these pin is the gate and the two things it must NOT do: refuse two DIFFERENT papers
that happen to slugify to the same id, and spend anything before it refuses.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness import controller, preflight
from harness.config import Config

_PDF = Path(__file__).resolve().parents[1] / "papers" / "CVPR.pdf"
_OTHER = Path(__file__).resolve().parents[1] / "papers" / "ICLR.pdf"

pytestmark = pytest.mark.skipif(
    not (_PDF.exists() and _OTHER.exists()),
    reason="needs two real PDFs; preflight reads bytes and has nothing to read without them")


@pytest.fixture()
def batch(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


def _copy(dst: Path, src: Path = _PDF) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return str(dst)


def test_the_same_document_twice_refuses_the_batch(batch: Config, tmp_path: Path):
    """The defect, reproduced: two files, one document, and a claim about two papers."""
    a = _copy(tmp_path / "one.pdf")
    b = _copy(tmp_path / "two.pdf")

    out = controller.review_papers(batch, [a, b])

    assert "error" in out, "a batch that is not N documents must not silently become N-1"
    assert out["preflight"]["requested"] == 2
    assert out["preflight"]["distinct_documents"] == 1
    assert "two.pdf" in out["error"] and "one.pdf" in out["error"], (
        "the refusal must NAME the files; 'this batch is invalid' is not actionable")


def test_nothing_is_spent_before_the_refusal(batch: Config, tmp_path: Path):
    """The check reads bytes. A refusal that had already ingested a paper would have
    spent the expensive half of what it exists to protect."""
    a = _copy(tmp_path / "one.pdf")
    b = _copy(tmp_path / "two.pdf")

    out = controller.review_papers(batch, [a, b])

    assert out["results"] == [] and out["completed"] == 0
    assert not (batch.projects_dir.exists() and any(batch.projects_dir.iterdir())), (
        "no project directory may be allocated for a batch that was refused")


def test_two_different_papers_are_not_refused(batch: Config, tmp_path: Path):
    """The gate must not become a reason a real batch cannot run.

    Only a genuine DUPLICATE DOCUMENT blocks. Two different papers pass, whatever their
    filenames look like — including the case invariant 35 calls out, where two distinct
    documents slugify to the same paper id and are reported with their distinct ids.
    """
    a = _copy(tmp_path / "first.pdf", _PDF)
    b = _copy(tmp_path / "second.pdf", _OTHER)

    got = preflight.check(batch, [a, b])

    assert got["ok"], got["blocking"]
    assert got["requested"] == 2 and got["distinct_documents"] == 2


def test_a_batch_that_passes_carries_its_own_count(batch: Config, tmp_path: Path):
    """`distinct_documents` is the number a corpus claim about this batch may use, and it
    is carried on the result whether or not the gate blocked — a batch that passed should
    be able to SHOW it did, rather than leaving a reader to infer it from an absence."""
    a = _copy(tmp_path / "first.pdf", _PDF)
    b = _copy(tmp_path / "second.pdf", _OTHER)

    got = preflight.check(batch, [a, b])

    assert set(got) >= {"requested", "distinct_documents", "distinct_paper_ids", "ok"}
    assert got["distinct_documents"] <= got["requested"], (
        "more documents than files is arithmetically impossible and would mean the "
        "counter is measuring something else")


def test_an_unreadable_input_does_not_refuse_the_batch(batch: Config, tmp_path: Path):
    """The narrowing that matters, and the regression that taught it.

    `preflight` blocks on two states and only ONE of them may stop a batch here. An
    UNREADABLE input — a path that is not there, or a bare case id, both of which
    `review_papers` legitimately accepts — is a PER-PAPER failure, and `corpus.account`
    already reports it with its own `failure_kind`; invariant 13 exists so that "6
    requested, 5 completed, 1 failed" is sayable. Refusing the batch for it threw away
    five good reviews over one bad file, which is what the first version of this gate did.

    A duplicate is different in kind: it corrupts the COUNT rather than one entry, because
    the duplicate never becomes a second case to account for.
    """
    real = _copy(tmp_path / "real.pdf")

    out = controller.review_papers(
        batch, [real, str(tmp_path / "definitely_not_here.pdf"), "not-a-case-id"],
        skip_probe=True)

    assert "error" not in out, "one unreadable input may not cost the others their review"
    assert out["corpus"]["requested"] == 3, (
        "and every requested paper must still reach the accounting, which is the only "
        "place a reader can see that one of them failed")


def test_the_operator_command_still_exists(batch: Config, tmp_path: Path):
    """Wiring it in did not remove it. Asking before spending is the point of `run.py
    preflight`, and an operator who wants the table without starting a review must keep
    being able to get it."""
    a = _copy(tmp_path / "one.pdf")

    got = preflight.run(batch, [a], out=tmp_path / "pre")

    assert got["requested"] == 1 and got["ok"]
    md = tmp_path / "pre" / "preflight.md"
    assert md.exists() and (tmp_path / "pre" / "preflight.json").exists(), (
        "the command writes BOTH — a table for a human and a record for a script")
    assert "distinct document" in md.read_text(encoding="utf-8")
