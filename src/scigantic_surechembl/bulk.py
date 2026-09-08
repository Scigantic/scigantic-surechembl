"""SureChEMBL's bulk data, read in place from EMBL-EBI with DuckDB.

Since 2025 SureChEMBL publishes its entire database every two weeks as
parquet under https://ftp.ebi.ac.uk/pub/databases/chembl/SureChEMBL/bulk_data/
(one dated directory per release): `compounds` (31.0M rows, 3.9GB),
`patents` (45.2M rows, 5.5GB), `patent_compound_map` (1.54 billion rows,
4.7GB), the biomedical-entity annotations (gene/protein, disease,
mechanism: 1.06M entities, 453M locations), and two small lookup tables.
Figures from the 2026-09-08 release.

No mirror, no download: EBI's server honours HTTP range requests, so
DuckDB's httpfs reads only the parquet footers and the row groups a query
touches. Which queries that makes cheap was measured (2026-09-08) from
the files' own row-group statistics, not assumed:

- `compounds` and `patents` are sorted by `id` with min/max statistics
  per row group, so a lookup by id prunes to one ~15K-row (compounds)
  or ~52K-row (patents) group: 1-7s over HTTPS, single-digit MB read.
- `patent_compound_map` is sorted by `patent_id`, so "compounds in
  patent N" is one row group (~1-4s). The reverse, "patents containing
  compound N", has no usable statistics (every group spans the full id
  range) and would scan the 4.3GB compound_id column; use the REST
  `patents_for_compound()` for that instead.
- The string columns (`inchi_key`, `patent_number`) carry no statistics
  at all, so a lookup by InChIKey or publication number is a full
  column scan (600MB / 394MB). Use `by_inchikey()` (UniChem) and the
  REST document endpoints for those; or `download()` the table once and
  query it locally.

`sql()` is the general path: any DuckDB SQL over views named after the
tables. The helper functions cover the pruned lookups above.

Requires the `bulk` extra (`pip install "scigantic-surechembl[bulk]"`,
duckdb + pandas); the rest of the package does not import it.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _client
from ._ids import compound_ids
from .models import Compound, PatentRecord, _to_int

if TYPE_CHECKING:
    import duckdb
    import pandas

BULK_BASE = "https://ftp.ebi.ac.uk/pub/databases/chembl/SureChEMBL/bulk_data/"

TABLES = (
    "compounds",
    "patents",
    "patent_compound_map",
    "fields",
    "biomedical_entities",
    "biomedical_locations",
    "biomedical_types",
)

# fields.parquet, verified 2026-09-08. Field 5 is image-extracted chemistry
# (patents after 2007), field 6 is MOL attachments (US patents after 2007).
FIELDS = {1: "desc", 2: "clms", 3: "abst", 4: "ttl", 5: "image", 6: "molattachment"}

_RELEASE_RE = re.compile(r'href="(\d{4}-\d{2}-\d{2})/"')

_releases_cache: list[str] | None = None
_connections: dict[str, Any] = {}
_lock = threading.Lock()


def releases() -> list[str]:
    """Every bulk release date available on EBI, oldest first. Read from
    the directory listing once per process."""
    global _releases_cache
    if _releases_cache is None:
        response = _client.send("GET", BULK_BASE, timeout=60.0)
        if response.status_code >= 400:
            raise _client.SureChEMBLError(
                f"could not list {BULK_BASE}: HTTP {response.status_code}", http_status=response.status_code
            )
        found = sorted(set(_RELEASE_RE.findall(response.text)))
        if not found:
            raise _client.SureChEMBLError(f"no release directories found at {BULK_BASE}")
        _releases_cache = found
    return list(_releases_cache)


def latest_release() -> str:
    """The most recent release date, e.g. "2026-09-08". Display it next
    to anything derived from the bulk data: SureChEMBL's attribution
    terms ask that the release date be shown."""
    return releases()[-1]


def table_url(table: str, release: str | None = None) -> str:
    """HTTPS URL of one table's parquet file for a release (default: the
    latest)."""
    if table not in TABLES:
        raise ValueError(f"table must be one of {TABLES}, not {table!r}")
    return f"{BULK_BASE}{release or latest_release()}/{table}.parquet"


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "the bulk module needs duckdb and pandas: pip install 'scigantic-surechembl[bulk]'"
        ) from exc
    return duckdb


def _new_connection() -> duckdb.DuckDBPyConnection:
    module = _duckdb()
    con = module.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET parquet_metadata_cache = true;")
    return con  # type: ignore[no-any-return]


def _con(release: str) -> duckdb.DuckDBPyConnection:
    """One connection per release, httpfs loaded, parquet metadata cached
    so repeated lookups do not refetch a file's footer."""
    with _lock:
        con: duckdb.DuckDBPyConnection | None = _connections.get(release)
        if con is None:
            con = _new_connection()
            _connections[release] = con
        return con


def connect(release: str | None = None, tables: Iterable[str] = TABLES) -> duckdb.DuckDBPyConnection:
    """A fresh DuckDB connection with a view per table, for callers who
    want to run their own queries. Creating a view reads that file's
    footer (a few seconds each over HTTPS), so pass `tables` to bind
    only what you need."""
    release = release or latest_release()
    con = _new_connection()
    for table in tables:
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{table_url(table, release)}')")
    return con


def sql(query: str, release: str | None = None, tables: Iterable[str] | None = None) -> pandas.DataFrame:
    """Run DuckDB SQL against the bulk tables and return a DataFrame.

    Table names in the query resolve to the release's parquet files.
    Only the tables the query mentions are bound (or pass `tables`), so
    a query over `fields` does not pay to read three multi-GB footers.
    Remember what prunes and what scans (module docstring): a WHERE on
    `compounds.id`, `patents.id` or `patent_compound_map.patent_id`
    stays cheap; anything else over the big three reads whole columns.
    """
    release = release or latest_release()
    if tables is None:
        tables = [t for t in TABLES if re.search(rf"\b{t}\b", query)]
    con = connect(release, tables)
    try:
        return con.execute(query).df()
    finally:
        con.close()


def compound_record(id: int | str, release: str | None = None) -> Compound | None:
    """One compound's structure row (id, SMILES, InChI, InChIKey, weight)
    from the bulk table, or None. The property set (logP, HBD/HBA, ...)
    is REST-only; `compound()` has it."""
    found = compound_records([id], release)
    return next(iter(found.values()), None)


def compound_records(ids: Iterable[int | str], release: str | None = None) -> dict[int, Compound]:
    """Structure rows for many ids, keyed by id. Each id prunes to its
    own row group, so this scales with the number of distinct groups
    touched, not the table size."""
    wanted = compound_ids(ids)
    if not wanted:
        return {}
    release = release or latest_release()
    con = _con(release)
    placeholders = ", ".join("?" * len(wanted))
    rows = con.execute(
        f"SELECT id, smiles, inchi, inchi_key, mol_weight FROM read_parquet(?) WHERE id IN ({placeholders})",
        [table_url("compounds", release), *wanted],
    ).fetchall()
    return {
        int(r[0]): Compound(id=int(r[0]), smiles=r[1], inchi=r[2], inchi_key=r[3], mol_weight=r[4])
        for r in rows
    }


def patent_record(patent_id: int, release: str | None = None) -> PatentRecord | None:
    """One row of the bulk `patents` table by its bulk integer id."""
    release = release or latest_release()
    con = _con(release)
    row = con.execute(
        "SELECT id, patent_number, country, publication_date, family_id, title, assignee, cpc, ipcr, ipc, ecla "
        "FROM read_parquet(?) WHERE id = ?",
        [table_url("patents", release), int(patent_id)],
    ).fetchone()
    if row is None:
        return None
    family = _to_int(row[4])
    return PatentRecord(
        id=int(row[0]),
        patent_number=str(row[1]),
        country=row[2],
        publication_date=row[3],
        family_id=None if family is None or family < 0 else family,
        title=row[5],
        assignee=list(row[6] or []),
        cpc=list(row[7] or []),
        ipcr=list(row[8] or []),
        ipc=list(row[9] or []),
        ecla=list(row[10] or []),
    )


def patent_compounds(patent_id: int, release: str | None = None) -> pandas.DataFrame:
    """Every (compound_id, field) pair for one bulk patent id: which
    compounds SureChEMBL found in it and where (description, claims,
    abstract, title, image, MOL attachment). One row group; 1-4s.

    Hydrate the ids with `compounds()` (REST, batched) rather than a
    join against the bulk `compounds` table: a patent's compounds are
    spread across the whole id range, so the join would touch most of
    that 3.9GB file.
    """
    release = release or latest_release()
    con = _con(release)
    frame = con.execute(
        "SELECT compound_id, field_id FROM read_parquet(?) WHERE patent_id = ? ORDER BY compound_id, field_id",
        [table_url("patent_compound_map", release), int(patent_id)],
    ).df()
    frame["field"] = frame["field_id"].map(FIELDS)
    return frame


def download(table: str, dest: str | Path, release: str | None = None, chunk_size: int = 1 << 20) -> Path:
    """Download one table's parquet file to `dest`, resuming a partial
    file via a Range request and verifying the final size against the
    server's Content-Length. For the workloads the in-place path can't
    do (reverse map lookups, InChIKey/publication-number scans, whole-
    corpus substructure sweeps): download once, then query locally with
    `duckdb.sql("SELECT ... FROM 'compounds.parquet'")` or the same
    `sql()` here with a `duckdb.connect()` of your own."""
    url = table_url(table, release)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(f"{dest.suffix}.part")
    head = _client.send("HEAD", url, timeout=60.0)
    total = int(head.headers.get("Content-Length") or 0)
    have = part.stat().st_size if part.exists() else 0
    if dest.exists() and total and dest.stat().st_size == total:
        return dest
    headers = {"Range": f"bytes={have}-"} if have and total and have < total else {}
    session = _client._get_session()
    with session.get(url, headers=headers, stream=True, timeout=120.0) as response:
        if response.status_code == 416:  # already complete on disk
            have = total
        elif response.status_code not in (200, 206):
            raise _client.SureChEMBLError(
                f"download of {url} failed: HTTP {response.status_code}", http_status=response.status_code
            )
        else:
            mode = "ab" if response.status_code == 206 else "wb"
            with part.open(mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
    size = part.stat().st_size
    if total and size != total:
        raise _client.SureChEMBLError(f"download of {url} incomplete: {size} of {total} bytes; re-run to resume")
    os.replace(part, dest)
    return dest
