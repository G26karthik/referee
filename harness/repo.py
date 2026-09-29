"""The paper's own code: which URL is the authors', clone, pin, verify, list.

Attribution is deterministic and burden-of-proof-on-authorship: a URL counts as the
authors' code only outside the reference list and after an availability cue in its own
sentence that gives the code to nobody else. Otherwise, and only with
SH_ALLOW_SOURCE_SEARCH, a public repository whose README opens with the exact title and an
official/first-person statement. The planner may still decline a repo it judges foreign.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import state

_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "huggingface.co")
_URL = re.compile(
    r"https?\s*:\s*/\s*/\s*(?:www\.)?(" + "|".join(re.escape(h).replace(r"\.", r"\s*\.\s*") for h in _HOSTS)
    + r")\s*/\s*((?:[A-Za-z0-9_.]|-\s{0,2})+)\s*/\s*((?:[A-Za-z0-9_.]|-\s{0,2})+)", re.I)
_REFERENCES = re.compile(r"^\s*(?:\d+\.?\s+)?(references|bibliography|works cited)\s*$", re.I | re.M)
# An appendix after the bibliography ("A" / "A Proofs" / "Appendix B") ends the reference span.
_APPENDIX = re.compile(r"^\s*(?:appendix\b[^\n]*|[A-Z]\.?|[A-Z](?:\.\d+)?\.?\s+[A-Z][A-Za-z ]{2,60})\s*$", re.M)
_OWN = (r"our\s+(?:(?:public|publicly\s+available|official|open[- ]source|github|full|complete|"
        r"accompanying)\s+){0,2}(?:code(?:base)?s?|implementation|repositor(?:y|ies)|repo|"
        r"source\s+code|software|toolkit|project\s+page)")
_AVAILABLE = r"(?:available|released|hosted|found|provided|accessible|published)"
_FIRST_PERSON = (
    re.compile(rf"\b{_OWN}\b(?:\s+and\s+\w+)?\s+(?:is|are|will\s+be|can\s+be)\s+(?:\w+\s+)?{_AVAILABLE}\b", re.I),
    re.compile(rf"\b{_AVAILABLE}\s+(?:\w+\s+)?(?:at|in|on|from|via)\s+{_OWN}\b", re.I),
    re.compile(r"\bwe\s+(?:have\s+)?(?:release|open-?source|publish)(?:d)?\s+(?:our|the|all)?\s*"
               r"(?:code|implementation|software|repository|codebase)\b", re.I))
_CUES = ("code is available", "code are available", "code available", "code will be",
         "accessible at", "code and models", "code and data", "open source code", "our code",
         "our implementation", "implementation is available", "source code", "released at",
         "available at", "available here", "code:")
_THIRD_PARTY = re.compile(r"\b(?:baselines?|their|et\s+al|original\s+authors|provided\s+by|we\s+(?:use[ds]?|"
                          r"adopt(?:ed)?|build\s+on|borrow(?:ed)?)|third[- ]party|prior\s+work|existing\s+"
                          r"implementation|builds?\s+(?:up)?on|built\s+on|extends?|extending|based\s+on|"
                          r"codebase\s+of|implementation\s+of|fork(?:ed)?\s+(?:of|from)|adapted\s+from)\b", re.I)
_NOT_AUTHORS = re.compile(r"\b(unofficial|non-?official|re-?implementation|reproducibility challenge|"
                          r"replication of|port of|third[- ]party|not affiliated|my (?:own )?implementation|"
                          r"not (?:the )?official|(?:code|implementation) (?:has|have|is|are) not "
                          r"(?:yet )?(?:been )?released)\b", re.I)
DATA_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet", ".npy", ".npz", ".pkl", ".h5",
                 ".hdf5", ".xlsx", ".feather", ".arrow")
DOC_NAMES = re.compile(r"(readme[^/]*|[^/]+\.md|[^/]+\.sh|makefile|[^/]+\.ya?ml|[^/]+\.txt)$", re.I)


def _cued(sentence: str) -> tuple[bool, bool]:
    """(cued, first_person) for the text before a URL in its own sentence."""
    ends = [m.end() for p in _FIRST_PERSON for m in p.finditer(sentence)]
    first = bool(ends) and not _THIRD_PARTY.search(sentence[max(ends):])
    return first or (any(c in sentence.lower() for c in _CUES) and not _THIRD_PARTY.search(sentence)), first


def candidates(pages: list[str]) -> list[dict]:
    """Every repository URL in the paper, each with its sentence and attribution facts."""
    text = "\n".join(pages)
    lo = m.end() if (m := _REFERENCES.search(text)) else len(text)
    hi = m.start() if (m := _APPENDIX.search(text, lo)) else len(text)
    out: dict[str, dict] = {}
    for m in _URL.finditer(text):
        owner, name = (re.sub(r"-\s+", "-", g) for g in (m.group(2), m.group(3)))
        name = name.rstrip(".,;:)]}").removesuffix(".git")
        if not owner.strip(".,;:)]}") or not name:
            continue
        host = re.sub(r"\s+", "", m.group(1)).lower()
        url = f"https://{host}/{owner.strip('.,;:)]}')}/{name}"
        window = text[max(0, m.start() - 140):m.start()]
        cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("; "))
        sentence = window[cut + 1:] if cut >= 0 else window
        cued, first = _cued(sentence)
        c = {"url": url, "in_references": lo <= m.start() < hi,
             "cued": cued, "first_person": first,
             "sentence": re.sub(r"\s+", " ", text[max(0, m.start() - 160):m.end() + 20]).strip()}
        out.setdefault(url, c)
        if cued and not out[url]["cued"]:
            out[url] = c
    return list(out.values())


def official(cands: list[dict]) -> dict | None:
    """The authors' own repository: cued, outside the references; first-person first."""
    ok = [c for c in cands if c["cued"] and not c["in_references"]]
    return sorted(ok, key=lambda c: not c["first_person"])[0] if ok else None


def search(cfg: state.Config, title: str) -> tuple[str, str]:
    """(url, evidence) for a public repo whose README opens with this exact title and an
    official/first-person statement; ('', why) otherwise. Sends only the title."""
    import urllib.parse
    import urllib.request

    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    if not (cfg.allow_network and cfg.allow_source_search):
        return "", "public source search is off (SH_ALLOW_SOURCE_SEARCH)"
    if len(norm(title).split()) < 4:
        return "", f"the title {title!r} is too short to search unambiguously"

    def get(url, accept="application/vnd.github+json"):
        req = urllib.request.Request(url, headers={"User-Agent": "referee", "Accept": accept})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    try:
        q = urllib.parse.quote(f'"{title}" in:readme')
        items = json.loads(get(f"https://api.github.com/search/repositories?q={q}&per_page=5")).get("items") or []
    except (OSError, ValueError) as e:
        return "", f"public source search failed: {e}"
    for item in items[:5]:       # ponytail: an exact-title README ranks in the top 5
        try:
            readme = get(f"https://api.github.com/repos/{item['full_name']}/readme",
                         "application/vnd.github.raw").decode("utf-8", "replace")
        except OSError:
            continue
        body = norm(re.sub(r"```[^`]{0,8000}```|@\w+\s*\{[^@]{0,4000}?\n\s*\}", " ", readme))
        at = body.find(norm(title))
        if 0 <= at <= 1500 and not _NOT_AUTHORS.search(readme[:4000]) and re.search(
                r"\b(official|our paper|our work|our method|we propose|we introduce)\b",
                body[max(0, at - 300):at + len(norm(title)) + 300]):
            return f"https://github.com/{item['full_name']}", "README opens with the exact title and an authorship statement"
    return "", "no public README opens with the exact title and an authorship statement"


def git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    safe = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull]   # a checkout configures nothing on the host
    try:
        p = subprocess.run(["git", *safe, *args], cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 128, str(e)


def acquire(cfg: state.Config, pid: str, meta: dict) -> dict:
    """Find, clone and pin the paper's own repository; writes `source.json`."""
    root = state.pdir(cfg, pid)
    cands = candidates(meta["pages"])
    own = official(cands)
    src = {"candidates": cands, "url": "", "discovered_by": "", "evidence": "", "status": "none"}
    if own:
        src.update(url=own["url"], discovered_by="paper", evidence=own["sentence"])
    else:
        url, why = search(cfg, meta["title"])
        src.update(url=url, discovered_by="search" if url else "", evidence=why)
    if src["url"]:
        dest = root / "repo"
        if not (dest / ".git").is_dir():
            if not cfg.allow_network:
                src.update(status="blocked", evidence=src["evidence"] + "; network gate closed")
            else:
                rc, out = git(["clone", "--depth", "1", src["url"], str(dest)])
                src["status"] = "cloned" if rc == 0 else "failed"
                if rc:
                    src["reason"] = out.strip()[-300:]
        if (dest / ".git").is_dir():
            src["status"] = "cloned"
            src["commit"] = git(["rev-parse", "HEAD"], dest)[1].strip()
    state.write_json(root / "source.json", src)
    return src


def restore(cfg: state.Config, root: Path) -> None:
    """A packed project's checkout (pack --clean deletes clones), re-cloned at its RECORDED
    commit, detached; verify_commit still gates every run."""
    src, dest = state.read_json(root / "source.json", {}) or {}, root / "repo"
    if not (src.get("url") and src.get("commit")) or (dest / ".git").is_dir() or not cfg.allow_network:
        return
    if git(["clone", "--no-checkout", src["url"], str(dest)])[0] == 0:
        git(["checkout", "--detach", "-q", src["commit"]], dest)


def verify_commit(path: Path, expected: str) -> tuple[bool, str]:
    """Is the checkout about to run exactly the pinned commit, with nothing modified or
    hidden? Fails closed: an uninspectable tree is not a clean tree."""
    if not expected or not (path / ".git").is_dir():
        return False, "no pinned commit or no checkout (a .git file is a redirect, refused)"
    rc, head = git(["rev-parse", "HEAD"], path, 30)
    if rc or head.strip() != expected:
        return False, f"the checkout is at {head.strip()[:12]}, not the pinned {expected[:12]}"
    rc, ls = git(["ls-files", "-v"], path, 60)
    hidden = [ln[2:] for ln in ls.splitlines() if ln[:1].islower() or ln[:1] == "S"]
    if rc or hidden:
        return False, f"tracked paths hide their changes (assume-unchanged/skip-worktree): {hidden[:3]}"
    rc, st = git(["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"], path, 60)
    if rc:
        return False, f"git status failed, so the tree cannot be shown clean: {st.strip()[:200]}"
    if st.strip():
        return False, f"the tree differs from the pinned commit: {st.split()[1:4]}"
    return True, f"clean checkout of {expected[:12]}"


def released(path: Path) -> list[dict]:
    """Data files of the checkout with sha256 at pin time."""
    # ponytail: 1000 files / 2 GB each; past that only the first 1000 (sorted) are listed.
    out = []
    files = sorted(f for f in path.rglob("*") if f.is_file() and f.suffix.lower() in DATA_SUFFIXES
                   and ".git" not in f.relative_to(path).parts)[:1000]
    for f in files:
        if f.stat().st_size > 2 << 30:
            continue
        first = ""
        if f.suffix.lower() in (".csv", ".tsv", ".json", ".jsonl"):
            with f.open(encoding="utf-8", errors="replace") as fh:
                first = "\n".join(fh.readline().rstrip("\r\n")[:2000 if i == 0 else 400] for i in range(4)).strip()
        out.append({"path": f.relative_to(path).as_posix(), "bytes": f.stat().st_size,
                    "sha256": state.sha256(f.read_bytes()), "head": first})
    return out


def listing(path: Path, data: list[dict]) -> str:
    """What a planner needs to see of a checkout: its tree, docs, and released data by
    directory (one row per directory, one preview each)."""
    # ponytail: 400 tree entries, 12k chars of data previews; beyond that "+N more".
    files = sorted(p.relative_to(path).as_posix() for p in path.rglob("*")
                   if p.is_file() and ".git" not in p.relative_to(path).parts)
    tree = "\n".join(files[:400]) + (f"\n... +{len(files) - 400} more files" if len(files) > 400 else "")
    by_dir: dict[str, list[dict]] = {}
    for d in data:
        by_dir.setdefault(d["path"].rpartition("/")[0], []).append(d)
    rows, left = [], 12_000
    for d, fs in by_dir.items():
        head = fs[0]["head"].replace("|", "/").replace("\n", " <br> ")
        head, left = (head, left - len(head)) if len(head) <= left else ("(no preview: budget spent)", left)
        names = ", ".join(f["path"].rpartition("/")[2] for f in fs[:60]) + (f", +{len(fs) - 60} more" if len(fs) > 60 else "")
        rows.append(f"| `{d or '.'}/` | {len(fs)} | {names} | {head} |")
    table = ("| directory | files | names | header + first rows of its first file |\n|---|---|---|---|\n"
             + "\n".join(rows)) if rows else "(no data files)"
    return f"FILES:\n{tree}\n\nRELEASED DATA FILES:\n{table}"
