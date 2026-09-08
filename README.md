<h1 align="center">scigantic-surechembl</h1>

<p align="center">
    <a href="https://github.com/Scigantic/scigantic-surechembl/actions/workflows/ci.yml">
        <img alt="CI" src="https://github.com/Scigantic/scigantic-surechembl/actions/workflows/ci.yml/badge.svg" /></a>
    <a href="https://pypi.org/project/scigantic-surechembl/">
        <img alt="PyPI" src="https://img.shields.io/pypi/v/scigantic-surechembl" /></a>
    <a href="https://pypi.org/project/scigantic-surechembl/">
        <img alt="PyPI - Python Version" src="https://img.shields.io/pypi/pyversions/scigantic-surechembl" /></a>
    <a href="https://github.com/Scigantic/scigantic-surechembl/blob/main/LICENSE">
        <img alt="License" src="https://img.shields.io/github/license/Scigantic/scigantic-surechembl" /></a>
</p>

[SureChEMBL](https://www.surechembl.org/) is EMBL-EBI's database of chemistry extracted automatically from the full text, images and MOL attachments of patents: 31 million compounds across 45 million patent documents (EP, WO and US full text, JP bibliographic data and English abstracts, CN in English translation), updated as patents publish. This package is a Python client for it: the live REST API, InChIKey resolution through UniChem, full patent documents, and the bulk parquet releases read in place from EBI's server with DuckDB. No mirror, no download, no API key.

```python
import scigantic_surechembl as sc

aspirin = sc.compound("SCHEMBL1353")
print(aspirin.smiles, aspirin.inchi_key, aspirin.mol_formula)

sc.count_patents_for_compound(aspirin.id)                 # 694428
for hit in sc.patents_for_compound(aspirin.id, max_results=5):
    print(hit.doc_id, hit.title)

doc = sc.patent("US10000000B2")
print(doc.title, doc.published, doc.assignees, doc.cpc[:3])
print(doc.claims[:200])
```

## Installation

```console
$ pip install scigantic-surechembl            # REST client
$ pip install "scigantic-surechembl[bulk]"     # plus DuckDB + pandas for the bulk parquet
```

## Why this exists

SureChEMBL was relaunched in 2024 with a new REST API and, in 2025, started publishing its whole database as parquet every two weeks. Checked on 2026-09-08, nothing on PyPI wraps either:

| What exists | What it is | What it does not do |
|---|---|---|
| [`chembl/surechembl-data-client`](https://github.com/chembl/surechembl-data-client) (official, MIT) | Python 2.7 scripts that load the pre-2024 TSV/MAP feeds into Oracle or PostgreSQL via SQLAlchemy | Not on PyPI. Targets the retired data feeds ("SureChEMBL data client no longer gets data update", per EBI's own docs). No REST API, no parquet. |
| [`Augmented-Nature/SureChEMBL-MCP-Server`](https://github.com/Augmented-Nature/SureChEMBL-MCP-Server) | A JavaScript MCP server over the REST API | Not Python, not a library. |
| The REST API itself | Swagger at `surechembl.org/api/swagger-ui.html`, every response typed as an opaque `{status, data}` envelope | See the next paragraph. |

The API's behaviour that a caller has to discover by probing, all handled here: it rejects the `SCHEMBL1353` form of its own ids (500, "For input string"), and wants the bare integer. A miss is a 200 with an empty list on one endpoint, a 400 on another, a 404 on a third and a 500 ("SQL exception") on a fourth. Structure search needs its JSON wrapped in the Java class name (`{"StructureSearchRequest": {...}}`), runs as an asynchronous job to poll, ignores its own `maxResults` field, and caps every search at 10,000 hits. A SMILES with stereo bonds cannot go in the URL path (the slashes 400) and must be posted as a form field. There is no InChIKey lookup at all; the FAQ says to go through UniChem, so `by_inchikey()` does. Full patent text comes back as a deeply nested XML-to-JSON record with each inventor listed up to three times in three spellings. And the API sends no rate-limit headers, so being a polite client is entirely the caller's job: requests here are paced through a token bucket and cached, and transient errors retried.

The bulk half is the other reason. EBI's server honours HTTP range requests, and the parquet files are sorted by id with per-row-group statistics, so DuckDB can answer an id lookup against a 3.9 GB file by reading a few MB, with nothing downloaded. Which queries that makes cheap, and which it does not, was measured from the files' own metadata and is documented below rather than left for you to find out on a 4.7 GB scan.

## Compounds

```python
sc.compound(1353)                      # by id; "SCHEMBL1353" and "1353" work too
sc.compounds([1353, 2871, "SCHEMBL7580"])   # batched, dict keyed by int id; misses absent
sc.by_name("aspirin")                  # list, several ids can share a name
sc.by_smiles("C/C=C/C(=O)O")           # the one compound with that canonical structure, or None
sc.by_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")   # list, via UniChem; see "Duplicate ids"
sc.structure_image("CC(=O)Oc1ccccc1C(=O)O", 300, 300)   # PNG bytes from SureChEMBL's depictor
```

A `Compound` carries `id`, `schembl_id`, `name`, `smiles`, `inchi`, `inchi_key`, `mol_weight`, and on the id/SMILES/search endpoints the full property set SureChEMBL computes: `mol_formula`, `log_p`, `hbd`, `hba`, `psa`, `rtb`, `heavy_atoms`, `aromatic_rings`, `qed_weighted`, `num_ro5_violations`, `ro3_pass`, `organic`, `is_element`, `struct_alert`. The name endpoint and the bulk table return structure and weight only. `global_frequency` is passed through from the API but is not a document count (aspirin reports 22 and is in 694,428 documents); use `count_patents_for_compound()`.

`by_inchikey()` goes through UniChem, which on the day this was built hung on roughly one request in six (a request that answers does so in under a second; one that hangs never answers, or answers as a 500 after 32 seconds). The lookup is therefore hedged: UniChem's legacy endpoint is asked first, and if it has not answered within 1.5 seconds the v1 endpoint is asked too, and the first good answer wins. Measured over 60 keys: median 0.3 s, 90th percentile 4.7 s, worst 12.6 s, every key resolved; without hedging the same run had a 90th percentile of 16.6 s and a worst of 33 s. A malformed key is rejected before any request. Two limits of the name endpoint are surfaced as `ValueError` rather than silent misses: an empty name, and a name containing `/` (the API only takes the name in the URL path, and rejects an encoded slash).

### Duplicate ids

SureChEMBL holds some structures under more than one id. Aspirin is both `SCHEMBL1353` and `SCHEMBL29350479`, with the same InChIKey, in the REST API, in UniChem, and in the bulk `compounds` table. That is why `by_inchikey()` returns a list, why an "identical" structure search for aspirin returns two hits, and why a compound-to-patent count should be taken over every id for the structure, not the first one found.

## Structure search

```python
sc.similar_compounds("CC(=O)Oc1ccccc1C(=O)O", max_results=50)     # hits carry .similarity
sc.substructure_search("c1ccc2ncccc2c1", max_results=500)          # SMILES or SMARTS
sc.structure_search(smiles, mode="identical")                       # all features must match
sc.structure_search(smiles, mode="connectivity")                    # same skeleton, any stereo/isotopes
```

The four modes are the ones SureChEMBL's own interface offers, under its names. Each search is an asynchronous job on the server: submitted, polled (0.5 s doubling to a 5 s cap), then paged. The server caps every structure search at 10,000 hits regardless of what is asked for, and similarity hits do not come back strictly sorted by score (verified: 1.0, 1.0, 0.96, 1.0, ...), so sort on `.similarity` yourself. The server's pages also run short of its own count and a page past the last repeats the last one (a 232-hit search paged at 100 gave 98, 99, 31, then the same 31 again), so results are de-duplicated and paging stops at the reported page count. A finished search is cached under its query, so re-running one is free. A search the server reports as failed is resubmitted once, then raised; on 2026-09-08 the substructure worker returned "internal error" for every query for about an hour while the other three modes kept working, so that path is real.

## Patents

```python
sc.patents_for_compound(1353, max_results=100)      # PatentHit list: doc_id, title, publication_date, assignee
sc.patents_for_compound([1353, 29350479])          # documents containing ANY of the ids
sc.count_patents_for_compound(1353)                # 694428, one request

sc.search_patents('ttl:kinase AND asg:novartis AND pdyear:2024', max_results=200)
sc.search_patents('clm:"sodium channel" AND cpc:C07D')
sc.count_patents('ab:aspirin AND nanoparticle')
```

`search_patents()` passes SureChEMBL's Solr syntax through untouched. Plain terms search all text; prefixes restrict a term to a field: `pn` publication number, `pd`/`pdyear` publication date, `ttl` title, `ab` abstract, `clm` claims, `desc` description, `asg` assignee, `apl` applicant, `inv` inventor, `ic` IPCR, `cpc` CPC, `fam` family id, `pri` priority, `pcit` cited patents, and language variants such as `ttl_en`/`clm_de`. The full list is in SureChEMBL's documentation under "Solr query field names and examples". Quote a publication number: `pn:"US-10000000-B2"` matches one document, while unquoted `pn:US-10000000-B2` has its hyphens tokenized and matches 55 million. An empty or wildcard-only query is refused by the server ("Query is too general") and a Solr syntax error comes back with Solr's own message, both raised as `SureChEMBLError`. Pages are fetched at a fixed size (capped at 250 for `patents_for_compound()`, where 500 overflows a Solr URI on the server, and 1,000 for `search_patents()`) and de-duplicated; 3,000 patents for aspirin took 34 s in 12 requests.

```python
doc = sc.patent("US-10000000-B2")      # or "US10000000B2"; None if unknown
doc.title, doc.published, doc.abstract, doc.claims, doc.description
doc.applicants, doc.inventors, doc.assignees      # de-duplicated across the record's three spellings
doc.cpc, doc.ipcr                                 # bare symbols: ['G01S17/894', ...]
doc.family_id, doc.application_number, doc.priority_numbers, doc.citations
doc.legal_events                                  # [LegalEvent(code='MAFP', date=..., title='MAINTENANCE FEE PAYMENT'), ...]
doc.pdf_url                                       # when SureChEMBL has the PDF
doc.raw                                           # the whole record, for everything not lifted out

sc.patent_chemistry("WO-2016144528-A1")   # every Compound SureChEMBL extracted from the document
sc.family_id("US-10000000-B2")            # 55456961, the DOCDB simple family
sc.family_members("US10000000B2")         # ['US-10845468-B2', 'EP-3268771-B1', 'WO-2016144528-A1', 'JP-6817387-B2', ...]
```

Publication numbers are normalized to SureChEMBL's `CC-NUMBER-KIND` form, so `US10000000B2`, `US 10000000 B2` and `WO2016/144528A1` all work, as do the letter-bearing numbers the corpus actually contains: Japanese era numbers (`JP-S60174822-A`, `JP-H08511828-A`), Japanese national-phase PCT filings (`JP-WO2018116905-A1`), and US reissues, plant patents, designs and statutory invention registrations (`US-RE43229-E1`, `US-PP22546-P3`, `US-D651743-S1`, `US-H2267-H1`). The document endpoint is keyed by the full number including kind code; `US10000000` alone will not resolve. Parsing was checked against 60 documents sampled across all five offices and every kind code in the bulk table, plus documents with descriptions of up to 0.7 MB.

## Bulk data

```python
from scigantic_surechembl import bulk

bulk.latest_release()                    # '2026-09-08'; a new one every two weeks
bulk.compound_record("SCHEMBL1353")      # structure row from compounds.parquet, one row group read
bulk.patent_record(10)                   # PatentRecord: number, country, date, family, title, assignee, CPC/IPC lists
bulk.patent_compounds(10)                # DataFrame of (compound_id, field): where in the patent each compound was found

bulk.sql("""
    SELECT p.patent_number, p.publication_date, count(*) AS compounds
    FROM patent_compound_map m JOIN patents p ON p.id = m.patent_id
    WHERE m.patent_id BETWEEN 1000 AND 1100 AND p.id BETWEEN 1000 AND 1100
    GROUP BY 1, 2 ORDER BY 3 DESC
""")

bulk.download("compounds", "compounds.parquet")   # resumable; for the workloads below that need a local copy
```

Since 2025 SureChEMBL publishes its whole database every two weeks as parquet under `ftp.ebi.ac.uk/pub/databases/chembl/SureChEMBL/bulk_data/<date>/`. The 2026-09-08 release:

| table | rows | size | sorted by |
|---|---|---|---|
| `compounds` | 31,047,780 | 3.9 GB | `id` |
| `patents` | 45,166,378 | 5.5 GB | `id` |
| `patent_compound_map` | 1,542,015,221 | 4.7 GB | `patent_id` |
| `biomedical_entities` | 1,059,724 | 32 MB | `id` |
| `biomedical_locations` | 453,327,904 | 1.6 GB | none usable |
| `fields`, `biomedical_types` | 6, 4 | KB | |

`bulk.sql()` runs DuckDB over views named after the tables, reading only the parquet footers and the row groups a query touches over HTTPS. What that makes cheap was measured from the files' row-group statistics (2026-09-08), not assumed:

- A lookup by `compounds.id` or `patents.id` prunes to one row group (about 15,000 and 52,000 rows respectively): 1 to 7 seconds, single-digit MB read. Measured: `compound_record(1353)` 3.6 s cold, 0.3 s warm; `patent_record(10)` 6.6 s cold.
- "Compounds in patent N" (`patent_compound_map` by `patent_id`) is one row group: 1 to 4 seconds for a 1,005-compound patent.
- The reverse, "patents containing compound N", has no usable statistics (every row group spans the full compound id range) and would scan the 4.3 GB `compound_id` column. Use `patents_for_compound()` for that.
- The string columns (`inchi_key`, `patent_number`) carry no statistics, so a lookup by either is a full column scan (600 MB and 394 MB). Use `by_inchikey()` and `patent()` for those.
- Anything whole-corpus (an exhaustive substructure sweep with RDKit, a reverse-map index, a join of compounds to patents by assignee) wants `download()` once and local DuckDB after.

`bulk.connect(release, tables)` returns a plain DuckDB connection with the views bound, for callers who want to hold a connection across queries. Binding a view reads that file's footer (a few seconds each), so pass only the tables you need. `bulk.release_tables(release)` lists what a release ships; the schema is not fixed across releases, and `sql()`/`connect()` name the missing table rather than surfacing a raw 404. The first release (2025-04-30) has no biomedical tables, stores SMILES as a BLOB column named `rdk_smiles`, and is written in 1,048,576-row groups, so one id lookup there reads about 120 MB (134 s measured); every release from 2025-06-01 on has the current layout. The helper functions are safe to call from several threads at once (each call takes its own cursor on a shared per-release connection; 8 threads doing 24 lookups finished in 11 s against 56 s single-connection).

Bulk patent ids are not the same as publication numbers: `patent_compound_map` joins on the integer `patents.id`, which the REST API never exposes. To go from a publication number to its bulk row you need the scan described above, or a local copy.

## Caching and rate limiting

On by default, expiring after 30 days, at `~/.cache/scigantic-surechembl` (macOS: `~/Library/Caches/`; override with `enable_cache(cache_dir=...)` or `SCIGANTIC_SURECHEMBL_CACHE`). Same reasoning as `scigantic-pubchem` and the reverse of `scigantic-chembl`/`scigantic-bindingdb`: those read an S3 mirror with no meaningful rate limit, this calls a shared EMBL-EBI service for every lookup. Every request also passes through a token bucket paced at 5 requests/second, and 429/502/503/504 responses are retried with backoff. A 500 is not retried: every one seen during development was deterministic (a malformed id, an unknown family).

```python
sc.disable_cache()
sc.enable_cache(ttl_days=7)
sc.enable_cache(ttl_days=None)     # never expire
sc.clear_cache()
```

## Command line

```console
$ scigantic-surechembl compound SCHEMBL1353
$ scigantic-surechembl name aspirin
$ scigantic-surechembl inchikey BSYNRYMUTXBXSQ-UHFFFAOYSA-N
$ scigantic-surechembl search "c1ccc2ncccc2c1" --mode substructure --max-results 50
$ scigantic-surechembl patents-for 1353 --count
$ scigantic-surechembl text 'ttl:aspirin AND pdyear:2024' --max-results 10
$ scigantic-surechembl patent US10000000B2 --full-text
$ scigantic-surechembl chemistry WO-2016144528-A1
$ scigantic-surechembl family US-10000000-B2
$ scigantic-surechembl image "CC(=O)Oc1ccccc1C(=O)O" aspirin.png
$ scigantic-surechembl releases
$ scigantic-surechembl sql "SELECT * FROM fields"
```

Output is JSON, one document per invocation, so it pipes into `jq`.

## Data license and attribution

The code here is MIT-0. The data is SureChEMBL's, and is not covered by that.

SureChEMBL's current data (the REST API and the `bulk_data` parquet releases) is licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), per the `LICENCE` file alongside the bulk releases and SureChEMBL's FAQ. The older quarterly compound dumps under `SureChEMBL/data/` are CC BY-SA 3.0; this package does not read them. SureChEMBL's attribution terms ask that any use include a link to https://www.surechembl.org/, that the SureChEMBL ids be preserved (every model here carries `schembl_id` for that reason), that the release date be shown for data derived from a bulk release (`bulk.latest_release()`), and that publications cite:

> Papadatos G, Davies M, Dedman N, Chambers J, Gaulton A, Siddle J, Koks R, Irvine SA, Pettersson J, Goncharoff N, Hersey A, Overington JP. SureChEMBL: a large-scale, chemically annotated patent document database. *Nucleic Acids Research* 2016;44(D1):D1220-D1228. doi:[10.1093/nar/gkv1253](https://doi.org/10.1093/nar/gkv1253)

The underlying patent text belongs to the issuing offices and their contributors. InChIKey resolution goes through [UniChem](https://www.ebi.ac.uk/unichem/), also EMBL-EBI.

## Testing

Every test that touches data runs live against SureChEMBL, UniChem and EBI's parquet files, with no mocks, the same as the rest of the scigantic-* family: the API's miss and error behaviour is uneven enough that a fixture would only prove the fixture. Cache tests use a private temporary directory. The bulk tests exercise only the row-group-pruned paths, so a run reads a few tens of MB, not gigabytes. `pip install -e ".[dev,bulk]" && pytest -q`.

Before release the package was run through a stress battery: 60 patent documents sampled across every office and kind code in the bulk table plus the largest documents findable by sequence-listing search; 10,000-id batch lookups; 3,000-deep and 2,000-deep patent pagination; 20 hostile Solr queries and 7 hostile structures; 16 threads against the rate limiter, 32 threads racing one cache key, and 8 threads on the bulk helpers; id boundaries at both ends of every table; every bulk release's schema; download resume from a truncated file; an unreachable host and a bad DNS name. Each finding above that names a number came from that run, and each bug it found has a regression test.

## Related packages

Part of a family of open-source Scigantic clients for public scientific archives: [scigantic-chembl](https://github.com/Scigantic/scigantic-chembl) (ChEMBL bioactivity from an S3 mirror), [scigantic-bindingdb](https://github.com/Scigantic/scigantic-bindingdb), [scigantic-pubchem](https://github.com/Scigantic/scigantic-pubchem) (live PUG REST), [scigantic-comptox](https://github.com/Scigantic/scigantic-comptox) (EPA ToxCast), [scigantic-wwpdb](https://github.com/Scigantic/scigantic-wwpdb), and more at [github.com/Scigantic](https://github.com/Scigantic). SureChEMBL compounds map to ChEMBL and PubChem through UniChem; `scigantic-pubchem`'s cross-reference functions and `scigantic-chembl` read those ids directly.
