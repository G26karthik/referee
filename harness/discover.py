"""Finding the public datasets a paper names but does not link.

A paper prints "CIFAR-10-C" or "the Porto taxi trajectories" without a URL. Discovery asks open
registries (Zenodo, DataCite, the Hugging Face dataset and model hubs) for records of that name and returns what
they hold; a planner picks among them and cites the record in a plan's `acquire`. The harness, not the
model, made the request and kept the answer, so a plan can only acquire a source a registry returned
(tasks._acquire checks `discovery.jsonl`), and every search leaves its query, the records and the
errors behind. Discovery sends only the text of a query, needs SH_ALLOW_DATA_SEARCH and the network,
and grants nothing else: no clone, no code, no execution (those are separate gates). A denied source
(SH_DENY_SOURCES) is never returned, and software records are not datasets.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import state

REGISTRIES = ("zenodo", "datacite", "huggingface", "huggingface-models")
PER_REGISTRY = 6      # ponytail: 6 records per registry; a planner narrows the query rather than paging


def _get(url: str) -> object:
    """JSON from a registry API: two tries, the second after a pause, on a transient failure only."""
    err: Exception | None = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "referee", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = e
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            err = e
        time.sleep(2 * (i + 1))
    raise OSError(f"{type(err).__name__}: {err}")


def _zenodo(q: str, get) -> list[dict]:
    j = get("https://zenodo.org/api/records?" + urllib.parse.urlencode({"q": q, "size": PER_REGISTRY, "sort": "bestmatch"}))
    out = []
    for h in (j.get("hits") or {}).get("hits") or []:
        m, files = h.get("metadata") or {}, h.get("files") or []
        if (m.get("resource_type") or {}).get("type") == "software":
            continue
        out.append({"source": f"https://zenodo.org/records/{h.get('id')}", "registry": "zenodo", "title": m.get("title", ""),
                    "creators": [c.get("name", "") for c in m.get("creators") or []][:3], "year": str(m.get("publication_date", ""))[:4],
                    "doi": h.get("doi", ""), "license": (m.get("license") or {}).get("id", ""),
                    "description": re.sub(r"<[^>]+>", " ", m.get("description", ""))[:300].strip(),
                    "files": [{"name": f.get("key"), "bytes": f.get("size")} for f in files][:15],
                    "total_bytes": sum(f.get("size") or 0 for f in files), "access_right": m.get("access_right", "")})
    return out


def _datacite(q: str, get) -> list[dict]:
    j = get("https://api.datacite.org/dois?" + urllib.parse.urlencode(
        {"query": q, "resource-type-id": "dataset", "page[size]": PER_REGISTRY}))
    out = []
    for d in j.get("data") or []:
        a = d.get("attributes") or {}
        doi = a.get("doi", "")
        if not doi or doi.startswith("10.48550/"):        # an arXiv preprint is a paper, not a dataset
            continue
        out.append({"source": f"https://doi.org/{doi}", "registry": "datacite", "title": (a.get("titles") or [{}])[0].get("title", ""),
                    "creators": [c.get("name", "") for c in a.get("creators") or []][:3], "year": str(a.get("publicationYear") or ""),
                    "doi": doi, "publisher": a.get("publisher", ""), "landing": a.get("url", ""),
                    "license": ((a.get("rightsList") or [{}])[0].get("rightsIdentifier") or ""),
                    "description": " ".join(x.get("description", "") for x in (a.get("descriptions") or [])[:1])[:300]})
    return out


# What a hub search returns with each repository. With `expand`, the answer holds only the fields named, so the
# card fields are named too; `safetensors` exists for models only, and `usedStorage` is refused by the list endpoints
# (HTTP 400, measured 2026-10-01): an unknown name fails the whole search, so a refused expansion falls back to the
# plain search (the gating and size fields then read unknown, never false).
_HF_EXPAND = {"datasets": ("author", "createdAt", "downloads", "tags", "gated", "private"),
              "models": ("author", "createdAt", "downloads", "tags", "gated", "private", "safetensors")}
_DTYPE_BYTES = {"F64": 8, "I64": 8, "U64": 8, "F32": 4, "I32": 4, "U32": 4, "F16": 2, "BF16": 2, "I16": 2, "U16": 2,
                "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1, "BOOL": 1}


def _hub(kind: str, registry: str, q: str, get) -> list[dict]:
    """Hub repositories with what a plan seal needs to judge a blocker: `gated` (False, "auto" or "manual"), `private`,
    `size_bytes` (weights from the safetensors dtype counts, else the repository's storage; `size_basis` says which)
    and `params`; None where the hub did not say."""
    base = {"search": q, "limit": PER_REGISTRY}
    try:
        j = get(f"https://huggingface.co/api/{kind}?" + urllib.parse.urlencode({**base, "expand": list(_HF_EXPAND[kind])},
                                                                              doseq=True))
    except OSError as e:
        if "400" not in str(e):
            raise
        j = get(f"https://huggingface.co/api/{kind}?" + urllib.parse.urlencode(base))
    out = []
    for d in [d for d in j if isinstance(d, dict) and d.get("id")][:PER_REGISTRY] if isinstance(j, list) else []:
        st = d.get("safetensors") if isinstance(d.get("safetensors"), dict) else {}
        by = st.get("parameters") if isinstance(st.get("parameters"), dict) else {}
        weights = sum(n * _DTYPE_BYTES[t.upper()] for t, n in by.items()) if by and all(
            str(t).upper() in _DTYPE_BYTES and isinstance(n, int) for t, n in by.items()) else None
        size, basis = ((weights, "safetensors") if weights else (d["usedStorage"], "usedStorage")
                       if isinstance(d.get("usedStorage"), int) else (None, ""))
        out.append({"source": f"hf://{kind}/{d['id']}", "registry": registry, "title": d["id"], "creators": [d.get("author", "")],
                    "year": str(d.get("createdAt", ""))[:4], "downloads": d.get("downloads"), "tags": (d.get("tags") or [])[:8],
                    "gated": d.get("gated"), "private": d.get("private"), "size_bytes": size, "size_basis": basis,
                    **({"params": st["total"]} if isinstance(st.get("total"), int) else {})})
    return out


def _huggingface(q: str, get) -> list[dict]:
    return _hub("datasets", "huggingface", q, get)


def _huggingface_models(q: str, get) -> list[dict]:
    """Trained checkpoints (weights and configs; a repository's code is never admitted by the fetcher)."""
    return _hub("models", "huggingface-models", q, get)


_SEARCH = {"zenodo": _zenodo, "datacite": _datacite, "huggingface": _huggingface, "huggingface-models": _huggingface_models}


def _spent(done: list[dict]) -> int:
    """Searches that count against the budget: one where some registry answered (or a listing that came back). A
    search every registry failed (an HTTP error, a timeout) learned nothing and is never a reason to stop searching."""
    return sum(1 for r in done if (any(not v.get("error") for v in r["results"].values()) if "results" in r
                                   else not r.get("error")))


def log_path(cfg: state.Config, pid: str) -> Path:
    return state.pdir(cfg, pid) / "discovery.jsonl"


def records(cfg: state.Config, pid: str) -> list[dict]:
    f = log_path(cfg, pid)
    return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()] if f.exists() else []


def search(cfg: state.Config, pid: str, query: str, registry: str = "", get=_get) -> dict:
    """Ask the registries for records named by `query`; the answer is logged (discovery.jsonl) and returned."""
    query = " ".join((query or "").split())[:200]
    if not (cfg.allow_network and cfg.allow_data_search):
        return {"error": "public data search is off (SH_ALLOW_DATA_SEARCH and SH_ALLOW_NETWORK): nothing was sent"}
    if not query:
        return {"error": "a query names the dataset (its title as the paper prints it; a phrase in quotes for an exact title)"}
    if registry and registry not in REGISTRIES:
        return {"error": f"registry is one of {list(REGISTRIES)}"}
    regs = [registry] if registry else list(REGISTRIES)
    with state.lock(state.pdir(cfg, pid) / ".discovery.lock"):
        done = records(cfg, pid)
        # The same query to the same registries again is answered from the log (an answer with an error is asked again).
        if old := next((r for r in done if "results" in r and r["query"].casefold() == query.casefold() and sorted(r["results"])
                        == sorted(regs) and not any(v.get("error") for v in r["results"].values())), None):
            return {**old, "reused": True}
        if _spent(done) >= cfg.max_discoveries:
            return {"error": f"the discovery budget of {cfg.max_discoveries} searches (SH_MAX_DISCOVERIES) is spent; "
                             "narrow the query or record the dataset as not found"}
        results = {}
        for reg in regs:
            try:
                found = _SEARCH[reg](query, get)
                dropped = [c for c in found if any(d in json.dumps(c).lower() for d in cfg.deny_sources)]
                results[reg] = {"candidates": [c for c in found if c not in dropped], "denied": len(dropped), "error": ""}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                results[reg] = {"candidates": [], "denied": 0, "error": f"{type(e).__name__}: {e}"[:300]}
        rec = {"id": f"D{len(done) + 1}", "query": query, "results": results}
        state.append_jsonl(log_path(cfg, pid), rec)
    return rec


def files(cfg: state.Config, pid: str, url: str, get=_get) -> dict:
    """The files of a repository record a paper cites (a record page, or a DOI that resolves to one), from its
    documented records API: names, sizes, checksums. A planner needs them to choose `include`; without them it can
    only guess with a suffix pattern that takes everything. Counts against the same search budget."""
    if not (cfg.allow_network and cfg.allow_data_search):
        return {"error": "public data search is off (SH_ALLOW_DATA_SEARCH and SH_ALLOW_NETWORK): nothing was sent"}
    from . import fetcher
    url = url.strip()
    zen = canon(url)
    page = f"https://{zen}" if zen.startswith("zenodo.org/") else url
    api = fetcher.record_api(page)
    if not api:
        return {"error": f"{url!r} is not a record page of a repository with a records API "
                         f"({', '.join(fetcher.RECORD_APIS)}); a landing page's files are found when it is fetched"}
    with state.lock(state.pdir(cfg, pid) / ".discovery.lock"):
        done = records(cfg, pid)
        if old := next((r for r in done if r.get("api") == api and r.get("files") and not r.get("error")), None):
            return {**old, "reused": True}                       # the same record's listing: answered from the log
        if _spent(done) >= cfg.max_discoveries:
            return {"error": f"the discovery budget of {cfg.max_discoveries} searches (SH_MAX_DISCOVERIES) is spent"}
        try:
            listing = fetcher.record_listing(get(api))
            rec = {"id": f"F{len(done) + 1}", "files_of": url, "api": api, "files": [
                {"name": f["name"], "bytes": f["bytes"], "md5": f["md5"]} for f in listing],
                "total_bytes": sum(f["bytes"] or 0 for f in listing)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            rec = {"id": f"F{len(done) + 1}", "files_of": url, "api": api, "files": [], "error": f"{type(e).__name__}: {e}"[:300]}
        state.append_jsonl(log_path(cfg, pid), rec)
    return rec


def canon(src: str) -> str:
    """One key for the several names of a record: a Zenodo DOI page and the Zenodo record are the same source."""
    s = re.sub(r"^https?://(?:www\.)?", "", (src or "").strip().lower()).rstrip("/")
    m = re.fullmatch(r"(?:dx\.)?doi\.org/10\.5281/zenodo\.(\d+)", s) or re.fullmatch(r"zenodo\.org/records?/(\d+)", s)
    return f"zenodo.org/records/{m[1]}" if m else s


def returned(cfg: state.Config, pid: str, did: str, src: str) -> dict | None:
    """The candidate record `did` returned for `src` (as the registry named it), else None."""
    rec = next((r for r in records(cfg, pid) if r["id"] == did), None)
    for reg in (rec or {}).get("results", {}).values():
        for c in reg.get("candidates") or []:
            if canon(c["source"]) == canon(src) or canon(c.get("landing", "")) == canon(src):
                return c
    return None
