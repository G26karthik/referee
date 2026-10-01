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
"""
from __future__ import annotations

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

CODE = (".py", ".ipynb", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".r", ".rmd", ".m", ".jl", ".c", ".cc", ".cpp", ".h",
        ".hpp", ".cu", ".java", ".js", ".ts", ".go", ".rs", ".lua", ".pl", ".rb", ".php")
DATA = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".csv", ".tsv", ".json", ".jsonl", ".parquet", ".npz", ".npy",
        ".pkl", ".h5", ".hdf5", ".arff", ".xlsx", ".feather", ".arrow", ".txt", ".data", ".names", ".mat", ".pt",
        ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".gz")
TEXT = (".csv", ".tsv", ".txt", ".json", ".jsonl", ".arff", ".md", ".data", ".names")
# The repositories' own records APIs: {host: url template}. A landing page on one of these hosts is
# resolved to its files through the API, with the checksums it publishes.
RECORD_APIS = {"zenodo.org": "https://zenodo.org/api/records/{id}", "figshare.com": "https://api.figshare.com/v2/articles/{id}"}
# ponytail: 20 files followed from one landing page (links are unvetted), 500 from a repository record (its own
# listing, sizes and checksums; the storage cap bounds the bytes), 3 tries per URL, 2 alternate readings of a citation.
# A cut is never silent: `rec["truncated"]` names how many matched and which were not followed.
MAX_FOLLOW, MAX_RECORD_FILES, TRIES, MAX_ALTERNATES = 20, 500, 3, 2


class FetchError(Exception):
    def __init__(self, klass: str, msg: str, attempts: list | None = None):
        super().__init__(msg)
        self.klass, self.attempts = klass, attempts or []


def classify(e: BaseException) -> tuple[str, str]:
    """(failure class, message). An exception of this code's own is a `bug`, never missing data."""
    if isinstance(e, FetchError):
        return e.klass, str(e)
    if isinstance(e, urllib.error.HTTPError):
        c = e.code
        k = ("transient" if c in (408, 425, 429) or c >= 500 else "missing" if c in (404, 410) else
             "inaccessible" if c in (401, 402, 403, 451) else "protocol")
        return k, f"HTTP {c} {e.reason}"
    if isinstance(e, ssl.SSLError):
        return "protocol", f"TLS: {e}"
    if isinstance(e, urllib.error.URLError):
        return ("protocol", f"TLS: {e.reason}") if isinstance(e.reason, ssl.SSLError) else ("transient", f"unreachable: {e.reason}")
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


class Fetcher:
    def __init__(self, cache: str, cap: int, deny: list[str], sleep=time.sleep, tries: int = TRIES, timeout: int = 120):
        self.cache, self.cap, self.deny, self.sleep, self.tries, self.timeout, self.total = cache, cap, deny, sleep, tries, timeout, 0

    def denied(self, s: str) -> bool:
        return any(d and d in s.lower() for d in self.deny)

    def get(self, url: str, dest: str, md5: str = "", size: int | None = None) -> tuple[str, dict]:
        """Download `url` (or read it from the cache) into `dest`; -> (path, info). `info` carries the
        FINAL url, the redirect chain, the status and content type, and the retries that were needed.
        The cache keeps that metadata, so a cached response resolves links exactly as a fresh one.
        Where the repository publishes a checksum or a size, a transfer that disagrees (cut short,
        corrupted) is retried like a network fault, is never cached, and a cached copy that disagrees
        is purged. Several containers share the cache, so every write goes to a temp name of its own and
        is renamed into place whole."""
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
            for i in range(self.tries):
                hop = _Redirects()
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "referee"})
                    with urllib.request.build_opener(hop).open(req, timeout=self.timeout) as r:
                        final = r.geturl()
                        if self.denied(final):
                            raise FetchError("denied", f"redirected to a denied source: {final}")
                        info.update(final_url=final, redirects=hop.chain, http_status=r.status,
                                    content_type=r.headers.get("Content-Type", ""),
                                    content_disposition=r.headers.get("Content-Disposition", ""))
                        self._save(r, cpath, md5, size)
                    break
                except Exception as e:                    # noqa: BLE001 - classified, never swallowed
                    klass, msg = classify(e)
                    if isinstance(e, FetchError) and klass not in ("corrupt",):
                        raise
                    failed.append({"try": i + 1, "class": klass, "error": msg[:300], "redirects": hop.chain})
                    if klass not in ("transient", "corrupt") or i == self.tries - 1:
                        raise FetchError("content_invalid" if klass == "corrupt" else klass, msg, failed) from e
                    self.sleep(2 ** i)
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

    def _save(self, r, cpath: str, md5: str = "", size: int | None = None) -> None:
        """Stream the response to a temp file of its own, check it against what was promised (Content-Length,
        the published size and md5), and only then rename it into the cache."""
        n = int(r.headers.get("Content-Length") or 0)
        if self.total + n > self.cap:
            raise FetchError("storage", f"{n} bytes would pass the storage cap ({self.cap} bytes, SH_MAX_DATA_GB)")
        tmp, got, h = f"{cpath}.part.{uuid.uuid4().hex}", 0, hashlib.md5()
        try:
            with open(tmp, "wb") as f:
                while b := r.read(1 << 20):
                    got += len(b)
                    if self.total + got > self.cap:
                        raise FetchError("storage", f"passed the storage cap ({self.cap} bytes, SH_MAX_DATA_GB)")
                    h.update(b)
                    f.write(b)
            if n and got != n:
                raise FetchError("corrupt", f"the transfer ended after {got} of {n} bytes")
            if size is not None and got != size:
                raise FetchError("corrupt", f"{got} bytes arrived, the published size is {size}")
            if md5 and h.hexdigest() != md5.lower():
                raise FetchError("corrupt", f"md5 {h.hexdigest()} differs from the published {md5}")
            _put(tmp, cpath)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
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


def is_html(path: str, info: dict) -> bool:
    if "html" in (info.get("content_type") or "").lower():
        return True
    with open(path, "rb") as f:
        head = f.read(2048).lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:512]


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


def pick(links: list[str], include: list[str]) -> list[str]:
    """The links to follow: those the plan's `include` patterns name, else the data and archive files."""
    if include:
        return [u for u in links if any(fnmatch.fnmatch(_base(u), p) or fnmatch.fnmatch(
            urllib.parse.urlparse(u).path.lstrip("/"), p) for p in include)]
    return [u for u in links if urllib.parse.urlparse(u).path.lower().endswith(DATA)]


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
    return any(fnmatch.fnmatch(os.path.basename(name), p) or fnmatch.fnmatch(name, p) for p in patterns)


def admit(path: str, name: str, dest: str, expect_md5: str = "", members: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Validate one downloaded file and place it under `dest`: -> (admitted, rejected). Rejected files
    are removed; each rejection says why. `members`: the plan's `include` patterns, applied to the
    files INSIDE an archive when they named no link on the page (they named what the download holds):
    the members they match are kept, all of them if none matches."""
    rej = lambda why, cls="content_invalid": ([], [{"file": name, "class": cls, "why": why}])
    low = name.lower()
    if low.endswith(CODE):
        return rej("executable source code is not admitted as data", "code_excluded")
    size = os.path.getsize(path)
    if size == 0:
        return rej("the file is empty")
    with open(path, "rb") as f:
        head = f.read(2048)
    if not low.endswith((".html", ".htm")) and (head.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
        return rej("an HTML page, not the data it was named for")
    if expect_md5 and _md5(path) != expect_md5.lower():
        return rej(f"md5 {_md5(path)} differs from the published {expect_md5}")
    sha = _sha(path)
    os.makedirs(dest, exist_ok=True)
    kept, dropped = [], []
    # A name that claims to be an archive must be one: a truncated ZIP is not even recognised as a ZIP.
    if (low.endswith(".zip") and not zipfile.is_zipfile(path)) or (
            low.endswith((".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz")) and not tarfile.is_tarfile(path)):
        return rej("the file is named as an archive but is not a readable one (truncated or corrupt)")
    if low.endswith((".gz", ".bz2", ".xz")) and not tarfile.is_tarfile(path):
        import bz2, gzip, lzma
        try:                                              # a compressed single file: it must decompress to its end
            with {"gz": gzip, "bz2": bz2, "xz": lzma}[low.rsplit(".", 1)[1]].open(path, "rb") as z:
                while z.read(1 << 22):
                    pass
        except (OSError, EOFError, lzma.LZMAError) as e:
            return rej(f"the compressed file does not decompress: {type(e).__name__}: {e}")
    stage = tempfile.mkdtemp(prefix=".extract-", dir=dest)      # extracted whole or not at all
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    return rej(f"the archive is corrupt at {bad}")
                for m in z.namelist():
                    if m.startswith("/") or ".." in m.split("/"):
                        return rej(f"unsafe path in archive: {m}", "content_invalid")
                inside = [m for m in z.infolist() if not m.is_dir() and not m.filename.lower().endswith(CODE)]
                pick_ = [m.filename for m in inside if _named(m.filename, members)] if members else []
                for m in z.infolist():
                    if m.is_dir():
                        continue
                    if pick_ and m.filename not in pick_ and not m.filename.lower().endswith(CODE):
                        continue
                    if m.filename.lower().endswith(CODE):
                        dropped.append({"file": m.filename, "class": "code_excluded", "why": "source code inside an archive"})
                        continue
                    z.extract(m, stage)
                    kept.append(m.filename)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                for m in t.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        return rej(f"unsafe path in archive: {m.name}")
                every = [m for m in t.getmembers() if m.isfile() and not m.name.lower().endswith(CODE)]
                chosen = [m for m in every if _named(m.name, members)] if members else []
                members = chosen or every
                dropped += [{"file": m.name, "class": "code_excluded", "why": "source code inside an archive"}
                            for m in t.getmembers() if m.isfile() and m.name.lower().endswith(CODE)]
                t.extractall(stage, members=members, filter="data")
                kept = [m.name for m in members]
        else:
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
    return [{"file": k, "from": name, "source_sha256": sha, "source_bytes": size} for k in kept], dropped


def _hf(s: dict, dest: str, cap: int, deny: list[str], rec: dict) -> None:
    src = s["source"]
    kind, _, repo = src[5:].partition("/")
    repo, _, rev = repo.partition("@")
    if any(d and d in repo.lower() for d in deny):
        raise FetchError("denied", "source denied by SH_DENY_SOURCES")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], check=True)
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().repo_info(repo, repo_type=kind.rstrip("s"), revision=rev or None, files_metadata=True)
    pats = s.get("include") or ["*"]
    files = [x for x in info.siblings or [] if any(fnmatch.fnmatch(x.rfilename, p) for p in pats)]
    need = sum(x.size or 0 for x in files)
    if need > cap:
        raise FetchError("storage", f"{need} bytes would pass the storage cap ({cap} bytes, SH_MAX_DATA_GB)")
    snapshot_download(repo, repo_type=kind.rstrip("s"), revision=info.sha, allow_patterns=pats,
                      ignore_patterns=[f"*{c}" for c in CODE], local_dir=dest)
    rec.update(revision=info.sha, files_matched=len(files))
    rec["admitted"] = [{"file": x.rfilename, "from": src} for x in files if not x.rfilename.lower().endswith(CODE)]


def _follow(f: Fetcher, targets: list[dict], dest: str, tmp: str, rec: dict, members: list[str] | None = None,
            limit: int = MAX_FOLLOW) -> None:
    if len(targets) > limit:              # never silent: the plan, the script author and the report are told
        rec["truncated"] = {"matched": len(targets), "followed": limit,
                            "not_followed": [t.get("name") or t["url"] for t in targets[limit:]][:30]}
    for t in targets[:limit]:
        row = {"url": t["url"]}
        try:
            p, info = f.get(t["url"], tmp, t.get("md5", ""), t.get("bytes"))
            row.update(final_url=info.get("final_url"), http_status=info.get("http_status"), redirects=info.get("redirects"),
                       from_cache=info.get("from_cache", False), bytes=info.get("bytes"))
            ok, bad = admit(p, t.get("name") or filename(info, t["url"]), dest, t.get("md5", ""), members)
            rec["admitted"] += ok
            rec["rejected"] += bad
            row["admitted"] = len(ok)
        except FetchError as e:
            row.update({"class": e.klass, "error": str(e)[:300], "tries": e.attempts})
        except Exception as e:                            # noqa: BLE001 - this code's own error: recorded as one
            row.update({"class": "bug", "error": f"{type(e).__name__}: {e}"[:300]})
        rec["followed"].append(row)


def _web(f: Fetcher, url: str, s: dict, dest: str, tmp: str, rec: dict) -> None:
    p, info = f.get(url, tmp)
    rec["attempts"].append({"url": url, "final_url": info.get("final_url"), "redirects": info.get("redirects"),
                            "http_status": info.get("http_status"), "content_type": info.get("content_type"),
                            "from_cache": info.get("from_cache", False), "retries": info.get("retries") or []})
    if not is_html(p, info):
        ok, bad = admit(p, filename(info, url), dest)
        rec["admitted"] += ok
        rec["rejected"] += bad
        return
    final = info.get("final_url") or url
    rec["landing"] = {"final_url": final, "sha256": _sha(p), "bytes": os.path.getsize(p)}
    include = s.get("include") or []
    targets: list[dict] = []
    members: list[str] | None = None
    listed = record_files(f, final, tmp)
    if listed is not None:
        rec["recovery"].append(f"documented mechanism: the repository records API for {final}")
        targets = [t for t in listed if not include or any(fnmatch.fnmatch(t["name"], pat) for pat in include)]
    else:
        with open(p, encoding="utf-8", errors="replace") as fh:
            page = fh.read()
        hosts = {urllib.parse.urlparse(final).netloc.lower(), urllib.parse.urlparse(url).netloc.lower()}
        links = html_links(page, final, hosts)
        rec["links"] = links[:200]
        targets = [{"url": u} for u in pick(links, include)]
        if include and not targets and 0 < len(pick(links, [])) <= 3:
            # `include` names no link on the page: the planner could not see the page, so it may have named what the
            # download holds. The page's own few data links are followed and the patterns filter their members.
            targets = [{"url": u} for u in pick(links, [])]
            members = include
            rec["recovery"].append(f"no link on the landing page matched {include}: its {len(targets)} data link(s) were followed "
                                   "and `include` was applied to their members")
    if not targets and "doi.org" in urllib.parse.urlparse(url).netloc:
        doi = urllib.parse.urlparse(url).path.lstrip("/")
        try:
            j, _ = f.json("https://api.datacite.org/dois/" + urllib.parse.quote(doi), tmp)
            targets = [{"url": u} for u in (j.get("data", {}).get("attributes", {}).get("contentUrl") or []) if u.startswith("http")]
            rec["recovery"].append("the DOI registry's content URLs" + (f": {len(targets)} found" if targets else ": none"))
        except FetchError as e:
            rec["recovery"].append(f"the DOI registry could not be read: {e.klass}")
    if not targets:
        rec["attempts"].append({"url": final, "class": "no_data",
                                "error": "the landing page exposes no downloadable file matching `include` or a data suffix"})
        return
    _follow(f, targets, dest, tmp, rec, members, MAX_RECORD_FILES if listed is not None else MAX_FOLLOW)


def acquire(f: Fetcher, s: dict, dest: str, tmp: str, idx: str) -> dict:
    """One source -> its record. Tries the citation as planned, then (bounded) the other printed readings of it."""
    rec: dict = {"source": s["source"], "dir": idx, "attempts": [], "admitted": [], "rejected": [], "followed": [], "recovery": []}
    if s.get("refused"):                  # a gate the caller closed for this source: recorded, nothing is sent
        rec.update(failure_class="gate", error=f"refused: {s['refused']}")
        return rec
    urls = [s["source"]] + [a for a in s.get("alternates") or [] if a != s["source"]][:MAX_ALTERNATES]
    for n, url in enumerate(urls):
        if n:
            rec["recovery"].append(f"another reading of the printed citation: {url}")
        try:
            if url.startswith("hf://"):
                _hf({**s, "source": url}, dest, f.cap, f.deny, rec)
            else:
                _web(f, url, s, dest, tmp, rec)
        except FetchError as e:
            rec["attempts"].append({"url": url, "class": e.klass, "error": str(e)[:300], "tries": e.attempts})
        except Exception as e:                            # noqa: BLE001
            k, msg = classify(e)
            rec["attempts"].append({"url": url, "class": k, "error": msg[:300]})
        if rec["admitted"]:
            break
    rec["failure_class"] = "" if rec["admitted"] else failure_class(rec)
    return rec


def failure_class(rec: dict) -> str:
    """Why nothing was admitted, as one class: the most actionable of everything that went wrong."""
    seen = [a.get("class") for a in rec["attempts"] + rec["followed"] if a.get("class")]
    if rec["rejected"] and not seen:
        seen = ["content_invalid"]
    for k in ("bug", "denied", "storage", "inaccessible", "transient", "missing", "content_invalid", "no_data", "protocol"):
        if k in seen:
            return k
    return "content_invalid" if rec["rejected"] else "no_data"


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
    return {"sources": recs, "files": files[:2000], "n_files": len(files), "bytes": sum(r["bytes"] for r in files),
            "status": "ok" if recs and all(r["admitted_files"] and not r.get("truncated") for r in recs)
            else "partial" if files else "none"}


def main(environ=os.environ, out_dir: str = "/data", tmp: str = "/root/.cache/referee-tmp",
         cache: str = "/root/.cache/referee-urls") -> dict:
    f = Fetcher(cache, int(environ["REFEREE_CAP"]), [d for d in environ.get("REFEREE_DENY", "").lower().split(",") if d])
    recs = [acquire(f, s, os.path.join(out_dir, str(i)), tmp, str(i)) for i, s in enumerate(json.loads(environ["REFEREE_SOURCES"]))]
    shutil.rmtree(tmp, ignore_errors=True)
    m = manifest(out_dir, recs)
    print("REFEREE_MANIFEST " + json.dumps(m))
    return m


if __name__ == "__main__":
    main()
