"""Acquisition of cited or discovered public artifacts. Runs inside a network-on container:
`harness/execute.py` hands this file's text to `python -c`; tests import it. Standard library only.

What it guarantees, each because a run once got it wrong:
  - Links on a landing page resolve against the FINAL URL of the response (after every redirect,
    cached or not), never the URL that was asked for: a DOI page redirects to the repository's host.
  - A failure is classified: `transient` (timeouts, 5xx, 429: retried with backoff), `missing`
    (404/410), `inaccessible` (401/402/403/451), `protocol`, `denied`, `storage`, `no_data` (a page
    that exposes no downloadable file), `content_invalid`, `bug` (this code's own error). Only the
    classes that can be cured are recovered from, each recovery bounded and recorded.
  - Recovery uses documented mechanisms only: another printed reading of the citation, the
    repository's own records API (Zenodo, Figshare), the DOI's registry metadata, the links of the
    official landing page. Nothing is guessed and nothing is crawled beyond the landing page.
  - Nothing is admitted to an experiment before its content is validated: not an HTML page, an empty
    or corrupt file, an archive with an unsafe path, a file whose published checksum disagrees, or
    executable source code (acquired artifacts are data; code enters only through the gated route).
    Released code, notebooks and READMEs are kept as TEXT only (`record_src`), for quoting, never as data.
  - A set is complete or says what it lacks: a named file not admitted (`missing`), an `include` pattern that
    matched nothing (`unmatched_include`), a cut (`truncated`). The primary reading of a citation decides
    a failure's class; its alternates rank below it.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

CODE =(".py", ".ipynb", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".r", ".rmd", ".m", ".jl", ".c", ".cc", ".cpp", ".h",
        ".hpp", ".cu", ".java", ".js", ".ts", ".go", ".rs", ".lua", ".pl", ".rb", ".php")
DATA = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".csv", ".tsv", ".json", ".jsonl", ".parquet", ".npz", ".npy",
        ".pkl", ".h5", ".hdf5", ".arff", ".xlsx", ".feather", ".arrow", ".txt", ".data", ".names", ".mat", ".pt",
        ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".gz")
TEXT = (".csv", ".tsv", ".txt", ".json", ".jsonl", ".arff", ".md", ".data", ".names")
# The repositories' own records APIs: {host: url template}. A landing page on one of these hosts is
# resolved to its files through the API, with the checksums it publishes.
RECORD_APIS = {"zenodo.org": "https://zenodo.org/api/records/{id}", "figshare.com": "https://api.figshare.com/v2/articles/{id}"}
# ponytail: 20 files followed from one landing page (links are unvetted), 500 from a repository record (its own
# listing, sizes and checksums; the storage cap bounds the bytes), 3 tries per URL, 2 alternate readings of a citation,
# 3 archives opened for an `include` that named no listed file or link (it may name what they hold).
# A cut is never silent: `rec["truncated"]` names how many matched and which were not followed.
MAX_FOLLOW, MAX_RECORD_FILES, TRIES, MAX_ALTERNATES, MAX_ARCHIVES = 20, 500, 3, 2, 3
# ponytail: quote-only text of released code, notebooks and READMEs: 256 KB kept per file, 2 MB per check, 50 files
# fetched for their text alone per source (each at most 1 MB, a notebook 16 MB, read whole to decode its cells); at most
# 640 KB of it (compressed, base64) on stdout, which keeps only its last 1 MB beside the manifest. Every cut is recorded
# (`record_src_cut`).
SRC_FILE, SRC_TOTAL, SRC_FILES, SRC_NOTEBOOK, SRC_WIRE = 256 << 10, 2 << 20, 50, 16 << 20, 640 << 10
ARCHIVES = (".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".tbz2", ".txz")   # unpacked by suffix only
RANK = ("bug", "denied", "storage", "inaccessible", "transient", "missing", "content_invalid", "no_data", "protocol")
_DOC = re.compile(r"(?:^|/)readme[^/]*$|\.(?:md|rst)$", re.I)


class FetchError(Exception):
    def __init__(self, klass: str, msg: str, attempts: list | None = None):
        super().__init__(msg)
        self.klass, self.attempts = klass, attempts or []


def _by_status(c: int) -> str:
    return ("transient" if c in (408, 425, 429) or c >= 500 else "missing" if c in (404, 410) else
            "inaccessible" if c in (401, 402, 403, 451) else "protocol")


def classify(e: BaseException) -> tuple[str, str]:
    """(failure class, message). An exception of this code's own is a `bug`, never missing data. A hub client's
    error is classed by the HTTP status it carries (`response.status_code`), else by its kind."""
    if isinstance(e, FetchError):
        return e.klass, str(e)
    if isinstance(e, urllib.error.HTTPError):
        return _by_status(e.code), f"HTTP {e.code} {e.reason}"
    if isinstance(code := getattr(getattr(e, "response", None), "status_code", None), int):
        return _by_status(code), f"HTTP {code} {type(e).__name__}: {e}"[:500]
    names = {c.__name__ for c in type(e).__mro__}
    if any(k in n for n in names for k in ("Timeout", "ConnectError", "NetworkError", "TransportError", "OfflineMode",
                                           "LocalEntryNotFound")):
        return "transient", f"{type(e).__name__}: {e}"
    if names & {"GatedRepoError"}:
        return "inaccessible", f"{type(e).__name__}: {e}"
    if names & {"RepositoryNotFoundError", "RevisionNotFoundError", "EntryNotFoundError"}:
        return "missing", f"{type(e).__name__}: {e}"
    # A TLS failure is a fault of this host's connection to the source (a handshake, a middlebox, a server's bad
    # moment: the same file arrived on 09-30 and failed with a decode alert on 10-01), never a fact about the data:
    # retried, and if it persists, a fault of this run (INCONCLUSIVE), not a data blocker.
    if isinstance(e, ssl.SSLError):
        return "transient", f"TLS: {e}"
    if isinstance(e, urllib.error.URLError):
        return "transient", (f"TLS: {e.reason}" if isinstance(e.reason, ssl.SSLError) else f"unreachable: {e.reason}")
    if isinstance(e, (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException)):
        return "transient", f"{type(e).__name__}: {e}"
    if isinstance(e, OSError):
        return ("storage" if "no space" in str(e).lower() else "transient"), f"{type(e).__name__}: {e}"
    return "bug", f"{type(e).__name__}: {e}"


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Records every hop, so the final URL and the history are known."""

    def __init__(self):
        self.chain: list[dict] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append({"from": req.full_url, "to": newurl, "status": code})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _put(tmp: str, dst: str) -> None:
    """Rename a finished temp file into place. If another container already put a complete copy there, keep
    that one (a concurrent writer, or Windows refusing to overwrite a file another reader has open)."""
    try:
        os.replace(tmp, dst)
    except OSError:
        if not os.path.exists(dst):
            raise
        os.remove(tmp)


def _base(url: str) -> str:
    return os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/")))


def _head(path: str, n: int) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def _src_limit(name: str) -> int:
    return SRC_NOTEBOOK if name.lower().endswith(".ipynb") else SRC_FILE + 1


def _src_fetchable(name: str, size) -> bool:
    """Is a released text file small enough to fetch for its text alone (its size known)?"""
    return size is not None and size <= (SRC_NOTEBOOK if name.lower().endswith(".ipynb") else 4 * SRC_FILE)


def _safe_rel(name: str) -> str:
    """A relative path that stays inside its folder on any host ("" if it cannot)."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    return "" if not parts or ".." in parts else "/".join(re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts)


def _as_text(path: str, data: bytes) -> str:
    """A notebook as the plain text of its cells' sources; anything else decoded as UTF-8."""
    if path.lower().endswith(".ipynb"):
        try:
            cells = json.loads(data.decode("utf-8")).get("cells") or []
            return "\n\n".join(f"# %% [{c.get('cell_type', '?')}] cell {i + 1}\n" + (
                "".join(c["source"]) if isinstance(c.get("source"), list) else str(c.get("source") or ""))
                for i, c in enumerate(cells))
        except (ValueError, AttributeError, TypeError, KeyError):
            pass
    return data.decode("utf-8", "replace")


class Fetcher:
    def __init__(self, cache: str, cap: int, deny: list[str], sleep=time.sleep, tries: int = TRIES, timeout: int = 120):
        self.cache, self.cap, self.deny, self.sleep, self.tries, self.timeout, self.total = cache, cap, deny, sleep, tries, timeout, 0
        self.unpacked, self.idx, self.src, self.src_bytes, self.src_cut = 0, "0", [], 0, []

    def denied(self, s: str) -> bool:
        return any(d and d in s.lower() for d in self.deny)

    def room(self) -> int:
        """Bytes left under the storage cap: downloads and what archives unpacked to both count."""
        return self.cap - self.total - self.unpacked

    def keep(self, name: str, data: bytes, origin: str = "") -> None:
        """The TEXT of a released file that is never data — code, a notebook (its cells' sources), a README — kept for
        quoting a reading of the released implementation; never mounted, never run. Capped; a cut is recorded."""
        path = _safe_rel(name)
        if not path or any(x["dir"] == self.idx and x["path"] == path for x in self.src):
            return
        if not path.lower().endswith((".html", ".htm")) and looks_html(data[:8192]):     # a login page is no released code
            self.src_cut.append(f"{self.idx}/{path}: an HTML page, not the file it was named for")
            return
        text = _as_text(path, data)
        raw = text.encode("utf-8")
        cut = len(raw) > SRC_FILE
        if cut:
            text = raw[:SRC_FILE].decode("utf-8", "ignore")
            raw = text.encode("utf-8")
        if self.src_bytes + len(raw) > SRC_TOTAL:
            self.src_cut.append(f"{self.idx}/{path}")
            return
        self.src_bytes += len(raw)
        self.src.append({"dir": self.idx, "path": path, "text": text, "bytes": len(raw), "from": origin,
                         "sha256": hashlib.sha256(raw).hexdigest(), **({"cut": f"its first {SRC_FILE} bytes"} if cut else {})})

    def wire(self) -> tuple[list[dict], str]:
        """(listing, payload): the kept text as one compressed line for stdout, within SRC_WIRE (what does not fit is cut,
        recorded), and the listing the manifest carries (path `<source>/<path>`, bytes, sha256)."""
        items = list(self.src)
        enc = ""
        while items:
            enc = base64.b64encode(zlib.compress(json.dumps([{k: x[k] for k in ("dir", "path", "text")} for x in items]).encode(), 9)).decode()
            if len(enc) <= SRC_WIRE:
                break
            self.src_cut.append(f"{items[-1]['dir']}/{items.pop()['path']}")
            enc = ""
        return [{"path": f"{x['dir']}/{x['path']}", **{k: x[k] for k in ("bytes", "sha256", "from", "cut") if k in x}} for x in items], enc

    def get(self, url: str, dest: str, md5: str = "", size: int | None = None) -> tuple[str, dict]:
        """Download `url` (or read it from the cache) into `dest`; -> (path, info). `info` carries the
        FINAL url, the redirect chain, the status and content type, and the retries that were needed.
        The cache keeps that metadata, so a cached response resolves links exactly as a fresh one.
        Where the repository publishes a checksum or a size, a transfer that disagrees (cut short,
        corrupted) is retried like a network fault, is never cached, and a cached copy that disagrees
        is purged. A cut that repeats at every try is a fault of the transfer (`transient`); content that
        disagrees with its published checksum at every try is `content_invalid`. Several containers share the
        cache, so every write goes to a temp name of its own and is renamed into place whole."""
        if self.denied(url):
            raise FetchError("denied", "source denied by SH_DENY_SOURCES")
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        cpath = f"{self.cache}/{key}/{_base(url) or 'index.html'}"
        info: dict = {"url": url}
        if os.path.exists(cpath) and (md5 or size is not None) and not (
                (size is None or os.path.getsize(cpath) == size) and (not md5 or _md5(cpath) == md5.lower())):
            for stale in (cpath, cpath + ".meta.json"):        # a cached copy that disagrees with the published one
                if os.path.exists(stale):
                    os.remove(stale)
            info["cache_purged"] = "the cached copy differs from the published checksum or size"
        if os.path.exists(cpath) and os.path.exists(cpath + ".meta.json"):
            with open(cpath + ".meta.json") as f:
                info.update(json.load(f))
            info["from_cache"] = True
        else:
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            failed: list[dict] = []
            part: dict = {}       # this fetch's own partial file, and the validator of the response that began it
            try:
                for i in range(self.tries):
                    hop = _Redirects()
                    held = os.path.getsize(part["tmp"]) if part.get("validator") and os.path.exists(part["tmp"]) else 0
                    try:
                        head = {"User-Agent": "referee", **({"Range": f"bytes={held}-", "If-Range": part["validator"]}
                                                            if held else {})}    # kept across a redirect hop
                        req = urllib.request.Request(url, headers=head)
                        with urllib.request.build_opener(hop).open(req, timeout=self.timeout) as r:
                            final = r.geturl()
                            if self.denied(final):
                                raise FetchError("denied", f"redirected to a denied source: {final}")
                            if r.status != 206:               # a continuation keeps what the response that began it said
                                info.update(final_url=final, redirects=hop.chain, http_status=r.status,
                                            content_type=r.headers.get("Content-Type", ""),
                                            content_disposition=r.headers.get("Content-Disposition", ""))
                            self._save(r, cpath, md5, size, part, held)
                        break
                    except Exception as e:                    # noqa: BLE001 - classified, never swallowed
                        klass, msg = classify(e)
                        if isinstance(e, FetchError) and klass not in ("corrupt", "short"):
                            raise
                        failed.append({"try": i + 1, "class": klass, "error": msg[:300], "redirects": hop.chain,
                                       **({"resumed_from": held} if held else {})})
                        if klass not in ("transient", "corrupt", "short") or i == self.tries - 1:
                            raise FetchError({"corrupt": "content_invalid", "short": "transient"}.get(klass, klass), msg, failed) from e
                        self.sleep(2 ** i)
            finally:
                if part.get("tmp") and os.path.exists(part["tmp"]):    # no partial file outlives its fetch
                    os.remove(part["tmp"])
            info.update(retries=failed, bytes=os.path.getsize(cpath))
            tmp = f"{cpath}.meta.json.{uuid.uuid4().hex}"
            with open(tmp, "w") as f:
                json.dump({k: v for k, v in info.items() if k not in ("url", "from_cache", "cache_purged")}, f)
            _put(tmp, cpath + ".meta.json")
        self.total += os.path.getsize(cpath)
        os.makedirs(dest, exist_ok=True)
        out = os.path.join(dest, filename(info, url))
        try:                                   # the cache and the work folder share a volume: no second copy of a big archive
            if os.path.exists(out):
                os.remove(out)
            os.link(cpath, out)
        except OSError:
            shutil.copy(cpath, out)
        return out, info

    def _save(self, r, cpath: str, md5: str = "", size: int | None = None, part: dict | None = None,
              held: int = 0) -> None:
        """Stream the response to a temp file of this fetch's own, check it against what was promised (the total
        length, the published size and md5), and only then rename it into the cache. A 206 that continues exactly the
        `held` bytes (asked with If-Range on the validator of the response that began them) is appended; a server that
        ignores the range starts the file over, and a range other than the one asked for is refused and dropped. What
        arrived before a cut or a network fault is kept in `part` for the next try only where an ETag or Last-Modified
        ties it to one version of the file (Oct-01: a 2.9 GB archive cut at 285 MB began again at byte 0)."""
        part = {} if part is None else part
        rng = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("Content-Range") or "")
        n = int(r.headers.get("Content-Length") or 0)
        cont = bool(held and r.status == 206 and rng and int(rng.group(1)) == held)
        if not cont and part.get("tmp") and os.path.exists(part["tmp"]):     # the whole file again, or another range
            os.remove(part["tmp"])
        if not cont:
            part.clear()
            held = 0
            if r.status == 206:
                raise FetchError("short", f"a partial answer ({r.headers.get('Content-Range')}) that continues no bytes held")
        total = (int(rng.group(2)) if rng.group(2) != "*" else held + n) if cont else n
        if total > self.room():
            raise FetchError("storage", f"{total} bytes would pass the storage cap ({self.cap} bytes, SH_MAX_DATA_GB)")
        if not cont:
            part.update(tmp=f"{cpath}.part.{uuid.uuid4().hex}", validator=r.headers.get("ETag") or r.headers.get("Last-Modified") or "")
        tmp, got = part["tmp"], held
        try:
            with open(tmp, "ab" if cont else "wb") as f:
                while b := r.read(1 << 20):
                    got += len(b)
                    if got > self.room():
                        raise FetchError("storage", f"passed the storage cap ({self.cap} bytes, SH_MAX_DATA_GB)")
                    f.write(b)
            if total and got < total:                       # cut short in transit
                raise FetchError("short", f"the transfer ended after {got} of {total} bytes")
            if size is not None and got != size:            # whole as sent, yet not the published file: corrupt
                raise FetchError("short" if got < size and not total else "corrupt", f"{got} bytes arrived, the published size is {size}")
            if md5 and (h := _md5(tmp)) != md5.lower():
                raise FetchError("corrupt", f"md5 {h} differs from the published {md5}")
            _put(tmp, cpath)
            part.clear()
        except BaseException as e:
            resumable = part.get("validator") and (e.klass == "short" if isinstance(e, FetchError)
                                                   else isinstance(e, Exception) and classify(e)[0] == "transient")
            if not resumable:
                if os.path.exists(tmp):
                    os.remove(tmp)
                part.clear()
            raise

    def json(self, url: str, tmp: str):
        p, info = self.get(url, tmp)
        with open(p, encoding="utf-8") as f:
            return json.load(f), info


def filename(info: dict, url: str) -> str:
    """The name a response is saved under: its Content-Disposition, else the final URL's last segment,
    else the requested one's; never a path."""
    m = re.search(r"""filename\*?=(?:UTF-8'')?"?([^";]+)""", info.get("content_disposition") or "")
    name = os.path.basename(urllib.parse.unquote(m.group(1).strip())) if m else ""
    name = name or _base(info.get("final_url") or "") or _base(url)
    return name if name not in ("", ".", "..") else "index.html"


def looks_html(head: bytes) -> bool:
    """The one test for "this is a web page", wherever a response or a file is judged: past a byte-order mark,
    whitespace, an XML prolog and comments, the document opens with an HTML doctype or element. Data that merely
    holds HTML strings (a crawl's JSONL) is no page."""
    h = head.decode("utf-16", "ignore").encode("utf-8", "ignore") if head[:2] in (b"\xff\xfe", b"\xfe\xff") else head
    h = h.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    while h.startswith((b"<?xml", b"<!--")):
        end = h.find(b"?>" if h.startswith(b"<?") else b"-->")
        if end < 0:
            return False
        h = h[end + (2 if h.startswith(b"<?") else 3):].lstrip()
    return h.startswith((b"<!doctype html", b"<html"))


def is_html(path: str, info: dict) -> bool:
    return "html" in (info.get("content_type") or "").lower() or looks_html(_head(path, 8192))


def html_links(page: str, final_url: str, hosts: set[str]) -> list[str]:
    """Links of a page resolved against its FINAL url (or its <base>), on the hosts of the request or
    of the page it ended on."""
    base = final_url
    if m := re.search(r"""<base[^>]+href=["']([^"']+)""", page, re.I):
        base = urllib.parse.urljoin(final_url, html.unescape(m.group(1)))
    out = set()
    for h in re.findall(r"""(?:href|data-url|data-href)=["']([^"'#]+)""", page, re.I):
        u = urllib.parse.urljoin(base, html.unescape(h.strip()))
        pu = urllib.parse.urlparse(u)
        if pu.scheme in ("http", "https") and pu.netloc.lower() in hosts:
            out.add(u)
    return sorted(out)


def _path(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlparse(url).path).lstrip("/")


def pick(links: list[str], include: list[str]) -> list[str]:
    """The links to follow: those the plan's `include` patterns name, else the data and archive files."""
    if include:
        return [u for u in links if _named(_path(u), include)]
    return [u for u in links if urllib.parse.urlparse(u).path.lower().endswith(DATA)]


def _affinity(name: str, include: str) -> int:
    """How many leading characters an archive's name (without its suffix) shares with an `include` name (case folded)."""
    a, b = name.rsplit("/", 1)[-1].lower(), include.rsplit("/", 1)[-1].lower()
    for s in ARCHIVES:
        if a.endswith(s):
            a = a[:-len(s)]
            break
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def record_api(url: str) -> str:
    """The documented records-API URL of a repository record page, or "" when the host has no adapter or the
    URL is no record."""
    pu = urllib.parse.urlparse(url)
    api = next((RECORD_APIS[h] for h in RECORD_APIS if pu.netloc.lower() == h or pu.netloc.lower().endswith("." + h)), "")
    m = re.search(r"/(?:records?|deposit|articles?)/(?:[^/]+/)*?(\d+)(?:/|$)", pu.path) if api else None
    return api.format(id=m[1]) if api and m else ""


def record_files(f: Fetcher, final_url: str, tmp: str) -> list[dict] | None:
    """The files of a repository record through its documented API, with published checksums; None
    when the host has no adapter or the URL is no record."""
    api = record_api(final_url)
    if not api:
        return None
    return record_listing(f.json(api, tmp)[0])


def record_listing(j: dict) -> list[dict]:
    """[{url, name, bytes, md5}] from a records-API answer."""
    out = []
    for e in j.get("files") or []:
        url = (e.get("links") or {}).get("self") or (e.get("links") or {}).get("download") or e.get("download_url")
        name = e.get("key") or e.get("name")
        chk = str(e.get("checksum") or e.get("computed_md5") or "")
        if url and name:
            out.append({"url": url, "name": name, "bytes": e.get("size"), "md5": chk.split(":", 1)[-1] if chk else ""})
    return out


def _named(name: str, patterns: list[str]) -> bool:
    """Does a pattern name this file (its path or its basename)? Case-sensitive on every host, as in the container."""
    return any(fnmatch.fnmatchcase(os.path.basename(name), p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def _archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVES)


def _unpacked_name(name: str) -> str:
    """The name a file packed in ONE single-file wrapper carries inside it (`train.csv.zip` -> `train.csv`; a `.gz`,
    `.bz2` or `.xz` alike); "" for anything else (a multi-file `.tar.*` is no single named file)."""
    low = name.lower()
    return "" if low.endswith((".tar.gz", ".tar.bz2", ".tar.xz")) else next(
        (name[: -len(x)] for x in (".zip", ".gz", ".bz2", ".xz") if low.endswith(x) and len(name) > len(x)), "")


def _dot(name: str) -> bool:
    """A path under a dot-name (`.gitattributes`, a hub's `.cache/`): repository metadata, never data."""
    return any(p.startswith(".") for p in name.replace("\\", "/").split("/") if p)


def _textual(name: str) -> bool:
    """Released code, a notebook or a README: its text is kept for quoting (`record_src`)."""
    return name.lower().endswith(CODE) or bool(_DOC.search(name))


def admit(path: str, name: str, dest: str, expect_md5: str = "", members: list[str] | None = None, *,
          keep=None, limit: int | None = None, only: bool = False) -> tuple[list[dict], list[dict]]:
    """Validate one downloaded file and place it under `dest`: -> (admitted, rejected). Rejected files
    are removed; each rejection says why. `members`: the plan's `include` patterns, applied to the
    files INSIDE an archive when they named no link on the page (they named what the download holds):
    the members they match are kept, all of them if none matches. Only an archive SUFFIX unpacks (an .npz,
    .xlsx or .pt is a zip and stays whole), into the folder its name has. `limit`: the bytes an archive may
    unpack to (what is left under the storage cap; a zip bomb is refused whole). `keep(name, bytes, origin)`
    receives the text of released code, notebooks and READMEs, whole files or members (quote-only text). `only`: one of
    several archives opened on speculation (the plan named a member, not an archive) keeps the matching members and
    nothing else: an unrelated archive of the same page never enters the data (Oct-06: WISDM_ar_latest beside WISDM_at);
    a page's single archive keeps all its members when none matches (the planner could not see inside it)."""
    rej = lambda why, cls="content_invalid": ([], [{"file": name, "class": cls, "why": why}])
    low = name.lower()
    if keep and _textual(name):
        keep(name, _head(path, _src_limit(name)), name)
    if low.endswith(CODE):
        return rej("executable source code is not admitted as data", "code_excluded")
    size = os.path.getsize(path)
    if size == 0:
        return rej("the file is empty")
    if not low.endswith((".html", ".htm")) and looks_html(_head(path, 8192)):
        return rej("an HTML page, not the data it was named for")
    if expect_md5 and _md5(path) != expect_md5.lower():
        return rej(f"md5 {_md5(path)} differs from the published {expect_md5}")
    sha = _sha(path)
    os.makedirs(dest, exist_ok=True)
    kept, dropped = [], []
    zipped, tarred = low.endswith(".zip"), _archive(low) and not low.endswith(".zip")
    # A name that claims to be an archive must be one: a truncated ZIP is not even recognised as a ZIP.
    if (zipped and not zipfile.is_zipfile(path)) or (tarred and not tarfile.is_tarfile(path)):
        return rej("the file is named as an archive but is not a readable one (truncated or corrupt)")
    over = lambda n: rej(f"the archive unpacks to {n} bytes, past the {limit} bytes left under the storage cap "
                         "(SH_MAX_DATA_GB): refused whole", "storage")
    sub = os.path.dirname(name.replace("\\", "/"))
    at = lambda m: f"{sub}/{m}" if sub else m
    if low.endswith((".gz", ".bz2", ".xz")) and not tarred:
        import bz2, gzip, lzma
        try:                                              # a compressed single file: it must decompress to its end
            with {"gz": gzip, "bz2": bz2, "xz": lzma}[low.rsplit(".", 1)[1]].open(path, "rb") as z:
                while z.read(1 << 22):
                    pass
        except (OSError, EOFError, lzma.LZMAError) as e:
            return rej(f"the compressed file does not decompress: {type(e).__name__}: {e}")
    stage = tempfile.mkdtemp(prefix=".extract-", dir=dest)      # extracted whole or not at all
    into = os.path.join(stage, sub)
    try:
        if zipped:
            with zipfile.ZipFile(path) as z:
                for m in z.namelist():
                    if m.startswith("/") or ".." in m.split("/"):
                        return rej(f"unsafe path in archive: {m}", "content_invalid")
                files = [m for m in z.infolist() if not m.is_dir()]
                data = [m for m in files if not m.filename.lower().endswith(CODE)]
                chosen = ([m for m in data if _named(m.filename, members)] if members else []) or ([] if only else data)
                if limit is not None and sum(m.file_size for m in chosen) > limit:   # a member never unpacks past its size
                    return over(sum(m.file_size for m in chosen))
                bad = z.testzip()
                if bad:
                    return rej(f"the archive is corrupt at {bad}")
                for m in files:
                    if keep and _textual(m.filename):
                        with z.open(m) as fh:
                            keep(at(m.filename), fh.read(_src_limit(m.filename)), name)
                dropped += [{"file": m.filename, "class": "code_excluded", "why": "source code inside an archive"}
                            for m in files if m not in data]
                for m in chosen:
                    z.extract(m, into)
                    kept.append(at(m.filename))
        elif tarred:
            with tarfile.open(path) as t:
                every = t.getmembers()
                for m in every:
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        return rej(f"unsafe path in archive: {m.name}")
                files = [m for m in every if m.isfile()]
                data = [m for m in files if not m.name.lower().endswith(CODE)]
                chosen = ([m for m in data if _named(m.name, members)] if members else []) or ([] if only else data)
                if limit is not None and sum(m.size for m in chosen) > limit:
                    return over(sum(m.size for m in chosen))
                for m in files:
                    if keep and _textual(m.name):
                        keep(at(m.name), t.extractfile(m).read(_src_limit(m.name)), name)
                dropped += [{"file": m.name, "class": "code_excluded", "why": "source code inside an archive"}
                            for m in files if m not in data]
                t.extractall(into, members=chosen, filter="data")
                kept = [at(m.name) for m in chosen]
        else:
            os.makedirs(os.path.dirname(os.path.join(stage, name)), exist_ok=True)
            shutil.move(path, os.path.join(stage, name))
            kept = [name]
        for root_, _, fs in os.walk(stage):                     # complete: move it into place
            for f in fs:
                rel = os.path.relpath(os.path.join(root_, f), stage)
                os.makedirs(os.path.dirname(os.path.join(dest, rel)) or dest, exist_ok=True)
                shutil.move(os.path.join(root_, f), os.path.join(dest, rel))
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
        return rej(f"the archive could not be read: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if not kept:
        return [], dropped or [{"file": name, "class": "content_invalid", "why": "the archive holds no data file"}]
    return [{"file": k, "from": name, "source_sha256": sha, "source_bytes": size,
             "bytes": os.path.getsize(os.path.join(dest, k))} for k in kept], dropped


def _hf(f: Fetcher, s: dict, dest: str, tmp: str, rec: dict) -> None:
    """A hub repository through its documented client: the files `include` names (repository metadata, empty
    placeholders and code aside), each validated by `admit` like any download; its code and README as text only.
    The bytes count against the same storage cap as every other download of the check."""
    src = s["source"]
    kind, _, repo = src[5:].partition("/")
    repo, _, rev = repo.partition("@")
    if f.denied(repo):
        raise FetchError("denied", "source denied by SH_DENY_SOURCES")
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], check=True)
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().repo_info(repo, repo_type=kind.rstrip("s"), revision=rev or None, files_metadata=True)
    pats = s.get("include") or ["*"]
    sib = [x for x in info.siblings or [] if x.size != 0 and not _dot(x.rfilename)]
    if len(sib) < MAX_RECORD_FILES:                                    # the hub's own listing, only when complete
        rec["listing"] = [x.rfilename for x in sib]
    data = {x.rfilename: x for x in sib if _named(x.rfilename, pats) and not x.rfilename.lower().endswith(CODE)}
    text = {x.rfilename: x for x in [x for x in sib if _textual(x.rfilename) and _src_fetchable(x.rfilename, x.size)][:SRC_FILES]}
    need = sum(x.size or 0 for x in {**text, **data}.values())
    if need > f.room():
        raise FetchError("storage", f"{need} bytes would pass the storage cap ({f.cap} bytes, SH_MAX_DATA_GB; "
                                    f"{f.cap - f.room()} already taken by this check)")
    os.makedirs(tmp, exist_ok=True)
    local = tempfile.mkdtemp(prefix="hub-", dir=tmp)       # the client's own metadata stays here, never in `dest`
    try:
        # A transient failure (a timeout, a 5xx, a partial file the client lost) is retried like any download, bounded by
        # the fetcher's tries, in the SAME folder so what arrived is kept (the client resumes it); anything else is not
        # retried (Oct-05 label ranking C6: one lost `.incomplete` file ended a 3 GB snapshot after one attempt).
        for i in range(f.tries):
            try:
                snapshot_download(repo, repo_type=kind.rstrip("s"), revision=info.sha,
                                  allow_patterns=sorted({**text, **data}), local_dir=local)
                break
            except Exception as e:                        # noqa: BLE001
                klass, msg = classify(e)
                if klass != "transient" or i == f.tries - 1:
                    raise
                rec["recovery"].append(f"hub snapshot retried after a transient failure ({msg[:160]}), resuming in place")
                f.sleep(min(60, 5 * 2 ** i))
        rec.update(revision=info.sha, files_matched=len(data))
        for name in [*data, *[t for t in text if t not in data]]:
            p = os.path.join(local, name)
            if not os.path.isfile(p):
                if name in data:
                    rec["missing"].append({"file": name, "class": "missing", "why": "listed by the hub, absent from its snapshot"})
                continue
            f.total += os.path.getsize(p)
            if name not in data:
                f.keep(name, _head(p, _src_limit(name)), src)
                continue
            ok, bad = admit(p, name, dest, keep=f.keep, limit=f.room())
            f.unpacked += sum(o["bytes"] for o in ok if o["file"] != o["from"])
            rec["admitted"] += [{**o, "from": src if o["file"] == o["from"] else o["from"]} for o in ok]
            rec["rejected"] += bad
            _missing(rec, name, ok, bad)
    finally:
        shutil.rmtree(local, ignore_errors=True)


def _missing(rec: dict, name: str, ok: list, bad: list) -> None:
    """A NAMED file (listed by its repository, or matched by `include`) that admitted nothing is missing from the set."""
    if ok:
        rec["_hit"].append(name)
    elif (b := next((b for b in bad if b.get("class") != "code_excluded"), None)) is not None:
        rec["missing"].append({"file": name, "class": b.get("class") or "content_invalid", "why": b.get("why", "")[:200]})


def _follow(f: Fetcher, targets: list[dict], dest: str, tmp: str, rec: dict, members: list[str] | None = None,
            limit: int = MAX_FOLLOW) -> None:
    named = [t for t in targets if not t.get("speculative")]
    if len(named) > limit:                # never silent: the plan, the script author and the report are told
        rec["truncated"] = {"matched": len(named), "followed": limit,
                            "not_followed": [t.get("name") or t["url"] for t in named[limit:]][:30]}
    for t in named[:limit] + [t for t in targets if t.get("speculative")]:
        row = {"url": t["url"], **({"speculative": True} if t.get("speculative") else {})}
        name = t.get("name") or _path(t["url"])
        try:
            p, info = f.get(t["url"], tmp, t.get("md5", ""), t.get("bytes"))
            row.update(final_url=info.get("final_url"), http_status=info.get("http_status"), redirects=info.get("redirects"),
                       from_cache=info.get("from_cache", False), bytes=info.get("bytes"))
            ok, bad = admit(p, t.get("name") or filename(info, t["url"]), dest, t.get("md5", ""), t.get("members", members),
                            keep=f.keep, limit=f.room(), only=bool(t.get("only")))
            f.unpacked += sum(o["bytes"] for o in ok if o["file"] != o["from"])
            rec["admitted"] += ok
            rec["rejected"] += bad
            row["admitted"] = len(ok)
            if t.get("named"):
                _missing(rec, name, ok, bad)
        except FetchError as e:
            row.update({"class": e.klass, "error": str(e)[:300], "tries": e.attempts})
            if t.get("named"):
                rec["missing"].append({"file": name, "class": e.klass, "why": str(e)[:200]})
        except Exception as e:                            # noqa: BLE001 - this code's own error: recorded as one
            row.update({"class": "bug", "error": f"{type(e).__name__}: {e}"[:300]})
            if t.get("named"):
                rec["missing"].append({"file": name, "class": "bug", "why": row["error"][:200]})
        rec["followed"].append(row)


def _texts(f: Fetcher, targets: list[dict], tmp: str, rec: dict) -> None:
    """Released code, notebooks and READMEs a record lists beside its data: fetched for their text alone."""
    for t in targets:
        row = {"url": t["url"], "text_only": True}
        try:
            p, _ = f.get(t["url"], tmp, t.get("md5", ""), t.get("bytes"))
            f.keep(t["name"], _head(p, _src_limit(t["name"])), t["name"])
        except FetchError as e:
            row.update({"class": e.klass, "error": str(e)[:300]})
        rec["followed"].append(row)


def _web(f: Fetcher, url: str, s: dict, dest: str, tmp: str, rec: dict) -> None:
    p, info = f.get(url, tmp)
    rec["attempts"].append({"url": url, "final_url": info.get("final_url"), "redirects": info.get("redirects"),
                            "http_status": info.get("http_status"), "content_type": info.get("content_type"),
                            "from_cache": info.get("from_cache", False), "retries": info.get("retries") or []})
    if not is_html(p, info):
        ok, bad = admit(p, filename(info, url), dest, keep=f.keep, limit=f.room())
        f.unpacked += sum(o["bytes"] for o in ok if o["file"] != o["from"])
        rec["admitted"] += ok
        rec["rejected"] += bad
        return
    final = info.get("final_url") or url
    rec["landing"] = {"final_url": final, "sha256": _sha(p), "bytes": os.path.getsize(p)}
    include = s.get("include") or []
    texts: list[dict] = []
    listed = record_files(f, final, tmp)
    if listed is not None:
        rec["recovery"].append(f"documented mechanism: the repository records API for {final}")
        listed = [t for t in listed if t.get("bytes") != 0 and not _dot(t["name"])]
        if len(listed) < MAX_RECORD_FILES:                # a complete listing: what the plan did not take is known
            rec["listing"] = [t["name"] for t in listed]
        targets = [{**t, "named": True} for t in listed if not include or _named(t["name"], include)]
        pool = listed
        took = {t["url"] for t in targets}
        texts = [t for t in listed if t["url"] not in took and _textual(t["name"]) and _src_fetchable(t["name"], t.get("bytes"))][:SRC_FILES]
    else:
        with open(p, encoding="utf-8", errors="replace") as fh:
            page = fh.read()
        hosts = {urllib.parse.urlparse(final).netloc.lower(), urllib.parse.urlparse(url).netloc.lower()}
        links = html_links(page, final, hosts)
        rec["links"] = links[:200]
        targets = [{"url": u, "named": bool(include)} for u in pick(links, include)]
        pool = [{"url": u} for u in pick(links, [])]
    unmatched = [q for q in include if not any(_named(t.get("name") or _path(t["url"]), [q]) for t in targets)]
    took = {t["url"] for t in targets}
    arch = [t for t in pool if _archive(t.get("name") or _path(t["url"])) and t["url"] not in took]
    if unmatched and arch:
        # `include` names no file the record lists or the page links: the planner could not see inside the download, so
        # it may have named what an archive holds. A few archives are followed and the patterns filter their members;
        # past MAX_ARCHIVES, those whose names share the most with `include` (5+ leading characters), and the archives
        # not opened are recorded (Oct-06 changepoint C9: 5 archives, none opened, silently). These follows are
        # speculative: what they fail at is recorded, never counted as a missing named file.
        near = lambda t: max(_affinity(t.get("name") or _path(t["url"]), q) for q in unmatched)
        opened = arch if len(arch) <= MAX_ARCHIVES else sorted(
            [t for t in arch if near(t) >= 5], key=lambda t: -near(t))[:MAX_ARCHIVES]
        if len(arch) > len(opened):
            rec["archives_not_opened"] = [t.get("name") or _path(t["url"]) for t in arch if t not in opened]
        targets += [{**t, "named": False, "speculative": True, "members": unmatched, "only": len(opened) > 1} for t in opened]
        where = "file of the record" if listed is not None else "link on the landing page"
        rec["recovery"].append(f"no {where} matched {unmatched}: {len(opened)} of its {len(arch)} archive(s) were followed "
                               "and `include` was applied to their members" + (
                                   f"; {len(arch) - len(opened)} not opened (MAX_ARCHIVES={MAX_ARCHIVES}; those named "
                                   "least like `include`)" if len(arch) > len(opened) else ""))
    if not targets and "doi.org" in urllib.parse.urlparse(url).netloc:
        doi = urllib.parse.urlparse(url).path.lstrip("/")
        try:
            j, _ = f.json("https://api.datacite.org/dois/" + urllib.parse.quote(doi), tmp)
            targets = [{"url": u, "named": True} for u in (j.get("data", {}).get("attributes", {}).get("contentUrl") or [])
                       if u.startswith("http")]
            rec["recovery"].append("the DOI registry's content URLs" + (f": {len(targets)} found" if targets else ": none"))
        except FetchError as e:
            rec["recovery"].append(f"the DOI registry could not be read: {e.klass}")
    if targets:
        _follow(f, targets, dest, tmp, rec, None, MAX_RECORD_FILES if listed is not None else MAX_FOLLOW)
    _texts(f, texts, tmp, rec)
    if not rec["admitted"] and all(t.get("speculative") for t in targets):
        rec["attempts"].append({"url": final, "class": "no_data",
                                "error": "the landing page exposes no downloadable file matching `include` or a data suffix"})


def acquire(f: Fetcher, s: dict, dest: str, tmp: str, idx: str) -> dict:
    """One source -> its record. Tries the citation as planned, then (bounded) the other printed readings of it.
    Every row says which reading it belongs to (0: as planned); what is missing is that of the last reading tried."""
    rec: dict = {"source": s["source"], "dir": idx, "attempts": [], "admitted": [], "rejected": [], "followed": [], "recovery": [],
                 "missing": []}
    if s.get("refused"):                  # a gate the caller closed for this source: recorded, nothing is sent
        rec.update(failure_class="gate", error=f"refused: {s['refused']}")
        return rec
    f.idx = idx
    urls = [s["source"]] + [a for a in s.get("alternates") or [] if a != s["source"]][:MAX_ALTERNATES]
    for n, url in enumerate(urls):
        if n:
            rec["recovery"].append(f"another reading of the printed citation: {url}")
        a0, f0, r0 = len(rec["attempts"]), len(rec["followed"]), len(rec["rejected"])
        rec["missing"], rec["_hit"] = [], []
        try:
            if url.startswith("hf://"):
                _hf(f, {**s, "source": url}, dest, tmp, rec)
            else:
                _web(f, url, s, dest, tmp, rec)
        except FetchError as e:
            rec["attempts"].append({"url": url, "class": e.klass, "error": str(e)[:300], "tries": e.attempts})
        except Exception as e:                            # noqa: BLE001
            k, msg = classify(e)
            rec["attempts"].append({"url": url, "class": k, "error": msg[:300]})
        for row in rec["attempts"][a0:] + rec["followed"][f0:] + rec["rejected"][r0:]:
            row["reading"] = n
        if rec["admitted"]:
            break
    names = rec.pop("_hit", []) + [m["file"] for m in rec["missing"]] + [
        x for a in rec["admitted"] for x in (a["file"], a.get("from") or "") if x]
    # A named file kept in its packed form (`train.csv` as `train.csv.zip`: a member archive and a compressed file stay
    # whole) was acquired; it is recorded as packed, never read as matching nothing (Oct-01 conformal C5).
    packed = [{"include": q, "file": x} for q in s.get("include") or [] if not any(_named(x, [q]) for x in names)
              for x in [next((a["file"] for a in rec["admitted"] if _named(_unpacked_name(a["file"]), [q])), "")] if x]
    if packed:
        rec["packed"] = packed
    rec["unmatched_include"] = [q for q in s.get("include") or [] if not any(_named(x, [q]) for x in names)
                                and q not in {p["include"] for p in packed}]
    rec["failure_class"] = "" if rec["admitted"] else failure_class(rec)
    return rec


def failure_class(rec: dict) -> str:
    """Why nothing was admitted, as one class: the most actionable of what went wrong in the FIRST reading of the
    citation that shows a failure (an alternate reading — a hyphen variant whose host does not resolve — never
    outranks the planned one). Speculative follows and text-only fetches decide nothing."""
    rows = [(a.get("reading", 0), a["class"]) for a in rec["attempts"] + rec["followed"]
            if a.get("class") and not a.get("speculative") and not a.get("text_only")]
    rows += [(r.get("reading", 0), r.get("class") or "content_invalid") for r in rec["rejected"] if r.get("class") != "code_excluded"]
    first = min((n for n, _ in rows), default=0)
    seen = {k for n, k in rows if n == first}
    return next((k for k in RANK if k in seen), "content_invalid" if rec["rejected"] else "no_data")


def manifest(out_dir: str, recs: list[dict]) -> dict:
    files, heads = [], 0
    for root, _, fs in os.walk(out_dir):
        for name in sorted(fs):
            p = os.path.join(root, name)
            row = {"path": os.path.relpath(p, out_dir).replace(os.sep, "/"), "bytes": os.path.getsize(p)}
            if len(files) < 5000:                          # ponytail: 5000 hashed
                row["sha256"] = _sha(p)
            if heads < 20 and name.lower().endswith(TEXT) and row["bytes"]:
                heads += 1
                with open(p, encoding="utf-8", errors="replace") as fh:
                    row["head"] = fh.read(400)
            files.append(row)
    for r in recs:
        r["admitted_files"] = sum(1 for x in files if x["path"].split("/")[0] == str(r["dir"]))
    whole = lambda r: r["admitted_files"] and not (r.get("truncated") or r.get("missing") or r.get("unmatched_include"))
    return {"sources": recs, "files": files[:2000], "n_files": len(files), "bytes": sum(r["bytes"] for r in files),
            "status": "ok" if recs and all(whole(r) for r in recs) else "partial" if files else "none"}


def main(environ=os.environ, out_dir: str = "/data", tmp: str = "/root/.cache/referee-tmp",
         cache: str = "/root/.cache/referee-urls") -> dict:
    f = Fetcher(cache, int(environ["REFEREE_CAP"]), [d for d in environ.get("REFEREE_DENY", "").lower().split(",") if d])
    recs = [acquire(f, s, os.path.join(out_dir, str(i)), tmp, str(i)) for i, s in enumerate(json.loads(environ["REFEREE_SOURCES"]))]
    shutil.rmtree(tmp, ignore_errors=True)
    m = manifest(out_dir, recs)
    m["record_src"], wire = f.wire()          # quote-only text: its own line, before the manifest (stdout keeps its tail)
    if f.src_cut:
        m["record_src_cut"], m["record_src_n_cut"] = f.src_cut[:50], len(f.src_cut)
    if wire:
        print("REFEREE_RECORD_SRC " + wire)
    print("REFEREE_MANIFEST " + json.dumps(m))
    return m


if __name__ == "__main__":
    main()
