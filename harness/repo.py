"""S3a — find the paper's code, get it, and work out how to run it.

Everything here is gated. Cloning a repository and resolving its dependencies means
fetching code from the internet and, later, executing it -- a different risk class from
every other, otherwise fully offline, stage in this harness. So `Config.allow_network`
and `Config.allow_install` both default to False and each function returns a `blocked`
status rather than doing anything when its gate is shut. A blocked acquisition is not a
finding about the paper -- it is a fact about this run, and the report says so.

URL extraction is deliberately conservative. PDF text mangles links ("Code is available:
https: //github.com/...", a footnote marker fused onto a citation URL, a typesetter's
line break landing inside a hyphenated owner name or inside the host literal itself), so
proximity to an availability cue is scored rather than taking the first match -- a
related-work footnote routinely points at someone else's repository, and cloning that
would audit the wrong project entirely.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable, Protocol

from .schema import CommitVerification, ExecCapability, PaperDoc, RepoAcquisition
from .config import Config

# Hosts worth cloning. Anything else (project pages, personal sites) is not a repo.
_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "huggingface.co")


def _host_pattern(host: str) -> str:
    """A host literal that tolerates PDF-inserted whitespace around each dot, the same
    treatment as `\\s*` around `://` and `/`."""
    return re.escape(host).replace(r"\.", r"\s*\.\s*")


_URL = re.compile(
    r"https?\s*:\s*/\s*/\s*(?:www\.)?(" + "|".join(_host_pattern(h) for h in _HOSTS) + r")"
    r"\s*/\s*((?:[A-Za-z0-9_.]|-\s{0,2})+)\s*/\s*((?:[A-Za-z0-9_.]|-\s{0,2})+)",
    re.IGNORECASE,
)
# Phrases that mark a link as THIS paper's code rather than a citation to someone else's.
_CUES = (
    "code is available", "code are available", "code available", "code will be",
    "code is accessible", "code are accessible", "accessible at",
    "code and models", "code and data", "project is available", "project page",
    "open source code", "our code", "our implementation", "implementation is available",
    "source code", "codebase is", "released at", "available at", "available here", "code:",
)
_CUE_WINDOW = 140          # chars before the URL searched for a cue
# Section headings whose contents are citations to other people's work, never this
# paper's own artifacts.
_REFERENCE_HEADING = re.compile(r"^(?:\d+\.?\s+)?(references|bibliography|works cited)\b", re.I)
_DEP_FILES = ("requirements.txt", "environment.yml", "environment.yaml", "pyproject.toml",
              "setup.py", "setup.cfg", "requirements-dev.txt", "requirements/base.txt")
# Import name -> the framework label the report shows. Order is significance, not preference.
_FRAMEWORKS = {
    "torch": "pytorch", "pytorch": "pytorch", "torchvision": "pytorch", "pytorch-lightning": "pytorch",
    "lightning": "pytorch", "tensorflow": "tensorflow", "tensorflow-gpu": "tensorflow",
    "keras": "tensorflow", "jax": "jax", "jaxlib": "jax", "flax": "jax", "optax": "jax",
    "transformers": "huggingface", "diffusers": "huggingface", "datasets": "huggingface",
    "accelerate": "huggingface", "scikit-learn": "sklearn", "sklearn": "sklearn",
    "sympy": "sympy", "numpy": "numpy", "scipy": "scipy", "pandas": "pandas",
    "xgboost": "xgboost", "lightgbm": "lightgbm", "mamba-ssm": "pytorch", "timm": "pytorch",
}
# Scripts a reviewer would try first, best candidate first.
_ENTRY_CANDIDATES = (
    "eval.py", "evaluate.py", "evaluation.py", "test.py", "inference.py", "infer.py",
    "predict.py", "demo.py", "main.py", "run.py", "train.py",
)
_ENTRY_DIRS = ("", "scripts", "tools", "src", "experiments")


# --------------------------------------------------------------------------- #
# URL extraction
# --------------------------------------------------------------------------- #
def _normalize(host: str, owner: str, name: str) -> str:
    """Rebuild a canonical clone URL from the three captured pieces, so the whitespace
    PDF extraction injects never reaches the output (including a typesetter's line break
    inserted mid-identifier at a real hyphen: `hassan- mahmood` -> `hassan-mahmood`)."""
    host = re.sub(r"\s+", "", host)
    name = re.sub(r"-\s+", "-", name)
    owner = re.sub(r"-\s+", "-", owner)
    name = name.rstrip(".,;:)]}")               # sentence punctuation glued to the path
    if name.lower().endswith(".git"):
        name = name[:-4]
    owner = owner.strip(".,;:)]}")
    if not owner or not name:
        return ""
    return f"https://{host.lower()}/{owner}/{name}"


def _score(text: str, start: int, host: str, url: str) -> int:
    """How likely is this the paper's OWN code? Higher is better."""
    window = text[max(0, start - _CUE_WINDOW):start].lower()
    score = 0
    if any(c in window for c in _CUES):
        score += 3
    if host.lower() == "github.com":
        score += 1
    # A footnote marker fused to the URL signs a citation to someone else's repository.
    if start > 0 and text[start - 1].isdigit():
        score -= 2
    if "/blob/" in url or "/tree/" in url:
        score -= 1
    return score


# Authorship stated in any phrasing: "available in our public repository:", "our code is
# publicly available at", "we release our code at". First person, the thing owned is CODE,
# only a whitelisted adjective in between, and an availability verb: "our pipeline uses the
# implementation provided by the original authors" owns nothing it links.
_OWN_CODE = (r"our\s+(?:(?:public|publicly\s+available|official|open[- ]source|github|full|"
             r"complete|accompanying)\s+){0,2}(?:code(?:base)?s?|implementation|"
             r"repositor(?:y|ies)|repo|source\s+code|software|toolkit|project\s+page)")
_AVAILABLE = r"(?:available|released|hosted|found|provided|accessible|published)"
_CUE_PATTERNS = (
    re.compile(rf"\b{_OWN_CODE}\b(?:\s+and\s+\w+)?\s+(?:is|are|will\s+be|can\s+be)\s+"
               rf"(?:\w+\s+)?{_AVAILABLE}\b", re.I),
    re.compile(rf"\b{_AVAILABLE}\s+(?:\w+\s+)?(?:at|in|on|from|via)\s+{_OWN_CODE}\b", re.I),
    re.compile(r"\bwe\s+(?:have\s+)?(?:release|open-?source|publish)(?:d)?\s+(?:our|the|all)?\s*"
               r"(?:code|implementation|software|repository|codebase)\b", re.I),
)


def _cued(text: str, start: int) -> bool:
    """Does an availability or authorship phrase precede this URL?"""
    window = text[max(0, start - _CUE_WINDOW):start]
    low = window.lower()
    return any(c in low for c in _CUES) or any(p.search(window) for p in _CUE_PATTERNS)


def _reference_spans(doc: PaperDoc, joiner: int = 1) -> list[tuple[int, int]]:
    """Character ranges of the bibliography, in the same concatenation `find_repo_urls`
    scans. A URL inside the reference list is a citation to somebody else's artifact by
    construction, however official it looks."""
    spans, pos = [], 0
    for s in doc.sections:
        end = pos + len(s.text)
        if _REFERENCE_HEADING.match((s.title or "").strip()):
            spans.append((pos, end))
        pos = end + joiner
    return spans


def find_repo_urls(doc: PaperDoc) -> list[str]:
    """Every candidate repository URL in the paper, most-likely-official first."""
    text = "\n".join(s.text for s in doc.sections)
    best: dict[str, tuple[int, int]] = {}
    for m in _URL.finditer(text):
        host = re.sub(r"\s+", "", m.group(1))
        url = _normalize(host, m.group(2), m.group(3))
        if not url:
            continue
        rank = (_score(text, m.start(), host, m.group(0)), -m.start())
        if url not in best or rank > best[url]:
            best[url] = rank
    return [u for u, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)]


def official_repo_url(doc: PaperDoc) -> str:
    """The repository THIS paper advertises as its own, or '' when it advertises none.

    Separate from `find_repo_urls`, which answers the diagnostic "what repository URLs
    appear in this paper" and should stay complete. This one answers "which repository
    may the harness clone and audit AS THIS PAPER'S CODE", and the burden of proof runs
    the other way: absent positive evidence of authorship, the answer is none.

    Two requirements, both necessary: an availability cue must precede the URL (a bare
    link is not a claim of authorship), and the URL must not sit in the reference list
    (where every link is a citation to someone else's work).
    """
    text = "\n".join(s.text for s in doc.sections)
    refs = _reference_spans(doc)
    best: dict[str, tuple[int, int]] = {}
    for m in _URL.finditer(text):
        url = _normalize(m.group(1), m.group(2), m.group(3))
        if not url or not _cued(text, m.start()):
            continue
        if any(lo <= m.start() < hi for lo, hi in refs):
            continue
        rank = (_score(text, m.start(), m.group(1), m.group(0)), -m.start())
        if url not in best or rank > best[url]:
            best[url] = rank
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[0][0] if ranked else ""


# --------------------------------------------------------------------------- #
# Sources: what this paper IS (identifiers, version) and where its code/data live
# --------------------------------------------------------------------------- #
_ARXIV_ID = re.compile(r"arXiv\s*:\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
_OPENREVIEW_ID = re.compile(r"openreview\s*\.\s*net\s*/\s*(?:forum|pdf)\s*\?\s*id\s*=\s*([A-Za-z0-9_\-]{6,})", re.I)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+[A-Za-z0-9])")
_DATA_HOSTS = re.compile(
    r"https?\s*:\s*/\s*/\s*(?:www\.)?((?:zenodo\.org|figshare\.com|osf\.io|kaggle\.com|"
    r"huggingface\.co/datasets|archive\.ics\.uci\.edu|dataverse\.[a-z.]+|physionet\.org|"
    r"data\.mendeley\.com)[^\s)\]}>,;\"]*)", re.I)
_README_AUTHORSHIP = re.compile(
    r"\b(official|our paper|our work|our method|we propose|we introduce)\b", re.I)


def _front_matter(doc: PaperDoc) -> str:
    text = "\n".join(s.text for s in doc.sections[:2])
    cut = re.search(r"\babstract\b", text, re.I)
    return text[:cut.start()] if cut else text[:800]


def paper_identifiers(doc: PaperDoc) -> dict[str, str]:
    """The paper's own identifiers as printed in it — never looked up elsewhere."""
    body = "\n".join(s.text for s in doc.sections[:max(1, doc.body_end_section_idx)]
                     ) if doc.body_end_section_idx > 0 else "\n".join(s.text for s in doc.sections)
    front = "\n".join(s.text for s in doc.sections[:3])
    out = {"title": doc.title, "pdf_sha256_12": doc.content_sha, "source_path": doc.source_path}
    try:
        import hashlib
        out["pdf_sha256"] = hashlib.sha256(Path(doc.source_path).read_bytes()).hexdigest()
    except (OSError, ValueError):
        pass
    if m := _ARXIV_ID.search(front) or _ARXIV_ID.search(body[:4000]):
        out["arxiv_id"], out["arxiv_version"] = m.group(1), (m.group(2) or "")
    if m := _OPENREVIEW_ID.search(body):
        out["openreview_id"] = m.group(1)
    if m := _DOI.search(front):
        out["doi"] = m.group(1)
    return out


def data_sources(doc: PaperDoc) -> list[dict[str, str]]:
    """Dataset archives the paper links outside its reference list, with the quote."""
    text = "\n".join(s.text for s in doc.sections)
    refs = _reference_spans(doc)
    out, seen = [], set()
    for m in _DATA_HOSTS.finditer(text):
        url = "https://" + re.sub(r"\s+", "", m.group(1)).rstrip(".")
        path = url.split("/", 3)[3].strip("/").lower() if url.count("/") >= 3 else ""
        if path in ("", "datasets", "dataset", "records", "record", "data"):
            continue                       # a host's front page names no dataset
        if url in seen or any(lo <= m.start() < hi for lo, hi in refs):
            continue
        seen.add(url)
        out.append({"url": url, "quote": re.sub(r"\s+", " ", text[max(0, m.start() - 120):m.end()])})
    return out[:20]  # ponytail: a paper linking more archives than this lists them in a table


def readme_attributes(readme: str, doc: PaperDoc) -> str:
    """The README quote that attributes this repository to THIS paper's AUTHORS, or ''.
    Required: the paper's full title (case/spacing aside, at least four words) in the
    README's opening; not a list of papers; no "unofficial"/re-implementation wording; and
    either an author's full name from the paper's front matter or an official/first-person
    statement next to the title. An identifier alone proves the README is ABOUT the paper,
    which a re-implementation also is — so it is not evidence here; nor is an author's name,
    which a re-implementation's citation block carries too."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    # A BibTeX/citation block CITES the paper — title and authors — and says nothing about
    # who wrote the code.
    readme = re.sub(r"```.*?```|@\w+\s*\{.*?\n\s*\}", " ", readme or "", flags=re.S)
    title = norm(doc.title)
    body = norm(readme)
    if len(title.split()) < 4 or title not in body:
        return ""
    at = body.index(title)
    # A repository ABOUT this paper names it up front; a curated list or a daily digest
    # names it somewhere among many papers. Both are refused: position, then plurality.
    ids = [v for k, v in paper_identifiers(doc).items()
           if k in ("arxiv_id", "openreview_id", "doi") and v]
    if at > 1500:  # ponytail: an own-paper README states its title within its opening
        return ""
    others = set(re.findall(r"\b\d{4}\.\d{4,5}\b", readme)) - set(ids)
    if len(others) > 3:
        return ""
    # About the paper is not BY its authors: a re-implementation cites the same title and id.
    head = readme[:4000]
    if _NOT_AUTHORS.search(head) or any(
            not re.search(r"\b(?:official|our)\b", head[max(0, m.start() - 40):m.start()], re.I)
            and not re.match(r"\s*(?:our|the\s+paper)\b", head[m.end():], re.I)
            for m in re.finditer(r"\bimplementation\s+of\b", head, re.I)):
        return ""
    window = body[max(0, at - 300):at + len(title) + 300]
    if not _README_AUTHORSHIP.search(window):
        return ""
    named = next((f"{f} {l}" for f, l in _author_names(doc)
                  if re.search(rf"\b{f.lower()}\s+(?:\w\s+)?{l.lower()}\b", body)), "")
    return ("README cites the title with a first-person or official statement"
            + (f" and names the author {named}" if named else "") + ": " + window[:200])


# A README that says it is someone else's version of the paper, or that the official code
# is elsewhere / not out ("The official code has not been released yet").
_NOT_AUTHORS = re.compile(
    r"\b(unofficial|non-?official|re-?implementation|reimplementation|reproducibility "
    r"challenge|replication of|port of|third[- ]party|not affiliated|my (?:own )?implementation|"
    r"not (?:yet )?(?:been )?released)\b", re.I)
_NAME_STOP = {"university", "institute", "department", "conference", "international",
              "proceedings", "abstract", "anonymous", "laboratory", "school", "college",
              "research", "science", "sciences", "technology", "engineering", "computer",
              "machine", "learning", "correspondence", "equal", "contribution", "national",
              "center", "centre", "faculty", "academy", "workshop", "journal", "preprint",
              "under", "review", "submitted", "accepted", "paper", "email", "google", "meta",
              "microsoft", "deepmind", "openai", "amazon", "corresponding", "author", "authors",
              "independent", "researcher", "student", "professor"}


def _author_names(doc: PaperDoc) -> list[tuple[str, str]]:
    """(first, last) name pairs from the front matter, venue/affiliation words excluded."""
    title = {w.lower() for w in re.findall(r"\w+", doc.title or "")}
    out = []
    for first, last in re.findall(
            r"\b([A-Z][a-z]+(?:-[A-Z][a-z]+)?)\s+(?:[A-Z]\.\s*)*([A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?)\b",
            _front_matter(doc)):
        low = {first.lower(), last.lower()}
        if not (low & _NAME_STOP or low & title) and (first, last) not in out:
            out.append((first, last))
    return out[:30]


def discover_public_repo(cfg: Config, doc: PaperDoc) -> tuple[str, str]:
    """(url, evidence) for a public repository whose README attributes itself to this exact
    paper, or ('', why). Used only when the PDF names none; the search sends the title
    alone, and a hit is accepted only on `readme_attributes` evidence, never on rank."""
    import urllib.parse
    import urllib.request

    if not (cfg.allow_network and cfg.allow_source_search):
        return "", "public source search is gated off (SH_ALLOW_SOURCE_SEARCH / SH_ALLOW_NETWORK)"
    title = re.sub(r"\s+", " ", doc.title or "").strip()
    if len(title.split()) < 4:
        return "", f"the parsed title {title!r} is too short to search for unambiguously"
    headers = {"User-Agent": "referee-harness", "Accept": "application/vnd.github+json"}

    def get(url: str, accept: str = "") -> bytes:
        req = urllib.request.Request(url, headers={**headers, **({"Accept": accept} if accept else {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()

    query = urllib.parse.quote(f'"{title}" in:readme')
    try:
        hits = json.loads(get(f"https://api.github.com/search/repositories?q={query}&per_page=5"))
    except (OSError, ValueError) as exc:
        return "", f"public source search failed: {exc}"
    rejected: list[str] = []
    for item in (hits.get("items") or [])[:5]:  # ponytail: an exact-title hit ranks in the top 5
        name = item.get("full_name", "")
        try:
            readme = get(f"https://api.github.com/repos/{name}/readme",
                         "application/vnd.github.raw").decode("utf-8", errors="replace")
        except OSError:
            continue
        if evidence := readme_attributes(readme, doc):
            return f"https://github.com/{name}", evidence
        rejected.append(name)
    return "", (f"no public repository's README is about the exact title {title!r}"
                + (f" (mention-only or list repositories refused: {', '.join(rejected)})"
                   if rejected else ""))


# --------------------------------------------------------------------------- #
# Dependency inspection — pure parsing, no network, always safe to run
# --------------------------------------------------------------------------- #
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _parse_requirements(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):        # -r nested, -e editable, --index-url
            continue
        if m := _REQ_LINE.match(line):
            out.append(m.group(1).lower())
    return out


def _parse_environment_yml(text: str) -> list[str]:
    """Minimal conda parse: the `dependencies:` list plus its nested `pip:` block.
    Deliberately not a YAML dependency -- this only needs the package names."""
    out, in_deps = [], False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^dependencies\s*:", stripped):
            in_deps = True
            continue
        if in_deps and re.match(r"^[A-Za-z_]+\s*:", stripped) and not stripped.startswith("- "):
            break                                    # a new top-level key ends the list
        if in_deps and stripped.startswith("-"):
            item = stripped.lstrip("- ").strip()
            if item.endswith(":"):                   # the `pip:` sub-block header
                continue
            if m := _REQ_LINE.match(item):
                out.append(m.group(1).lower())
    return out


def _parse_pyproject(raw: bytes) -> list[str]:
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, UnicodeError):
        return []
    out = []
    for dep in (data.get("project") or {}).get("dependencies") or []:
        if m := _REQ_LINE.match(str(dep)):
            out.append(m.group(1).lower())
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    out += [k.lower() for k in poetry if k.lower() != "python"]
    return out


def inspect_dependencies(repo: Path) -> tuple[list[str], list[str], list[str]]:
    """(dependency files found, package names, framework labels). Never executes anything."""
    files, deps = [], []
    for rel in _DEP_FILES:
        path = repo / rel
        if not path.is_file():
            continue
        files.append(rel)
        try:
            if rel.endswith(".toml"):
                deps += _parse_pyproject(path.read_bytes())
            elif rel.endswith((".yml", ".yaml")):
                deps += _parse_environment_yml(path.read_text(encoding="utf-8", errors="replace"))
            elif rel.endswith(".txt"):
                deps += _parse_requirements(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    ordered = list(dict.fromkeys(deps))
    frameworks = list(dict.fromkeys(fw for d in ordered if (fw := _FRAMEWORKS.get(d))))
    return files, ordered, frameworks


def find_entrypoint(repo: Path) -> str:
    """A repo-relative script this repository could be asked to run, or ''.

    ADVERTISED FIRST, filename convention second: the fixed candidate list below matches
    a name only when spelled exactly as one of eleven conventional filenames in one of
    five conventional directories, which misses a repository whose README plainly
    advertises a differently-named entrypoint. What a repository TELLS a reader to run is
    stronger evidence than what its files are called, and it is the same evidence
    `experiment_id.harvest_candidates` already trusts for command discovery.

    This gate only decides whether the deeper assessment runs at all. It cannot bind a
    claim to a command, and it does not authorise anything -- experiment identity and
    `backends.authorize` remain exactly as strict.
    """
    for cmd in _advertised_programs(repo):
        cand = repo / cmd
        if cand.is_file():
            return cmd
    for directory in _ENTRY_DIRS:
        for name in _ENTRY_CANDIDATES:
            cand = repo / directory / name if directory else repo / name
            if cand.is_file():
                return str(cand.relative_to(repo)).replace("\\", "/")
    return ""


def _advertised_programs(repo: Path) -> list[str]:
    """Repo-relative program paths the repository itself advertises, best first.
    Deferred import: `experiment_id` already knows how to read these; duplicating that
    parser here would create a second, divergent answer."""
    from .experiment_id import harvest_candidates

    out: list[str] = []
    try:
        candidates = harvest_candidates(repo)
    except OSError:
        return out
    for cmd in candidates:
        for token in cmd.argv[1:]:
            if token.endswith((".py", ".sh")) and token not in out:
                out.append(token.replace("\\", "/"))
    return out


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


class GitTree(Protocol):
    """The three reads commit verification needs, over a checkout wherever it lives.
    Extracted so `verify_commit` is written ONCE and a remote backend cannot drift from
    its fail-closed logic by reimplementing it: a new backend supplies three methods
    rather than a second opinion about what 'clean' means."""

    path: str

    def git(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        """(returncode, stdout, stderr) for `git <args>` inside the checkout."""
        ...

    def is_dir(self) -> bool:
        """Is there a checkout here at all?"""
        ...

    def is_file(self, relpath: str) -> bool:
        """Is `<checkout>/<relpath>` a regular file? Used only for `.git`."""
        ...


class LocalGitTree:
    """A checkout on this machine. What every path did before the protocol existed."""

    def __init__(self, repo: str | Path) -> None:
        self.path = str(repo)
        self._repo = Path(repo)

    def git(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        try:
            p = _git(args, self._repo, timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 128, "", str(e)
        return p.returncode, p.stdout or "", p.stderr or ""

    def is_dir(self) -> bool:
        return self._repo.is_dir()

    def is_file(self, relpath: str) -> bool:
        return (self._repo / relpath).is_file()


def _tree(repo: str | Path, tree: GitTree | None) -> GitTree:
    return tree if tree is not None else LocalGitTree(repo)


_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def head_commit(repo: Path, tree: GitTree | None = None) -> str:
    """The full 40-character SHA at HEAD, or '' if the directory is not a checkout."""
    rc, out, _ = _tree(repo, tree).git(["rev-parse", "HEAD"], 30)
    sha = (out or "").strip()
    return sha if rc == 0 and _SHA.fullmatch(sha) else ""


def is_shallow(repo: Path, tree: GitTree | None = None) -> bool:
    rc, out, _ = _tree(repo, tree).git(["rev-parse", "--is-shallow-repository"], 30)
    return rc == 0 and (out or "").strip() == "true"


class TreeUninspectable(RuntimeError):
    """`git status` did not run, so nothing is known about the working tree."""


def dirty_files(repo: Path, tree: GitTree | None = None) -> list[str]:
    """Paths that differ from HEAD. A clean SHA over a modified tree is not the audited
    code. RAISES rather than returning [] when `git status` fails: a corrupt index, a
    held lock or a permissions failure must never be read as "no files differ", since
    that would let `verify_commit` certify a tree nobody actually inspected. Every other
    unverifiable condition in this module blocks the same way.

    `--untracked-files=all` catches a new file added to the checkout (a patched module
    dropped in beside the real ones) that a TRACKED-only status would miss.
    `--ignore-submodules=none` stops a submodule's own `submodule.<name>.ignore` setting
    from silently hiding that submodule's modifications.
    """
    rc, out, err = _tree(repo, tree).git(
        ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"], 60)
    if rc != 0:
        raise TreeUninspectable(
            f"`git status` exited {rc} in {repo}: {(err or '').strip()[:200]}")
    return sorted(line[3:].strip() for line in (out or "").splitlines() if line.strip())


def linked_git_dir(repo: Path, tree: GitTree | None = None) -> bool:
    """True when `repo/.git` is a FILE (a worktree/submodule redirect), not a directory.
    `acquire()` only ever produces a real `git clone`, whose `.git` is always a
    directory, so a FILE there means this path shares object storage or working-tree
    state with whatever it points at -- uninspectable rather than evidence either way."""
    return _tree(repo, tree).is_file(".git")


def locked_index_paths(repo: Path, tree: GitTree | None = None) -> list[str]:
    """Tracked paths marked assume-unchanged or skip-worktree -- both exist to make git
    STOP reporting a path's modifications in `status`, so a commit verification that only
    reads `git status` is trusting the exact mechanism designed to hide this from it."""
    rc, stdout, _ = _tree(repo, tree).git(["ls-files", "-v"], 30)
    if rc != 0:
        return []
    out = []
    for line in (stdout or "").splitlines():
        line = line.rstrip("\n")
        if not line:
            continue
        tag, _, path = line.partition(" ")
        if tag and (tag.islower() or tag == "S"):
            out.append(path.strip())
    return out


def fetch_revision(cfg: Config, dest: Path, url: str, sha: str) -> tuple[bool, str]:
    """Bring one specific commit into an existing checkout and stand on it. A depth-1
    clone holds only the tip of the default branch, so `fetch --depth 1 origin <sha>`
    asks the server for that one object graph; failing that the audited code could not
    be obtained."""
    if not cfg.allow_network:
        return False, ("network gate closed, so the audited commit cannot be fetched; only "
                       "whatever is already on disk is available")
    try:
        p = _git(["fetch", "--depth", "1", "origin", sha], dest, cfg.clone_timeout_s)
        if p.returncode != 0:
            return False, (f"git fetch of {sha[:12]} exited {p.returncode}: "
                           f"{(p.stderr or '').strip()[-200:]}")
        c = _git(["checkout", "--force", sha], dest, 120)
        if c.returncode != 0:
            return False, (f"git checkout of {sha[:12]} exited {c.returncode}: "
                           f"{(c.stderr or '').strip()[-200:]}")
    except subprocess.TimeoutExpired:
        return False, f"fetching {sha[:12]} timed out after {cfg.clone_timeout_s}s"
    return True, ""


def verify_commit(repo: str | Path, expected: str,
                  tree: GitTree | None = None) -> CommitVerification:
    """Is the checkout about to run the one that was audited? Fresh, at the point of use:
    reads the disk NOW rather than comparing two recorded strings, since a default branch
    can move or a cached checkout refresh between the audit and this run.

    Four outcomes, and only `verified` permits execution:

      verified   HEAD equals the audited SHA and the tree is clean
      mismatch   HEAD is some other commit
      dirty      HEAD matches but files have been modified since
      unknown    no audited SHA was recorded, or HEAD could not be read

    `unknown` blocks: "we never wrote down which commit we audited" is not evidence that
    this is that commit.

    `tree` lets a REMOTE checkout be certified by this same function, running every check
    inside a sandbox rather than through a second copy of this logic. `repo` is still
    carried for the messages, which name the checkout a reader would go and look at.
    """
    ver = CommitVerification(expected=(expected or "").strip().lower())
    path = Path(repo) if repo else None
    checkout = _tree(path or Path("."), tree)
    if not path or not checkout.is_dir():
        ver.state, ver.reason = "unknown", "there is no checkout on disk to verify"
        return ver

    ver.actual = head_commit(path, checkout)
    ver.shallow = is_shallow(path, checkout)
    if not ver.actual:
        ver.state = "unknown"
        ver.reason = f"'{path}' is not a git checkout, so its commit cannot be established"
        return ver
    if not ver.expected:
        ver.state = "unknown"
        ver.reason = (f"no audited commit was recorded, so the checkout now at "
                      f"{ver.actual[:12]} cannot be shown to be the code that was audited")
        return ver

    # A recorded value may legitimately be abbreviated (older artifacts hold 12 chars).
    # Git's own abbreviation semantics apply: a prefix of at least 12 hex chars identifies
    # a commit; shorter than that is not accepted.
    matched = (ver.actual == ver.expected
               or (len(ver.expected) >= 12 and ver.actual.startswith(ver.expected)))
    if not matched:
        ver.state = "mismatch"
        ver.reason = (
            f"the checkout is at {ver.actual[:12]} but the audit, the experiment identity and "
            f"the cited findings are all about {ver.expected[:12]}. Executing this would "
            f"reconcile a number produced by one commit against reasoning done on another"
            + (". The clone is shallow, so the audited commit is not present locally"
               if ver.shallow else "."))
        return ver

    # A redirected `.git` or a locked index path is not the same failure `dirty_files`
    # detects, so both are checked BEFORE it, fail-closed: neither leaves this harness
    # able to say the tree it is about to certify is the tree `git status` inspected.
    if linked_git_dir(path, checkout):
        ver.state = "unknown"
        ver.reason = (
            f"HEAD is the audited commit {ver.actual[:12]}, but '{path}/.git' is a file, not "
            f"a directory — a linked worktree or submodule redirect this acquisition never "
            f"produces on its own. The checkout cannot be shown to be an independent tree, so "
            f"it cannot be certified clean.")
        return ver
    locked = locked_index_paths(path, checkout)
    if locked:
        ver.state = "unknown"
        ver.reason = (
            f"HEAD is the audited commit {ver.actual[:12]}, but {len(locked)} tracked path(s) "
            f"carry assume-unchanged or skip-worktree: {', '.join(locked[:5])}. Both bits exist "
            f"to make `git status` stop reporting a path's own modifications, so their presence "
            f"means the tree cannot be shown to be clean, not that it is.")
        return ver

    try:
        ver.dirty_files = dirty_files(path, checkout)
    except TreeUninspectable as e:
        # Fails CLOSED: not knowing whether the tree is clean is not the same as knowing
        # it is. `unknown` is the state every other unverifiable condition here uses.
        ver.state = "unknown"
        ver.reason = (
            f"HEAD is the audited commit {ver.actual[:12]}, but the working tree could not be "
            f"inspected, so it cannot be shown to match it: {e}. An uninspectable tree is not "
            f"a clean tree.")
        return ver
    if ver.dirty_files:
        ver.state = "dirty"
        ver.reason = (
            f"HEAD is the audited commit {ver.actual[:12]}, but {len(ver.dirty_files)} tracked "
            f"file(s) differ from it: {', '.join(ver.dirty_files[:5])}. The SHA no longer "
            f"describes the code that would run.")
        return ver

    ver.state = "verified"
    ver.reason = (f"the checkout is exactly the audited commit {ver.actual[:12]} with a clean "
                  f"working tree")
    return ver


def acquire(cfg: Config, root: Path, pid: str, doc: PaperDoc,
            revision: str = "") -> RepoAcquisition:
    """Clone the paper's repository into `runs/<pid>/repo`, if we are allowed to.

    Returns a populated `RepoAcquisition` in every path, including the ones where
    nothing happened, because "we did not look" and "we looked and there is nothing"
    are different facts the report has to be able to tell apart.

    `revision` is the audited commit, when a previous run recorded one. Passing it makes
    acquisition a PIN rather than a fetch: the checkout is moved onto that SHA, or the
    acquisition says it could not be. Without it the default branch is taken, which is
    reproducible only until someone pushes.
    """
    # ONLY the authorship-qualified URL, never the raw candidate list -- `repo_urls`
    # holds every repository link the paper mentions, most belonging to other people.
    url = doc.repo_url or official_repo_url(doc)
    dest = root / "runs" / pid / "repo"
    # How the URL was attributed is decided once, at ingest (`stages.ingest`), and only read
    # here: a public-search attribution is recorded with its README evidence.
    how, _, evidence = (doc.repo_discovery or ("pdf_cue: " if url else "")).partition(": ")
    discovered_by = how if url else ""

    if not url:
        return RepoAcquisition(
            status="unavailable", discovery_evidence=evidence,
            reason=("the paper advertises no repository of its own. "
                    + (f"{len(doc.repo_urls)} repository URL(s) appear in the text but none is "
                       f"introduced by an availability cue outside the reference list, so none "
                       f"can be attributed to these authors: {', '.join(doc.repo_urls[:3])}"
                       if doc.repo_urls else "No repository URL appears in its text.")
                    + (f" Public source search: {evidence}." if evidence else "")))
    if dest.exists() and (dest / ".git").exists():
        acq = RepoAcquisition(url=url, status="cached", path=str(dest),
                              reason="clone already present; not re-fetched")
        acq.commit = head_commit(dest)
    elif not cfg.allow_network:
        return RepoAcquisition(
            url=url, status="blocked",
            reason="network gate closed (set SH_ALLOW_NETWORK=1 to clone). Nothing was "
                   "fetched, so nothing here is evidence about the paper's code.")
    elif shutil.which("git") is None:
        return RepoAcquisition(url=url, status="failed", reason="git is not on PATH")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            p = _git(["clone", "--depth", "1", "--recurse-submodules", url, str(dest)],
                     None, cfg.clone_timeout_s)
        except subprocess.TimeoutExpired:
            return RepoAcquisition(url=url, status="failed",
                                   reason=f"clone timed out after {cfg.clone_timeout_s}s")
        if p.returncode != 0:
            return RepoAcquisition(
                url=url, status="failed",
                reason=f"git clone exited {p.returncode}: {(p.stderr or '').strip()[-300:]}")
        acq = RepoAcquisition(url=url, status="cloned", path=str(dest))
        acq.commit = head_commit(dest)

    # --- pin to the audited commit ----------------------------------------------------
    # Recording what arrived is not pinning: move onto the requested revision or say
    # plainly that the audited code could not be obtained.
    acq.requested_revision = (revision or "").strip().lower()
    acq.shallow = is_shallow(dest)
    if acq.requested_revision and acq.commit != acq.requested_revision:
        ok, why = fetch_revision(cfg, dest, url, acq.requested_revision)
        if ok:
            acq.commit = head_commit(dest)
            acq.shallow = is_shallow(dest)
            acq.reason = f"checked out the audited commit {acq.requested_revision[:12]}"
        else:
            acq.status = "failed"
            acq.reason = (f"the audited commit {acq.requested_revision[:12]} could not be "
                          f"obtained, so nothing here is the code that was audited: {why}")
            return acq
    acq.pinned = bool(acq.requested_revision) and acq.commit == acq.requested_revision

    acq.dependency_files, acq.dependencies, acq.frameworks = inspect_dependencies(dest)
    acq.entrypoint = find_entrypoint(dest)
    acq.discovered_by, acq.discovery_evidence = discovered_by, evidence
    return acq


# --------------------------------------------------------------------------- #
# Isolated environment
# --------------------------------------------------------------------------- #
def _venv_python(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")


def build_env(cfg: Config, root: Path, pid: str, acq: RepoAcquisition) -> RepoAcquisition:
    """Create `runs/<pid>/env` and install the repo's declared stack into it. Installing
    is the second gate, separate from cloning: reading a dependency list is safe, whereas
    resolving it runs arbitrary setup code from the index."""
    if acq.status not in ("cloned", "cached") or not acq.path:
        acq.env_status = "not_attempted"
        return acq
    if not (cfg.allow_install and cfg.allow_network):
        acq.env_status = "blocked"
        return acq

    env_dir = root / "runs" / pid / "env"
    py = _venv_python(env_dir)
    if not py.exists():
        try:
            subprocess.run([cfg.python, "-m", "venv", str(env_dir)], capture_output=True,
                           text=True, timeout=600, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            acq.env_status, acq.reason = "failed", f"venv creation failed: {e}"
            return acq

    installed = False
    for rel in acq.dependency_files:
        if not rel.endswith(".txt"):
            continue                                  # only pip requirement files are installable here
        try:
            p = subprocess.run([str(py), "-m", "pip", "install", "-r",
                                str(Path(acq.path) / rel)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=cfg.install_timeout_s)
        except subprocess.TimeoutExpired:
            acq.env_status = "failed"
            acq.reason = f"pip install -r {rel} timed out after {cfg.install_timeout_s}s"
            return acq
        if p.returncode != 0:
            acq.env_status = "failed"
            acq.reason = f"pip install -r {rel} exited {p.returncode}: {(p.stderr or '').strip()[-300:]}"
            return acq
        installed = True
    acq.env_path = str(py)
    acq.env_status = "ready"
    if not installed:
        acq.reason = "no pip requirements file to install; the bare venv is what ran"
    return acq


# --------------------------------------------------------------------------- #
# Execution capability — decided before anything runs
# --------------------------------------------------------------------------- #
# Markers that only appear in a conda environment pinned to Linux. A repository whose
# declared environment is Linux-only cannot be faithfully constructed on Windows, and a
# crash under a substitute environment is a fact about the substitute.
_LINUX_ENV_MARKERS = ("linux-64", "libgcc-ng", "libstdcxx-ng", "ld_impl_linux", "libgomp")
_ENV_DECL_FILES = ("environment.yml", "environment.yaml")


def entrypoint_imports(repo: Path, entrypoint: str) -> list[str]:
    """Top-level module names the entrypoint imports, by PARSING it, never importing it.
    Only the roots of absolute imports at module level are returned: a conditional
    import inside a function is not a startup requirement, and importing the file to
    find out would execute the very code this check exists to avoid running."""
    path = repo / entrypoint
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return []
    roots: list[str] = []
    for node in tree.body:                       # module level only
        if isinstance(node, ast.Import):
            roots += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.append(node.module.split(".")[0])
    seen: list[str] = []
    for r in roots:
        if r not in seen:
            seen.append(r)
    return seen


def missing_imports(interpreter: str, repo: Path, modules: list[str], timeout: int = 120) -> list[str]:
    """Which of `modules` the TARGET interpreter cannot resolve. Runs
    `importlib.util.find_spec` in a subprocess of that interpreter, which locates a
    module without running it. Modules living in the checkout resolve because the repo
    root is put on the path, which is why `repo` is passed rather than assumed."""
    if not modules:
        return []
    probe = (
        "import importlib.util, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "out = []\n"
        "for m in sys.argv[2:]:\n"
        "    try:\n"
        "        if importlib.util.find_spec(m) is None: out.append(m)\n"
        "    except Exception: out.append(m)\n"
        "print(json.dumps(out))\n"
    )
    try:
        p = subprocess.run([interpreter, "-c", probe, str(repo), *modules],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return list(modules)                     # cannot ask: treat as unresolved, not as fine
    if p.returncode != 0:
        return list(modules)
    try:
        return list(json.loads((p.stdout or "[]").strip().splitlines()[-1]))
    except (json.JSONDecodeError, ValueError, IndexError):
        return list(modules)


def accepts_argument(repo: Path, entrypoint: str, flag: str = "seed") -> bool:
    """Does the repository plausibly accept `--<flag>` on this entrypoint? Statically,
    and deliberately generously (argparse `add_argument`, a dataclass/attrs field, or a
    HuggingFace-style argument class), searched across `.py` files in the checkout since
    definitions are routinely factored into an `args.py`. Generous because a false
    "invalid" would suppress a legitimate reproduction; a false "valid" only costs a run
    that fails into `startup_failure`, still INCONCLUSIVE."""
    pattern = re.compile(rf"(--{flag}\b)|(^\s*{flag}\s*:)", re.M)
    candidates = [repo / entrypoint] + sorted(repo.glob("*.py")) + sorted(repo.glob("*/*_args.py"))
    for path in candidates[:60]:
        if not path.is_file():
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def declared_platform(repo: Path) -> str:
    """'linux' when the repo's environment file pins a Linux-only stack, else ''."""
    for name in _ENV_DECL_FILES:
        path = repo / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in _LINUX_ENV_MARKERS):
            return "linux"
    return ""


# `missing_imports`, or whatever answers the same question somewhere else. Signature is
# (interpreter, repo_root, modules) -> the modules that interpreter cannot resolve.
ImportResolver = Callable[[str, Path, list[str]], list[str]]


def assess_capability(acq: RepoAcquisition, interpreter: str, harness_python: str,
                      flag: str = "seed", platform: str = "",
                      resolve_imports: ImportResolver | None = None) -> ExecCapability:
    """Can this machine give the repository a fair run? Decided before anything executes.

    Ordered from the most fundamental blocker outward, so the reported reason is the root
    cause rather than a downstream symptom: a Linux-only environment explains missing
    dependencies, and missing dependencies explain an import crash.

    `established` False never accuses the paper of anything -- it records that this
    runner could not mount the experiment, which is what reconciliation needs to refuse
    to convict.

    `resolve_imports` is the one check here that cannot be answered by reading files: the
    others are static and read the same bytes wherever the checkout sits, but resolving a
    module means ASKING an interpreter, which for a remote backend is not on this
    machine. So the question is delegated to whichever backend owns that interpreter.
    """
    resolve_imports = resolve_imports or missing_imports
    # The platform compared against is the one the RUN will see, supplied by the
    # execution backend, not the one this process happens to be on.
    platform = platform or sys.platform
    cap = ExecCapability(
        env_status=acq.env_status or "", interpreter=interpreter or "",
        entrypoint=acq.entrypoint or "", current_platform=platform,
        interpreter_is_repo_env=bool(interpreter) and interpreter == (acq.env_path or None),
    )
    repo_path = Path(acq.path) if acq.path else None

    if not repo_path or not repo_path.is_dir():
        cap.reason_code, cap.detail = "not_attempted", "no checkout on disk to execute"
        return cap
    if not acq.entrypoint:
        cap.reason_code = "invalid_invocation"
        cap.detail = "no runnable entrypoint was identified in the checkout"
        return cap

    cap.declared_platform = declared_platform(repo_path)
    if cap.declared_platform and cap.declared_platform not in platform:
        cap.reason_code = "environment_incompatible"
        cap.detail = (f"the repository declares a {cap.declared_platform}-only environment and the "
                      f"execution backend offers {platform}; it cannot be constructed faithfully there")
        return cap

    if acq.env_status != "ready" or not acq.env_path:
        cap.reason_code = "environment_incompatible"
        cap.detail = (f"no environment was built for this repository (env_status "
                      f"'{acq.env_status or 'not_attempted'}'), so the only interpreter available is "
                      f"the harness's own, which does not carry the repository's dependencies")
        return cap
    if interpreter == harness_python or not cap.interpreter_is_repo_env:
        cap.reason_code = "environment_incompatible"
        cap.detail = ("the run would use the harness's own interpreter rather than the environment "
                      "built for the repository")
        return cap

    cap.checked_imports = entrypoint_imports(repo_path, acq.entrypoint)
    cap.missing_dependencies = resolve_imports(interpreter, repo_path, cap.checked_imports)
    if cap.missing_dependencies:
        cap.reason_code = "dependency_missing"
        cap.detail = ("the entrypoint imports modules the built environment cannot resolve: "
                      + ", ".join(cap.missing_dependencies[:8]))
        return cap

    # The check is about the invocation this harness WILL make, not a flag it made up. An
    # empty `flag` means the caller established that no seed will be passed (a
    # deterministic quantity with no seed-to-seed distribution), for which `--seed` would
    # be exactly the invented argument this check exists to refuse.
    if flag:
        cap.accepts_seed_argument = accepts_argument(repo_path, acq.entrypoint, flag)
        if not cap.accepts_seed_argument:
            cap.reason_code = "invalid_invocation"
            cap.detail = (f"this harness would pass --{flag}, which does not appear anywhere in "
                          f"the repository's argument definitions; the command is ours, not theirs")
            return cap
    else:
        cap.accepts_seed_argument = None
        cap.detail = "no seed is passed for this target, so no seed argument is required of the repository"

    cap.established, cap.reason_code = True, "established"
    cap.detail = "environment built, dependencies resolvable, and the invocation is one the repo accepts"
    return cap


# --------------------------------------------------------------------------- #
# Standalone fallback when the paper ships no code
# --------------------------------------------------------------------------- #
STANDALONE_TEMPLATE = '''\
"""Standalone verification probe — __PID__.

The paper advertises no runnable repository, so there is nothing to clone. This file
is a scaffold for reproducing the paper's stated formulation directly, and AS
GENERATED IT REPRODUCES NOTHING: `compute(seed)` returns a constant, so the harness
will report it as a degenerate probe rather than as evidence about the paper.

Claim this probe is meant to settle:
    __CLAIM__

Cell the result will be reconciled against:
    __CELL__ = __CELL_VALUE__

To make it real, implement `compute(seed)` from the paper's own equations — the maths
is in the sections the audit lenses quoted — and return the metric the cited cell
reports, on the same scale. Anything the paper does not state is a modelling choice
you must not silently invent; leave it out and say so, rather than tuning until the
number matches.

Contract (parsed by harness/local_exec.py — keep both lines):
    SH_DEVICE <cuda|mps|cpu>
    SH_METRIC arm=<name> seed=<int> value=<float>
"""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", default="reproduction")
args = ap.parse_args()

try:
    import torch
    DEVICE = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
except ImportError:
    DEVICE = "cpu"
print(f"SH_DEVICE {DEVICE}", flush=True)


def compute(seed: int) -> float:
    """TODO(driver): implement the paper's formulation and return its metric."""
    raise NotImplementedError(
        "standalone_probe.py is a scaffold: implement compute() from the paper's "
        "equations before treating its output as a reproduction."
    )


print(f"SH_METRIC arm={args.arm} seed={args.seed} value={compute(args.seed):.6f}", flush=True)
'''


def synthesize_standalone(root: Path, pid: str, claim: str, cell_ref: str,
                          cell_value: str) -> RepoAcquisition:
    """Write `runs/<pid>/standalone_probe.py` when there is no repository to clone. The
    scaffold raises rather than returning a plausible number: a file that silently
    emitted a constant would flow through reconciliation into a confident
    "FAILED_REPRODUCTION" verdict against a paper whose code was never run."""
    path = root / "runs" / pid / "standalone_probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = STANDALONE_TEMPLATE
    for key, value in (("__PID__", pid), ("__CLAIM__", (claim or "(none recorded)")[:400]),
                       ("__CELL__", cell_ref or "(no cell cited)"),
                       ("__CELL_VALUE__", cell_value or "(unknown)")):
        body = body.replace(key, str(value).replace('"""', "'''"))
    path.write_text(body, encoding="utf-8")
    return RepoAcquisition(
        status="synthesized", path=str(path),
        reason="no repository advertised; wrote a standalone scaffold. It raises "
               "NotImplementedError until its compute() is written, so it cannot "
               "produce a reproduction verdict on its own.",
        frameworks=["standalone"],
    )


if __name__ == "__main__":  # self-check: python -m harness.repo
    from .schema import Section

    d = PaperDoc(paper_id="t", sections=[Section(
        section_idx=0,
        text="Code is available: https: //github.com/wuyang98/weathergen and see "
             "2https://github.com/facebookresearch/vissl for the baseline. "
             "This project is available at https: //github.com/mbzuai-nlp/finchain.git.")])
    urls = find_repo_urls(d)
    assert urls[0] == "https://github.com/wuyang98/weathergen", urls
    assert "https://github.com/mbzuai-nlp/finchain" in urls, urls
    assert urls[-1] == "https://github.com/facebookresearch/vissl", "footnote cite must rank last"

    hyphen_broken = PaperDoc(paper_id="t2", sections=[Section(
        section_idx=0,
        text="The code of this work is available at "
             "https://github.com/hassan- mahmood/SemanticMLLAttacks.git")])
    assert official_repo_url(hyphen_broken) == \
        "https://github.com/hassan-mahmood/SemanticMLLAttacks", \
        "a hyphen the owner name really contains must survive a typesetter's line break"

    host_broken = PaperDoc(paper_id="t3", sections=[Section(
        section_idx=0,
        text="Code is accessible at https://github. com/yankd22/FedSaC/.")])
    assert official_repo_url(host_broken) == "https://github.com/yankd22/FedSaC", \
        "a line break inside the host literal itself must still resolve to that host"

    assert _parse_requirements("torch>=2.0  # comment\n-r other.txt\n\nnumpy==1.26\n") == \
        ["torch", "numpy"]
    assert _parse_environment_yml(
        "name: x\ndependencies:\n  - python=3.11\n  - pip:\n    - jax==0.4\nvariables:\n  A: 1\n"
    ) == ["python", "jax"]
    print("repo self-check OK")
