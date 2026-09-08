# Changelog

## 0.1.1

Second stress round, against the published 0.1.0 wheel.

- **The cache can no longer make a lookup fail.** A read-only cache
  directory raised `PermissionError` from every call; a cache path that
  was a file, or an unwritable `SCIGANTIC_SURECHEMBL_CACHE`, raised from
  the first call. Each now turns caching off for the process with one
  warning and the lookup proceeds live. A cache entry missing its
  `value` key raised `KeyError`; it and a non-numeric timestamp are now
  misses. `clear()` tolerates another thread removing a file first.
- **`Patent.title` matches SureChEMBL's own choice.** A record can carry
  two English titles (the office's and a vendor's descriptive one, in
  either order). The parser took the first; SureChEMBL's bulk
  `patents.title` is the last, on every such record found. `title` now
  follows that rule and a new `titles` field carries all of them.
- **`patents_for_compound()` with several ids is an intersection**, not
  the union the docstring and README claimed: documents containing ALL
  the given compounds (aspirin 694,428, caffeine 202,385, both 47,721;
  verified against the documents' own chemistry). Documented as the
  co-occurrence query it is; more than 500 ids now raise `ValueError`
  (1,000 overflows a Solr URI server-side).
- **`substructure_search()` refuses queries with fewer than 5 atoms**
  (`ValueError`, nothing sent). A single-atom or bare-small-ring query
  was observed twice to wedge SureChEMBL's substructure worker for
  everyone for about an hour.
- Documented from the consistency check: REST serves ~18% more compound
  ids than the bulk table, all with zero patent occurrences; where both
  have a compound, structure fields agreed on 222 of 222; extracted
  chemistry agreed exactly on all 6 patents compared (up to 4,068
  compounds); ~5% of bulk publications are not served by the REST
  document endpoint.

## 0.1.0

First release.

- Compounds by id (`1353` or `SCHEMBL1353`), batched ids, name, SMILES,
  and InChIKey (through UniChem, since SureChEMBL has no InChIKey
  endpoint); PNG depiction.
- Structure search in SureChEMBL's four modes (substructure, similarity,
  identical, connectivity) over the asynchronous job API, paged, with
  the server's 10,000-hit cap documented.
- Patents by compound id(s), Solr full-text search with field prefixes,
  and single-request counts for both.
- Full patent documents parsed to a typed `Patent` (title, dates,
  abstract/claims/description text, de-duplicated parties, CPC/IPCR
  symbols, family, priorities, citations, legal events, PDF link) with
  the raw record kept; per-document extracted chemistry; DOCDB family
  id and members.
- Bulk parquet releases on EBI read in place with DuckDB: release
  listing, pruned lookups by compound id, patent id and patent's
  compounds, arbitrary SQL over views, and a resumable downloader.
- Token-bucket pacing (5 req/s), retry on 429/502/503/504, a 30-day
  on-disk cache on by default, mypy strict, Python 3.10 to 3.14.

Hardened before release by a stress battery (see README, "Testing").
What it found and what changed:

- Publication numbers with letters in the number part (`JP-S60174822-A`,
  `JP-WO2018116905-A1`, `US-RE43229-E1`, `US-PP22546-P3`, `US-D651743-S1`,
  `US-H2267-H1`) were rejected by a digits-only parser; 11 of 69 sampled
  documents. Now accepted, hyphenated or not.
- Structure-search paging returned duplicates: the server's pages run
  short of its own count and a page past the last repeats. Results are
  de-duplicated and paging stops at the server's page count.
- Document paging lost results when `max_results` was not a multiple
  of the page size (shrinking the last page's size moved the server's
  page boundaries: 250 back for 300 asked). Page size is now fixed for
  the whole loop; `patents_for_compound()` caps it at 250 (500 overflows
  a Solr URI server-side), `search_patents()` at 1,000.
- UniChem hung on ~1 in 6 requests. InChIKey lookups are now hedged
  across UniChem's two endpoints with an 8 s read timeout (p90 16.6 s to
  4.7 s over 60 keys) and malformed keys are rejected before any request.
- The bulk helpers shared one DuckDB connection across threads, which
  is not safe (5 of 8 concurrent calls failed); each call now takes a
  cursor. `release_tables()` added; `sql()`/`connect()` name a table a
  release lacks instead of a raw 404; the first release's BLOB
  `rdk_smiles` column is decoded.
- `by_name()` raises `ValueError` for an empty name or one containing
  `/` (the endpoint cannot take either) instead of an opaque API error;
  `structure_image()` raises on a non-PNG body instead of returning
  empty bytes; non-int/str compound ids raise `ValueError`, not
  `TypeError`.
- A dropped connection is retried 3 times, not 5 (the case seen was
  Solr closing the connection on a query it could not finish), and TCP
  connect is bounded at 10 s separately from the read timeout (an
  unreachable host now fails in 33 s, not minutes).
- The CLI prints `error: ...` and exits 2 (bad input) or 1 (API error)
  instead of a traceback.
