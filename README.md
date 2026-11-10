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

[SureChEMBL](https://www.surechembl.org/) is EMBL-EBI's database of chemistry extracted automatically from the full text, images and MOL attachments of patents: 31 million compounds across 45 million patent documents (EP, WO and US full text, JP bibliographic data and English abstracts, CN in English translation), updated as patents publish. This package is a Python client for it: the live REST API, InChIKey and cross-reference resolution through UniChem (the patents for a compound you hold by its ChEMBL id, PubChem CID or InChIKey), full patent documents, the bulk parquet releases read in place from EBI's server with DuckDB, and joins into scigantic-chembl, scigantic-bindingdb and scigantic-pubchem. No mirror, no download, no API key.

```python
import scigantic_surechembl as sc

aspirin = sc.compound("SCHEMBL1353")
print(aspirin.smiles, aspirin.inchi_key, aspirin.mol_formula)

sc.count_patents_for_compound(aspirin.id)                 # 699345
for hit in sc.patents_for_compound(aspirin.id, max_results=5):
    print(hit.doc_id, hit.title)

doc = sc.patent("US10000000B2")
print(doc.title, doc.published, doc.assignees, doc.family_id)
print(doc.claims[:200])
```

## Installation

```console
$ pip install scigantic-surechembl            # REST client
$ pip install "scigantic-surechembl[bulk]"     # plus DuckDB + pandas for the bulk parquet
$ pip install "scigantic-surechembl[names]"    # plus scigantic-pubchem, for by_name()
```

## The late-2026 API change (0.4.0)

SureChEMBL replaced its REST API in late 2026. The Java service this package was built against (and every endpoint the 0.3 releases called) now answers 404 or 405; the new one is a FastAPI service with its spec at `https://www.surechembl.org/api/openapi.json`. Release 0.4.0 is a port to it, made from responses observed live on 2026-11-09 rather than from the spec alone. The public functions keep their names and signatures, and what changed underneath is:

| Function | 0.3 (old API) | 0.4 (new API) |
|---|---|---|
| `compound()`, `compounds()` | `GET /chemical/id/{id}`, `POST /chemical/id` | `GET /chemical/id?ids=...` (bare integers; `SCHEMBL1353` is a 404) |
| `by_smiles()` | `POST /chemical/smiles/` | an "identical" structure search, then the first hit by id |
| `by_name()` | `GET /chemical/name/{name}` | **removed upstream**; now resolved through PubChem (needs the `names` extra), see below |
| `structure_image()` | `GET /service/chemical/image` | `POST /chemical/image`; `highlight=` raises, the new endpoint has no highlight option |
| `structure_search()` and friends | async job, poll a hash, page the results | synchronous `POST /search/structure`, ids only, records fetched by id; new `threshold=` argument |
| `patents_for_compound()`, `count_patents_for_compound()` | `POST /search/documents_for_structures` | `GET`, ids as a repeated `chemicalIds` parameter |
| `search_patents()`, `count_patents()` | `POST /search/content` | `GET /search/content?grouped=false`; restricted by the server to US, EP, WO, JP and CN |
| `patent()` | `GET /document/{id}/contents` | `GET /patent/get_patent/{ucid}` plus one search-index request for family id and title |
| `patent_chemistry()` | `POST /export/document-chemistry` (text, images and MOL attachments) | `GET /patent/leadmine/{ucid}`: compounds named in the text only |
| `family_id()`, `family_members()` | `/document/{id}/family/id`, `/family/members` | **removed upstream**; answered from the search index's `fam` field, five offices only |
| `patent_pdf()` | (new) | `GET /patent/pdf/{ucid}` |

What the new API no longer gives, and how 0.4.0 reports it rather than inventing it: `Patent.cpc`, `Patent.citations` and `Patent.legal_events` are empty lists (the record's classification and citation arrays were empty on nearly every document probed, and legal status is gone); `Patent.has_pdf` is `None`, meaning unknown; `family_members()` lists only US, EP, WO, JP and CN members, because the search index the family now comes from is filtered to those offices by the server; and `patent_chemistry()` returns fewer compounds than before (the valsartan patent, US-5399578-A: 454 from text against 986 from the old export). Cache keys carry a version, bumped in 0.4.0, so nothing cached by 0.3.x is ever read back.

## Why this exists

SureChEMBL was relaunched in 2024 with a new REST API, in 2025 started publishing its whole database as parquet every two weeks, and in late 2026 replaced the REST API again. Checked on 2026-09-08, nothing on PyPI wraps either:

| What exists | What it is | What it does not do |
|---|---|---|
| [`chembl/surechembl-data-client`](https://github.com/chembl/surechembl-data-client) (official, MIT) | Python 2.7 scripts that load the pre-2024 TSV/MAP feeds into Oracle or PostgreSQL via SQLAlchemy | Not on PyPI. Targets the retired data feeds ("SureChEMBL data client no longer gets data update", per EBI's own docs). No REST API, no parquet. |
| [`Augmented-Nature/SureChEMBL-MCP-Server`](https://github.com/Augmented-Nature/SureChEMBL-MCP-Server) | A JavaScript MCP server over the REST API | Not Python, not a library. |
| The REST API itself | Swagger at `surechembl.org/api/docs`, most responses typed as an untyped `{}` | See the next paragraph. |

The API's behaviour that a caller has to discover by probing, all handled here (as observed on the late-2026 API): it rejects the `SCHEMBL1353` form of its own ids (a 404 on `/chemical/id`, a Solr error on the document search) and wants the bare integer. A miss is a 404 `{"error": ...}` on one endpoint, a 404 `{"detail": ...}` on another, and an empty list on a third. A Solr syntax error in a patent search comes back as HTTP 503, which looks like an outage and must not be retried like one. Structure search returns bare ids (or `[id, score]` pairs), so the records are a second round of requests, and caps every search at 10,000 hits. Several compound ids to the document search must travel as a repeated parameter; the comma-joined form is a server error. There is no InChIKey lookup and no name lookup at all; InChIKeys go through UniChem, as SureChEMBL's FAQ says, and names through PubChem. Full patent text comes back with every bibliographic section as a string of office XML, with each inventor listed up to three times in three spellings. The full-text search silently ANDs every query with a five-country filter (it echoes the rewritten query back). And the API sends no rate-limit headers, so being a polite client is entirely the caller's job: requests here are paced through a token bucket and cached, and transient errors retried.

The bulk half is the other reason. EBI's server honours HTTP range requests, and the parquet files are sorted by id with per-row-group statistics, so DuckDB can answer an id lookup against a 3.9 GB file by reading a few MB, with nothing downloaded. Which queries that makes cheap, and which it does not, was measured from the files' own metadata and is documented below rather than left for you to find out on a 4.7 GB scan.

## Compounds

```python
sc.compound(1353)                      # by id; "SCHEMBL1353" and "1353" work too
sc.compounds([1353, 2871, "SCHEMBL7580"])   # batched, dict keyed by int id; misses absent
sc.by_name("aspirin")                  # list, via PubChem then UniChem (the `names` extra)
sc.by_smiles("C/C=C/C(=O)O")           # the first "identical" structure-search hit, or None
sc.by_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")   # list, via UniChem; see "Duplicate ids"
sc.structure_image("CC(=O)Oc1ccccc1C(=O)O", 300, 300)   # PNG bytes from SureChEMBL's depictor
```

A `Compound` carries `id`, `schembl_id`, `name`, `smiles`, `inchi`, `inchi_key`, `mol_weight`, and, on every live lookup (all of them go through `/chemical/id` now), the full property set SureChEMBL computes: `mol_formula`, `log_p`, `hbd`, `hba`, `psa`, `rtb`, `heavy_atoms`, `aromatic_rings`, `qed_weighted`, `num_ro5_violations`, `ro3_pass`, `organic`, `is_element`, `struct_alert`. The bulk table returns structure and weight only. `global_frequency` is passed through from the API but is not a document count (aspirin reports 22 and is in 699,345 documents); use `count_patents_for_compound()`.

`by_name()` changed meaning in 0.4.0. SureChEMBL's name endpoint searched the names it had extracted from patents, and it is gone with no replacement. `by_name()` now asks PubChem (through [scigantic-pubchem](https://github.com/Scigantic/scigantic-pubchem), installed by the `names` extra) for its best match to the name, takes that structure's standard InChIKey, and returns every SureChEMBL id UniChem holds for it. So the answer is the structure PubChem means by the name, and a name only patents use, which PubChem does not know, returns `[]`. Without scigantic-pubchem installed it raises `SureChEMBLError` saying so, rather than returning nothing.

`by_inchikey()` goes through UniChem, which on the day this was built hung on roughly one request in six (a request that answers does so in under a second; one that hangs never answers, or answers as a 500 after 32 seconds). The lookup is therefore hedged: UniChem's legacy endpoint is asked first, and if it has not answered within 1.5 seconds the v1 endpoint is asked too, and the first good answer wins. Measured over 60 keys: median 0.3 s, 90th percentile 4.7 s, worst 12.6 s, every key resolved; without hedging the same run had a 90th percentile of 16.6 s and a worst of 33 s. A malformed key is rejected before any request.

### Ids the REST API has and the bulk table does not

The bulk `compounds` table holds compounds that were extracted from at least one patent. The REST API also serves compounds with no patent occurrence at all: in a sample of 300 random ids, REST resolved 276 and the bulk table 222, and every one of the 54 REST-only ids had zero documents and `global_frequency` 0, all in the upper id range (above 31.7M). Where both have a compound, InChI, InChIKey, SMILES text and weight agreed on all 222. In the other direction, the bulk `patents` table listed some publications the old REST document endpoint did not serve (6 of 120 sampled, 404, measured on the 0.3 API). When a record carries two English titles (an office title and a vendor's descriptive one, in either order), `patent().title` is the office title, taken from the search index, and `patent().titles` has all of them.

### Duplicate ids

SureChEMBL holds some structures under more than one id. Aspirin is both `SCHEMBL1353` and `SCHEMBL29350479`, with the same InChIKey, in the REST API, in UniChem, and in the bulk `compounds` table. That is why `by_inchikey()` returns a list, why an "identical" structure search for aspirin returns two hits, and why a compound-to-patent count should be taken over every id for the structure, not the first one found.

## Structure search

```python
sc.similar_compounds("CC(=O)Oc1ccccc1C(=O)O", max_results=50)     # hits carry .similarity, best first
sc.similar_compounds("CC(=O)Oc1ccccc1C(=O)O", threshold=0.95)     # server default 0.8
sc.substructure_search("c1ccc2ncccc2c1", max_results=500)          # at least 5 atoms, see below
sc.structure_search("CC(=O)Oc1ccccc1C(=O)O", mode="identical")     # all features must match
sc.structure_search("CC(=O)Oc1ccccc1C(=O)O", mode="connectivity")  # same skeleton, any stereo/isotopes
```

The four modes are the ones SureChEMBL's own interface offers, under its names. Since the late-2026 API change a search is synchronous: one request returns a page of ids (or `[id, similarity]` pairs) and the full hit count, and the server caches the whole result so later pages come back at once. The records are then fetched through `/chemical/id` in batches of 500, so a search costs its pages (1,000 ids each) plus one request per 500 hits. The server still caps every structure search at 10,000 hits, similarity hits now arrive best first, and an unparseable SMILES is a 422, raised as `ValueError`. A finished search is also cached locally under its query. `substructure_search()` refuses a query with fewer than 5 atoms with a `ValueError` before sending it: twice on 2026-09-08, on the old API, a trivially broad substructure query (a single carbon, a bare cyclohexane) took the substructure service down for everyone for about an hour. The new service may cope better, but a whole-corpus sweep belongs on the bulk parquet with RDKit, not on a shared service, and the guard costs nothing.

## Patents

```python
sc.patents_for_compound(1353, max_results=100)      # PatentHit list: doc_id, title, publication_date, assignee
sc.patents_for_compound([1353, 5671])              # documents containing ALL the ids: aspirin AND caffeine
sc.count_patents_for_compound(1353)                # 699345, one request

sc.search_patents('ttl:kinase AND asg:novartis AND pdyear:2024', max_results=200)
sc.search_patents('clm:"sodium channel" AND cpc:C07D')
sc.count_patents('ab:aspirin AND nanoparticle')
```

Several ids to `patents_for_compound()` mean an intersection, not a union: aspirin is in 699,345 documents, SCHEMBL15082 in 86, and the pair in 58 (measured 2026-11-09). That makes it the co-occurrence query; for a union, query the ids separately and merge on `doc_id`. Because of the duplicate ids described above, "every patent mentioning aspirin" is the union over its ids, not one call. At most 500 ids per call.

`search_patents()` passes SureChEMBL's Solr syntax through, and the server then ANDs it with `pnctry:(US OR EP OR WO OR JP OR CN)`, so publications from other offices are never hits. Plain terms search all text; prefixes restrict a term to a field: `pn` publication number, `pd`/`pdyear` publication date, `ttl` title, `ab` abstract, `clm` claims, `desc` description, `asg` assignee, `apl` applicant, `inv` inventor, `ic` IPCR, `cpc` CPC, `fam` family id, `pri` priority, `pcit` cited patents, and language variants such as `ttl_en`/`clm_de`. The full list is in SureChEMBL's documentation under "Solr query field names and examples". Quote a publication number: `pn:"US-10000000-B2"` (or `ucid:"US-10000000-B2"`) matches one document. An empty query is refused locally with `ValueError` (the server would return all 125 million documents), and a Solr syntax error comes back with Solr's own message, raised as `SureChEMBLError` at once rather than retried. Hits carry `title`, `publication_date`, `assignees` (`assignee` is the first) and, from the full-text search only, `family_id`. Pages are fetched at a fixed size (capped at 500 for `patents_for_compound()` and 1,000 for `search_patents()`) and de-duplicated.

```python
doc = sc.patent("US-10000000-B2")      # or "US10000000B2"; None if unknown
doc.title, doc.published, doc.abstract, doc.claims, doc.description
doc.applicants, doc.inventors, doc.assignees      # de-duplicated across the record's three spellings
doc.family_id, doc.application_number, doc.priority_numbers
doc.ipcr                                          # IPC symbols, when the record has a classification block
doc.pdf_url                                       # https://www.surechembl.org/api/patent/pdf/US-10000000-B2
doc.raw                                           # the whole record, for everything not lifted out

sc.patent_pdf("US-8592440-B2")            # the PDF bytes, or None
sc.patent_chemistry("US-8592440-B2")      # the compounds named in the document's text
sc.family_id("US-10000000-B2")            # 55456961, the DOCDB simple family
sc.family_members("US10000000B2")         # ['JP-7098706-B2', 'EP-3268771-B1', ..., 'WO-2016144528-A1', ...]
```

`doc.cpc`, `doc.citations` and `doc.legal_events` are still fields, and are empty lists on the new API (see "The late-2026 API change"). `patent_chemistry()` runs SureChEMBL's LeadMine text-mining over the document and returns, in order of first mention, every compound it resolves to a SureChEMBL id; structures that appear only in images or MOL attachments are missing, so for a patent's complete extracted chemistry use the bulk `patent_compound_map` (see "Bulk data"). The analysis runs live and takes several seconds for a long document. `family_id()` and `family_members()` come from the full-text search index, which covers US, EP, WO, JP and CN only, so a family's members from other offices are not listed and a DE publication has no family id.

Publication numbers are normalized to SureChEMBL's `CC-NUMBER-KIND` form, so `US10000000B2`, `US 10000000 B2` and `WO2016/144528A1` all work, as do the letter-bearing numbers the corpus actually contains: Japanese era numbers (`JP-S60174822-A`, `JP-H08511828-A`), Japanese national-phase PCT filings (`JP-WO2018116905-A1`), and US reissues, plant patents, designs and statutory invention registrations (`US-RE43229-E1`, `US-PP22546-P3`, `US-D651743-S1`, `US-H2267-H1`). The patent endpoint is keyed by the full number including kind code; `US10000000` alone will not resolve.

## Cross-references

```python
x = sc.xrefs("SCHEMBL1353")
x.chembl, x.pubchem_cid, x.drugbank, x.chebi, x.pdb_ligand, x.bindingdb, x.unii
# ['CHEMBL25'], [2244], ['DB00945'], ['CHEBI:15365'], ['AIN'], ['22360'], ['R16CO5Y76E']
x.surechembl        # [1353, 29350479], every SureChEMBL id for the structure
x.sources           # every UniChem source, keyed by source id

sc.xrefs_for("chembl", "CHEMBL25")        # start from a ChEMBL id, PubChem CID, DrugBank id, ...
sc.xrefs_for_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
sc.surechembl_ids_for("pubchem", 2244)    # [1353, 29350479]

sc.patents_for_chembl("CHEMBL25", max_results=500)      # the patent landscape of a ChEMBL compound
sc.patents_for_pubchem_cid(2519)                        # ... of a PubChem compound
sc.patents_for_inchikey("RYYVLZVUVIJVGH-UHFFFAOYSA-N")  # ... of a structure
```

All through [UniChem](https://www.ebi.ac.uk/unichem/), EMBL-EBI's identifier bridge, with the same hedged requests as `by_inchikey()`. The three `patents_for_*()` functions are the join that motivated this: SureChEMBL is keyed by its own ids and a structure can sit under more than one of them, so they take the union of `patents_for_compound()` over every SureChEMBL id UniChem maps the identifier to, merged on `doc_id`.

## Bridges into scigantic-chembl, scigantic-bindingdb and scigantic-pubchem

```console
$ pip install "scigantic-surechembl[bridge]"
```

```python
from scigantic_surechembl import bridge

bridge.chembl_compound(1353)
# {'chembl_id': 'CHEMBL25', 'pref_name': 'ASPIRIN', 'max_phase': 4.0, 'molregno': 1280, ...}
bridge.chembl_activities(1353)                 # 4,087 rows: type, value, units, pchembl, target, assay
bridge.chembl_matches_for_patent("US-5399578-A")
# one row per compound patent_chemistry() finds in the text, with ChEMBL's id, phase and activity count

bridge.bindingdb_measurements(1353)            # 137 affinity rows by structure
bridge.bindingdb_measurements_for_patent("US-11566007-B2")
# 7,524 Ki/IC50/Kd rows BindingDB curated from this KRAS patent's own SAR tables: 619 ligands, 12 targets
bridge.bindingdb_overlap_for_patent("US-11566007-B2")   # the same, with SureChEMBL's compounds joined on

bridge.pubchem_compound(1353)                  # scigantic_pubchem.Compound(cid=2244, title='Aspirin', ...)
```

The ChEMBL and BindingDB joins are on structure, not on UniChem: `compound_structures.standard_inchi_key` in [scigantic-chembl](https://github.com/Scigantic/scigantic-chembl)'s mirror and `measurements.ligand_inchi_key` in [scigantic-bindingdb](https://github.com/Scigantic/scigantic-bindingdb)'s are joined directly against SureChEMBL's InChIKeys, so a patent's whole compound list is one DuckDB query rather than a round trip per compound (4,068 compounds in 1.9 s). `chembl_matches_for_patent()` answers "how much of this patent's chemistry is known bioactive matter", approved and well-measured compounds first.

BindingDB also curates affinities directly from patents: 1.34 million of its measurements, from about 8,900 US patents, carry a `patent_number`. `bindingdb_measurements_for_patent()` reads those for a SureChEMBL document, which gives the measured SAR for the structures SureChEMBL extracted from the same text. The two do not always agree on the full InChIKey, measured on four such patents: for one, 638 of BindingDB's 774 ligands match a SureChEMBL structure exactly; for the KRAS patent above, 0 of 619 do, because BindingDB drew the ligands without stereochemistry while SureChEMBL kept the stereo the text specified, yet 562 share the InChIKey's connectivity block; for a third, SureChEMBL extracted no structures at all. `bindingdb_overlap_for_patent()` therefore reports both a full-key match (`surechembl_id`) and a skeleton match (`surechembl_skeleton_ids`), which is the level at which the two databases actually agree. Those figures were measured with the 0.3 releases, when `patent_chemistry()` read SureChEMBL's full extraction. Since 0.4.0 it sees the text only, and SAR tables are mostly images, so the overlap is much smaller (re-measured 2026-11-09: 121 of 774 full-key matches for the first patent, 6 of 619 skeleton matches for the KRAS one); for the full overlap, join against the bulk `patent_compound_map`.

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

Bulk patent ids are not the same as publication numbers: `patent_compound_map` joins on the integer `patents.id`, which the REST API never exposes, and `patents.patent_number` carries no statistics, so a lookup by number would scan the column every time. `build_patent_number_index()` reads the two columns once from EBI (about 660 MB), sorts by publication number and writes a local zstd parquet in small row groups; after that `patent_id_for_number()`, `patent_record_for_number()` and `patent_compounds_for_number()` prune to one row group. Measured on the 45M-row release: 110 to 220 s to build depending on the link, 236 MB on disk, 10 ms per lookup. The file name carries the release date; rebuild when you move releases.

```python
bulk.build_patent_number_index()                 # once per release, into the package cache dir
bulk.patent_id_for_number("US-10000000-B2")      # 19017503
bulk.patent_compounds_for_number("EP-2426128-A1")   # 4,264 (compound, field) rows
```

## Fingerprints: local similarity over all 41M compounds

```console
$ pip install "scigantic-surechembl[fingerprints]"     # FPSim2, RDKit, PyTables
```

```python
from scigantic_surechembl import fingerprints

index = fingerprints.FingerprintIndex()        # downloads EBI's 1.36 GB file once, loads it in 2 s
index.similar("CC(=O)Oc1ccccc1C(=O)O", threshold=0.7, n_workers=4)   # 232 rows (compound_id, similarity) in 0.5 s
index.top_k("CC(=O)Oc1ccccc1C(=O)O", k=10)
index.substructure_candidates("c1ccc2ncccc2c1")                     # 4,346 ids in 20 ms; a screen, see below
```

EBI ships an [FPSim2](https://github.com/chembl/FPSim2) fingerprint file with each bulk release, keyed by SureChEMBL compound id: 41.2M compounds (the REST API's full set, more than the 31M with patent occurrences in the bulk tables), Morgan radius 2 at 256 bits, built with RDKit 2021.09. Loaded in memory (about 1.7 GB of RSS) it answers a similarity query in 20 to 500 ms over the whole set with no result cap and no dependence on the server-side search worker. Its counts match the server exactly (232 aspirin hits at 0.7 either way), so the server searches the same fingerprints. 256 bits is coarse: distinct structures tie at 1.0 (aspirin's dimer and its 13C isotopologue both score 1.0 against aspirin), so treat the result as a neighbourhood, not a ranking.

Substructure is different. With Morgan fingerprints a bit-containment screen is incomplete: quinoline gave 4,346 candidates, of which RDKit confirmed 89% of a sample, while the server's substructure search returned over 10,000 (its cap). `substructure_candidates()` is named for what it is, a fast partial screen (benzene: 11.2M candidates in 0.35 s); use `substructure_search()` when recall matters, and confirm candidates with RDKit on their SMILES.

## Caching and rate limiting

On by default, expiring after 30 days, at `~/.cache/scigantic-surechembl` (macOS: `~/Library/Caches/`; override with `enable_cache(cache_dir=...)` or `SCIGANTIC_SURECHEMBL_CACHE`). The cache can never make a lookup fail: a directory that cannot be created or written (a read-only filesystem, a path that turns out to be a file) turns caching off for the process with one warning and the lookup proceeds live, and a corrupt, truncated or foreign entry is a miss. `enable_cache(cache_dir=...)` is the one call that raises on an unusable directory, since a caller naming one wants to know. Same reasoning as `scigantic-pubchem` and the reverse of `scigantic-chembl`/`scigantic-bindingdb`: those read an S3 mirror with no meaningful rate limit, this calls a shared EMBL-EBI service for every lookup. Every request also passes through a token bucket paced at 5 requests/second, and 429/502/503/504 responses are retried with backoff, except a 503 whose body is a Solr error, which is deterministic and raised at once. A 500 is not retried. Cache keys include a format version (bumped in 0.4.0), so entries left by an older release are never returned.

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
$ scigantic-surechembl xrefs chembl:CHEMBL25
$ scigantic-surechembl patents-for-chembl CHEMBL25 --max-results 50
$ scigantic-surechembl chembl-patent US-5399578-A
$ scigantic-surechembl bindingdb-patent US-11566007-B2
$ scigantic-surechembl text 'ttl:aspirin AND pdyear:2024' --max-results 10
$ scigantic-surechembl patent US10000000B2 --full-text
$ scigantic-surechembl chemistry US-8592440-B2
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

Before the 0.3 releases the package was run through two stress batteries, against the old API; the figures in this paragraph are from then. The second, against the published wheel, added a cross-layer consistency check (300 random compounds and 120 patents compared between the bulk parquet and the REST API, and the extracted chemistry of 6 patents compared between the bulk map and the REST export: identical on every shared record), Solr offsets past 10,000, 60-page compound-to-patent walks, cache directories that are read-only, a file, or unwritable, and sustained mixed load from 6 threads (2,054 operations, zero errors, traced memory flat at 2 MB, one connection pool, no thread growth). The first: 60 patent documents sampled across every office and kind code in the bulk table plus the largest documents findable by sequence-listing search; 10,000-id batch lookups; 3,000-deep and 2,000-deep patent pagination; 20 hostile Solr queries and 7 hostile structures; 16 threads against the rate limiter, 32 threads racing one cache key, and 8 threads on the bulk helpers; id boundaries at both ends of every table; every bulk release's schema; download resume from a truncated file; an unreachable host and a bad DNS name. Each finding above that names a number came from that run, and each bug it found has a regression test.

## Related packages

Part of a family of open-source Scigantic clients for public scientific archives: [scigantic-chembl](https://github.com/Scigantic/scigantic-chembl) (ChEMBL bioactivity from an S3 mirror), [scigantic-bindingdb](https://github.com/Scigantic/scigantic-bindingdb), [scigantic-pubchem](https://github.com/Scigantic/scigantic-pubchem) (live PUG REST), [scigantic-comptox](https://github.com/Scigantic/scigantic-comptox) (EPA ToxCast), [scigantic-wwpdb](https://github.com/Scigantic/scigantic-wwpdb), and more at [github.com/Scigantic](https://github.com/Scigantic). The `bridge` module above joins the first three directly.
