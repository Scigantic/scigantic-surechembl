# Changelog

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
