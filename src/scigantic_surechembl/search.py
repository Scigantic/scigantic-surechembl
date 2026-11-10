"""Searches: chemical structure over SureChEMBL's compound index, patents
by compound, and patents by Solr full-text query.

Structure search is synchronous since SureChEMBL's late-2026 API change.
`POST /search/structure` takes `{struct, struct_search_type, threshold,
page, items_per_page}` and answers at once with one page of hits; the
server caches the full result per (structure, type, threshold), so later
pages come back immediately (`"cached": true`). The old submit-a-job,
poll-a-hash flow is gone. Verified live 2026-11-09:

- A page carries ids only: `chemical_ids` is a list of ints, or of
  `[id, similarity]` pairs for a similarity search. The compound records
  are fetched afterwards through `/chemical/id` in batches, so a search
  costs its result pages plus one request per 500 hits.
- `total` is the full hit count; a page past the end is an empty list.
- The server still caps a search at 10,000 hits (a substructure search
  for aspirin reports exactly 10000).
- Similarity hits now arrive in descending score order.
- An unparseable SMILES or an unknown search type is a 422; raised here
  as ValueError.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from . import _client, cache
from ._ids import compound_ids
from .models import Compound, PatentHit

SEARCH_MODES = ("substructure", "similarity", "identical", "connectivity")

# A substructure query this small matches most of the 31M-compound index.
# Observed twice on 2026-09-08 (the old API): a single-atom query ("C",
# then "[#6]") and a bare cyclohexane sat in the queue for minutes and every
# substructure search submitted afterwards, by anyone, failed for about an
# hour. The new service is a different implementation, but the guard costs
# nothing and protects a shared EMBL-EBI service: a caller gets nothing
# useful from a 10,000-hit cap on millions of matches anyway.
_MIN_SUBSTRUCTURE_ATOMS = 5
_ATOM_TOKEN_RE = re.compile(r"\[[^\]]+\]|Cl|Br|[BCNOSPFI]|[bcnosp]")

# Server-side ceiling on structure-search hits, verified live.
STRUCTURE_SEARCH_CAP = 10_000

_DEFAULT_PAGE = 100
# Verified live 2026-11-09: items_per_page=1000 answers in under a second.
_STRUCTURE_PAGE = 1000


def structure_search(
    structure: str,
    mode: str = "substructure",
    max_results: int = 100,
    timeout: float = 300.0,
    threshold: float | None = None,
) -> list[Compound]:
    """Run one of SureChEMBL's four structure searches over its whole
    compound index and return up to `max_results` hits as full Compound
    records.

    `structure` is a SMILES (the API validates it as one and rejects
    anything else with a 422, raised here as ValueError). `mode` is one of
    "substructure", "similarity" (hits carry `.similarity`, best first),
    "identical" (every feature must match, so a non-stereo query only
    matches non-stereo targets) or "connectivity" (same atom
    connectivity, any stereo/isotope). `threshold` is the similarity
    cut-off, 0 to 1, used only for "similarity"; None leaves the
    server's default (0.8). `timeout` is the read timeout per request.

    Hits beyond the server's 10,000 cap are not reachable through this
    endpoint; for an exhaustive substructure sweep use the bulk parquet
    (see the bulk module) with RDKit locally.
    """
    pairs = _structure_ids(structure, mode, max_results, timeout, threshold)
    if not pairs:
        return []
    from .compounds import compounds

    found = compounds([cid for cid, _ in pairs])
    out: list[Compound] = []
    for cid, sim in pairs:
        # An id the search returned but /chemical/id does not (not seen
        # live, but the two are separate indexes) is kept as a bare record
        # rather than dropped, so the hit count stays honest.
        c = found.get(cid) or Compound(id=cid)
        out.append(replace(c, similarity=sim) if sim is not None else c)
    return out


def _structure_ids(
    structure: str,
    mode: str,
    max_results: int,
    timeout: float = 300.0,
    threshold: float | None = None,
) -> list[tuple[int, float | None]]:
    """(id, similarity) pairs for a structure search, paged and cached.
    similarity is None outside similarity mode."""
    if mode not in SEARCH_MODES:
        raise ValueError(f"mode must be one of {SEARCH_MODES}, not {mode!r}")
    if mode == "substructure" and len(_ATOM_TOKEN_RE.findall(structure)) < _MIN_SUBSTRUCTURE_ATOMS:
        raise ValueError(
            f"substructure query {structure!r} has fewer than {_MIN_SUBSTRUCTURE_ATOMS} atoms; a query this "
            "broad matches most of SureChEMBL and has been observed to take the search service down. "
            "Add more of the scaffold, or use the bulk parquet with RDKit for a whole-corpus sweep."
        )
    if threshold is not None and not 0 <= threshold <= 1:
        raise ValueError(f"threshold must be between 0 and 1, not {threshold!r}")
    if max_results <= 0:
        return []
    max_results = min(max_results, STRUCTURE_SEARCH_CAP)
    page_size = min(_STRUCTURE_PAGE, max_results)
    cache_key = cache.key("structure_search", structure, mode, threshold if mode == "similarity" else None, max_results)
    cached = cache.get(cache_key)
    if cached is not None:
        return [(int(i), None if s is None else float(s)) for i, s in cached]

    pairs: list[tuple[int, float | None]] = []
    seen: set[int] = set()
    page = 1
    while len(pairs) < max_results:
        body: dict[str, Any] = {
            "struct": structure,
            "struct_search_type": mode,
            "page": page,
            "items_per_page": page_size,
        }
        if mode == "similarity" and threshold is not None:
            body["threshold"] = threshold
        try:
            data = _client.request("POST", "/search/structure", json_body=body, cacheable=False, timeout=timeout)
        except _client.SureChEMBLError as exc:
            if exc.http_status == 422:
                raise ValueError(f"SureChEMBL rejected the structure search: {exc}") from exc
            raise
        batch = (data.get("chemical_ids") or []) if isinstance(data, dict) else []
        before = len(pairs)
        for item in batch:
            if isinstance(item, list) and item:
                cid, sim = int(item[0]), (float(item[1]) if len(item) > 1 and item[1] is not None else None)
            else:
                cid, sim = int(item), None
            if cid not in seen:
                seen.add(cid)
                pairs.append((cid, sim))
        total = int(data.get("total") or 0) if isinstance(data, dict) else 0
        if not batch or len(pairs) == before or len(pairs) >= total:
            break
        page += 1
    pairs = pairs[:max_results]
    if pairs:
        cache.put(cache_key, [[i, s] for i, s in pairs])
    return pairs


def similar_compounds(
    structure: str, max_results: int = 100, timeout: float = 300.0, threshold: float | None = None
) -> list[Compound]:
    """structure_search(mode="similarity"): hits carry `.similarity`."""
    return structure_search(structure, "similarity", max_results, timeout, threshold)


def substructure_search(structure: str, max_results: int = 100, timeout: float = 300.0) -> list[Compound]:
    """structure_search(mode="substructure"): a SMILES query."""
    return structure_search(structure, "substructure", max_results, timeout)


# Verified live 2026-11-09: both document searches answer itemsPerPage=1000
# (documents_for_structures in about 3.5 s for aspirin). The old API
# failed above 300 with a Solr "414 URI Too Long", so 500 is kept for
# documents_for_structures as a margin.
_MAX_PAGE_DOCUMENTS_FOR_STRUCTURES = 500
_MAX_PAGE_CONTENT = 1000


def patents_for_compound(
    ids: int | str | Iterable[int | str],
    max_results: int = 100,
    page_size: int = _DEFAULT_PAGE,
) -> list[PatentHit]:
    """Patents in which a compound was found, in SureChEMBL's order.

    Several ids narrow the search to documents containing ALL of them
    (an intersection, verified again on the new API 2026-11-09: aspirin
    is in 699,345 documents, SCHEMBL15082 in 86, the pair in 58). So this
    is also the co-occurrence query: "patents that mention both A and B".
    For a union, run the ids separately and merge on doc_id. At most 500
    ids per call. The ids travel as a repeated `chemicalIds` parameter;
    the comma-joined form the old API took is a Solr error now.

    Exhaustive for a rare compound, and the right call for "which patents
    mention this exact structure". For a common one it is not the tool:
    aspirin at 500 per page is 1,400 round trips.
    `count_patents_for_compound()` gives the total in one request so a
    caller can decide. Hits from this endpoint carry no family id.
    """
    wanted = _compound_id_list(ids)
    if not wanted or max_results <= 0:
        return []
    return _page_documents(
        "/search/documents_for_structures",
        {"chemicalIds": [str(i) for i in wanted]},
        max_results,
        min(page_size, _MAX_PAGE_DOCUMENTS_FOR_STRUCTURES),
    )


def _page_documents(path: str, base: dict[str, Any], max_results: int, page_size: int) -> list[PatentHit]:
    """Shared paging for the two document searches. De-duplicates on
    doc_id and stops on the server's numFound, an empty page, or a page
    that added nothing new. (The new API reports `start: 0` on every
    page of documents_for_structures, so `start` is not trusted.)

    The page size is fixed for the whole loop and only the final list is
    trimmed: shrinking itemsPerPage on the last page to "just what's
    missing" moves the server's page boundaries, so page 2 at 50 per
    page re-serves documents 51-100 that page 1 at 250 already returned
    (found by the stress test: 250 results back for max_results=300)."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    page_size = min(page_size, max_results)
    hits: list[PatentHit] = []
    seen: set[str] = set()
    page = 1
    while len(hits) < max_results:
        data = _client.request("GET", path, params={**base, "page": page, "itemsPerPage": page_size})
        docs = (data.get("docs") or []) if isinstance(data, dict) else []
        before = len(hits)
        for d in docs:
            hit = PatentHit.from_api(d)
            if hit.doc_id not in seen:
                seen.add(hit.doc_id)
                hits.append(hit)
        total = int(data.get("numFound") or 0)
        if not docs or len(hits) == before or len(hits) >= total:
            break
        page += 1
    return hits[:max_results]


# 500 ids (as 500 repeated chemicalIds parameters, about 10 KB of query
# string) answered normally on 2026-11-09. The old API failed at 1,000 with
# a Solr "414 URI Too Long"; the limit is kept rather than re-probed.
_MAX_IDS_PER_SEARCH = 500


def _compound_id_list(ids: int | str | Iterable[int | str]) -> list[int]:
    wanted = compound_ids([ids] if isinstance(ids, (int, str)) else ids)
    if len(wanted) > _MAX_IDS_PER_SEARCH:
        raise ValueError(
            f"at most {_MAX_IDS_PER_SEARCH} compound ids per search (got {len(wanted)}); the server rejects "
            "longer lists, and since several ids mean an intersection the list cannot be split for you"
        )
    return wanted


def count_patents_for_compound(ids: int | str | Iterable[int | str]) -> int:
    """How many patents contain the compound (or, for several ids, all of
    them; see patents_for_compound()), in one request."""
    wanted = _compound_id_list(ids)
    if not wanted:
        return 0
    data = _client.request(
        "GET",
        "/search/documents_for_structures",
        params={"chemicalIds": [str(i) for i in wanted], "page": 1, "itemsPerPage": 1},
    )
    return int(data.get("numFound") or 0)


def search_patents(query: str, max_results: int = 100, page_size: int = _DEFAULT_PAGE) -> list[PatentHit]:
    """Full-text patent search with SureChEMBL's Solr syntax, one hit per
    publication.

    Plain terms search all text; field prefixes restrict them, e.g.
    `ttl:kinase`, `clm:"sodium channel"`, `asg:novartis`, `pdyear:2024`,
    `cpc:C07D`, `fam:37847165`, `pn:"WO-2016144528-A1"`, combined with
    AND/OR/NOT and parentheses. Quote a publication number. The full
    field list is in SureChEMBL's docs ("Solr query field names and
    examples"). Hits carry title, publication date, assignees and the
    DOCDB family id when the index has them.

    Since the late-2026 API change the server ANDs every query with
    `pnctry:(US OR EP OR WO OR JP OR CN)` (it echoes the rewritten query
    back), so only publications from those five offices are searchable
    here; a DE or AU publication is never a hit, even when the patent
    endpoint has it. Results are requested ungrouped (`grouped=false`);
    the API's default groups hits by family. An empty query is refused
    locally with ValueError (the server would return all 125 million
    documents), and a Solr syntax error is raised with Solr's own message
    as SureChEMBLError.
    """
    _check_query(query)
    if max_results <= 0:
        return []
    return _page_documents(
        "/search/content", {"query": query, "grouped": "false"}, max_results, min(page_size, _MAX_PAGE_CONTENT)
    )


def count_patents(query: str) -> int:
    """Total hits for a Solr query, in one request (same five-office
    restriction as search_patents())."""
    _check_query(query)
    data = _client.request(
        "GET", "/search/content", params={"query": query, "page": 1, "itemsPerPage": 1, "grouped": "false"}
    )
    return int(data.get("numFound") or 0)


def _check_query(query: str) -> None:
    if not query or not query.strip():
        raise ValueError("query is empty")
