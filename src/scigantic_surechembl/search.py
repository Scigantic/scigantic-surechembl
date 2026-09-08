"""Searches: chemical structure over SureChEMBL's compound index, patents
by compound, and patents by Solr full-text query.

Structure search is asynchronous on the server. `POST /search/structure`
returns a job hash; `/search/{hash}/status` reports "Start/Loading
search..." until it reports "Searching finished." with a resultCount;
`/search/{hash}/results` then pages through hits. All verified live
2026-09-08, including two things the OpenAPI spec doesn't say:

- The request body must be wrapped as `{"StructureSearchRequest": {...}}`.
- `maxResults` in the request is ignored. The server caps every structure
  search at 10,000 hits (a benzene substructure search reports exactly
  10000 with maxResults=100), so `max_results` here is enforced by how
  many pages are fetched, not by the server.

Search hashes have `ttl: -1` (they persist), but nothing here relies on
that: each finished page of results is cached under the query itself, so
a repeated search is served locally and a lost hash just means a fresh
job.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from . import _client, cache
from ._ids import compound_ids
from .models import Compound, PatentHit

SEARCH_MODES = ("substructure", "similarity", "identical", "connectivity")

# Server-side ceiling on structure-search hits, verified live.
STRUCTURE_SEARCH_CAP = 10_000

_POLL_INITIAL = 0.5
_POLL_MAX = 5.0
_DEFAULT_PAGE = 100


def structure_search(
    structure: str,
    mode: str = "substructure",
    max_results: int = 100,
    timeout: float = 300.0,
) -> list[Compound]:
    """Run one of SureChEMBL's four structure searches over its whole
    compound index and return up to `max_results` hits.

    `structure` is a SMILES, or SMARTS for substructure. `mode` is one of
    "substructure", "similarity" (Tanimoto on hashed fingerprints, hits
    carry `.similarity`), "identical" (every feature must match, so a
    non-stereo query only matches non-stereo targets) or "connectivity"
    (same atom connectivity, any stereo/isotope). These are the search
    types SureChEMBL's UI offers, under the names its docs use.

    Hits are returned in the server's order, which for similarity is NOT
    strictly by score (verified live: 1.0, 1.0, 0.96, 1.0, ...); sort on
    `.similarity` if you need ranking. Hits beyond the server's 10,000
    cap are not reachable through this endpoint; for an exhaustive
    substructure sweep use the bulk parquet (see the bulk module) with
    RDKit locally.
    """
    if mode not in SEARCH_MODES:
        raise ValueError(f"mode must be one of {SEARCH_MODES}, not {mode!r}")
    if max_results <= 0:
        return []
    page_size = min(_DEFAULT_PAGE, max_results)
    cache_key = cache.key("structure_search", structure, mode, max_results, page_size)
    cached = cache.get(cache_key)
    if cached is not None:
        return [Compound.from_api(r) for r in cached]

    search_hash, total = _submit_and_wait(structure, mode, timeout)
    records: list[dict[str, Any]] = []
    page = 1
    while len(records) < min(max_results, total):
        data = _client.request(
            "GET",
            f"/search/{search_hash}/results",
            params={"page": page, "max_results": page_size},
            cacheable=False,
        )
        batch = (data.get("results") or {}).get("structures") or []
        if not batch:
            break
        records.extend(batch)
        page += 1
    records = records[:max_results]
    cache.put(cache_key, records)
    return [Compound.from_api(r) for r in records]


class _SearchFailed(Exception):
    """The server reported the job itself failed (not a transport error)."""


def _submit_and_wait(structure: str, mode: str, timeout: float) -> tuple[str, int]:
    """Submit a structure search and poll it to completion; returns
    (hash, hit count). A job the server reports as failed ("Search not
    complete due to internal error.", seen live 2026-09-08 on a
    substructure query that had succeeded minutes earlier) is
    resubmitted once, since it was transient the time it was observed;
    a second failure is raised rather than polled until the timeout."""
    last_error = ""
    for attempt in range(2):
        job = _client.request(
            "POST",
            "/search/structure",
            json_body={"StructureSearchRequest": {"struct": structure, "structSearchType": mode}},
            cacheable=False,
        )
        search_hash = str(job["hash"])
        try:
            return search_hash, _wait_for_search(search_hash, timeout)
        except _SearchFailed as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(2.0)
    raise _client.SureChEMBLError(f"structure search failed on the server twice: {last_error}")


def _wait_for_search(search_hash: str, timeout: float) -> int:
    """Poll a search job until it reports finished; return its hit count.
    Poll interval starts at 0.5s and doubles to a 5s cap, so a fast job
    is picked up quickly and a slow one is not hammered. The status
    message is the only signal: "Start/Loading search..." while running,
    "Searching finished." when done, and a message containing "error"
    when the job died (raised as _SearchFailed immediately rather than
    polled to the deadline)."""
    deadline = time.monotonic() + timeout
    wait = _POLL_INITIAL
    while True:
        status = _client.request("GET", f"/search/{search_hash}/status", cacheable=False)
        message = str(status.get("message", ""))
        if message.startswith("Searching finished"):
            return int(status.get("resultCount") or 0)
        if "error" in message.lower():
            raise _SearchFailed(f"search {search_hash}: {message}")
        if time.monotonic() > deadline:
            raise _client.SureChEMBLError(
                f"structure search {search_hash} not finished after {timeout:.0f}s (last status: {message!r})"
            )
        time.sleep(wait)
        wait = min(wait * 2, _POLL_MAX)


def similar_compounds(structure: str, max_results: int = 100, timeout: float = 300.0) -> list[Compound]:
    """structure_search(mode="similarity"): hits carry `.similarity`."""
    return structure_search(structure, "similarity", max_results, timeout)


def substructure_search(structure: str, max_results: int = 100, timeout: float = 300.0) -> list[Compound]:
    """structure_search(mode="substructure"): SMILES or SMARTS query."""
    return structure_search(structure, "substructure", max_results, timeout)


def patents_for_compound(
    ids: int | str | Iterable[int | str],
    max_results: int = 100,
    page_size: int = _DEFAULT_PAGE,
) -> list[PatentHit]:
    """Patents in which one or more compounds were found, newest-indexed
    first as SureChEMBL orders them. Accepts one id or many (a
    many-id query returns documents containing ANY of them).

    Exhaustive for a rare compound, and the right call for "which patents
    mention this exact structure". For a common one it is not the tool:
    aspirin (id 1353) is in 694,428 documents, which at 100 per page is
    thousands of round trips. `count_patents_for_compound()` gives the
    total in one request so a caller can decide.
    """
    wanted = compound_ids([ids] if isinstance(ids, (int, str)) else ids)
    if not wanted or max_results <= 0:
        return []
    id_param = ",".join(map(str, wanted))
    hits: list[PatentHit] = []
    page = 1
    while len(hits) < max_results:
        data = _client.request(
            "POST",
            "/search/documents_for_structures",
            params={"chemicalIds": id_param, "page": page, "itemsPerPage": min(page_size, max_results - len(hits))},
        )
        docs = (data.get("results") or {}).get("documents") or []
        if not docs:
            break
        hits.extend(PatentHit.from_api(d) for d in docs)
        total = int((data.get("results") or {}).get("total_hits") or 0)
        if len(hits) >= total:
            break
        page += 1
    return hits[:max_results]


def count_patents_for_compound(ids: int | str | Iterable[int | str]) -> int:
    """How many patents contain the compound(s), in one request."""
    wanted = compound_ids([ids] if isinstance(ids, (int, str)) else ids)
    if not wanted:
        return 0
    data = _client.request(
        "POST",
        "/search/documents_for_structures",
        params={"chemicalIds": ",".join(map(str, wanted)), "page": 1, "itemsPerPage": 1},
    )
    return int((data.get("results") or {}).get("total_hits") or 0)


def search_patents(query: str, max_results: int = 100, page_size: int = _DEFAULT_PAGE) -> list[PatentHit]:
    """Full-text patent search with SureChEMBL's Solr syntax, passed
    through untouched.

    Plain terms search all text; field prefixes restrict them, e.g.
    `ttl:kinase`, `clm:"sodium channel"`, `asg:novartis`, `pdyear:2024`,
    `cpc:C07D`, `pn:WO-2016144528-A1`, combined with AND/OR/NOT and
    parentheses. The full field list is in SureChEMBL's docs
    ("Solr query field names and examples"). Hits carry title,
    publication date and assignee when the index has them.
    """
    if max_results <= 0:
        return []
    hits: list[PatentHit] = []
    page = 1
    while len(hits) < max_results:
        data = _client.request(
            "POST",
            "/search/content",
            params={"query": query, "page": page, "itemsPerPage": min(page_size, max_results - len(hits))},
        )
        docs = (data.get("results") or {}).get("documents") or []
        if not docs:
            break
        hits.extend(PatentHit.from_api(d) for d in docs)
        total = int((data.get("results") or {}).get("total_hits") or 0)
        if len(hits) >= total:
            break
        page += 1
    return hits[:max_results]


def count_patents(query: str) -> int:
    """Total hits for a Solr query, in one request."""
    data = _client.request("POST", "/search/content", params={"query": query, "page": 1, "itemsPerPage": 1})
    return int((data.get("results") or {}).get("total_hits") or 0)
