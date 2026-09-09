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
_TABLE_RE = re.compile(r'href="([a-z_]+)\.parquet"')

_releases_cache: list[str] | None = None
_release_tables_cache: dict[str, list[str]] = {}
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


def release_tables(release: str | None = None) -> list[str]:
    """The tables a release actually ships, from its directory listing.

    The schema is not fixed across releases (EBI says so, and it shows:
    the first release, 2025-04-30, has no biomedical tables and its
    `compounds` has `rdk_smiles` and no `inchi` column, verified
    2026-09-08). One GET per release, cached for the process."""
    release = release or latest_release()
    with _lock:
        cached = _release_tables_cache.get(release)
    if cached is not None:
        return list(cached)
    response = _client.send("GET", f"{BULK_BASE}{release}/", timeout=60.0)
    if response.status_code == 404:
        raise _client.SureChEMBLError(f"no SureChEMBL bulk release {release!r}; see releases()", http_status=404)
    if response.status_code >= 400:
        raise _client.SureChEMBLError(
            f"could not list release {release}: HTTP {response.status_code}", http_status=response.status_code
        )
    found = [t for t in TABLES if t in set(_TABLE_RE.findall(response.text))]
    with _lock:
        _release_tables_cache[release] = found
    return list(found)


def table_url(table: str, release: str | None = None) -> str:
    """HTTPS URL of one table's parquet file for a release (default: the
    latest). Does not check that the release ships the table; see
    release_tables()."""
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
    """A cursor on the one shared connection per release (httpfs loaded,
    parquet metadata cached so repeated lookups do not refetch a file's
    footer). A cursor rather than the connection itself: DuckDB's Python
    connection is not safe to use from two threads at once (verified
    2026-09-08: eight threads calling patent_compounds() on the shared
    connection got `'NoneType' object is not subscriptable` from five of
    them), while cursors on it are, and share its metadata cache."""
    with _lock:
        con: duckdb.DuckDBPyConnection | None = _connections.get(release)
        if con is None:
            con = _new_connection()
            _connections[release] = con
        return con.cursor()


def connect(release: str | None = None, tables: Iterable[str] | None = None) -> duckdb.DuckDBPyConnection:
    """A fresh DuckDB connection with a view per table, for callers who
    want to run their own queries. Creating a view reads that file's
    footer (a few seconds each over HTTPS), so pass `tables` to bind
    only what you need; the default binds every table the release
    ships. Asking for a table the release does not have raises
    SureChEMBLError naming it."""
    release = release or latest_release()
    available = release_tables(release)
    wanted = list(tables) if tables is not None else available
    missing = [t for t in wanted if t not in available]
    if missing:
        raise _client.SureChEMBLError(
            f"release {release} has no {', '.join(missing)} table(s); it ships {', '.join(available)}"
        )
    con = _new_connection()
    for table in wanted:
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
    con = connect(release, tables)  # raises naming any table the release lacks
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
    touched, not the table size (measured: 200 ids spread over the
    whole range, 154 found, 24 s). The first release (2025-04-30) is the
    exception: it names the SMILES column `rdk_smiles`, stores it as a
    BLOB, and is written in 1M-row groups, so one lookup there reads
    ~120 MB (134 s measured). Every release from 2025-06-01 on matches
    the current layout."""
    wanted = compound_ids(ids)
    if not wanted:
        return {}
    release = release or latest_release()
    url = table_url("compounds", release)
    cur = _con(release)
    try:
        columns = {row[0] for row in cur.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()}
        smiles_col = "smiles" if "smiles" in columns else "rdk_smiles" if "rdk_smiles" in columns else "NULL"
        inchi_col = "inchi" if "inchi" in columns else "NULL"
        placeholders = ", ".join("?" * len(wanted))
        rows = cur.execute(
            f"SELECT id, {smiles_col}, {inchi_col}, inchi_key, mol_weight FROM read_parquet(?) "
            f"WHERE id IN ({placeholders})",
            [url, *wanted],
        ).fetchall()
    finally:
        cur.close()
    return {
        int(r[0]): Compound(
            id=int(r[0]), smiles=_text(r[1]), inchi=_text(r[2]), inchi_key=_text(r[3]), mol_weight=r[4]
        )
        for r in rows
    }


def _text(value: Any) -> str | None:
    """The first release stores rdk_smiles as a BLOB; later ones as text."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def patent_record(patent_id: int, release: str | None = None) -> PatentRecord | None:
    """One row of the bulk `patents` table by its bulk integer id."""
    if isinstance(patent_id, bool) or int(patent_id) <= 0:
        raise ValueError(f"not a bulk patent id: {patent_id!r}")
    release = release or latest_release()
    cur = _con(release)
    try:
        row = cur.execute(
            "SELECT id, patent_number, country, publication_date, family_id, title, assignee, cpc, ipcr, ipc, ecla "
            "FROM read_parquet(?) WHERE id = ?",
            [table_url("patents", release), int(patent_id)],
        ).fetchone()
    finally:
        cur.close()
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
    if isinstance(patent_id, bool) or int(patent_id) <= 0:
        raise ValueError(f"not a bulk patent id: {patent_id!r}")
    release = release or latest_release()
    cur = _con(release)
    try:
        frame = cur.execute(
            "SELECT compound_id, field_id FROM read_parquet(?) WHERE patent_id = ? ORDER BY compound_id, field_id",
            [table_url("patent_compound_map", release), int(patent_id)],
        ).df()
    finally:
        cur.close()
    frame["field"] = frame["field_id"].map(FIELDS)
    return frame


# --- publication number -> bulk id ------------------------------------------


def patent_number_index_path(release: str | None = None) -> Path:
    """Default location of the local publication-number index for a
    release, under this package's cache directory."""
    from . import cache

    return cache.cache_dir() / f"patent_number_index_{release or latest_release()}.parquet"


def build_patent_number_index(dest: str | Path | None = None, release: str | None = None) -> Path:
    """Build a local index from publication number to bulk patent id.

    The bulk `patents` table is sorted by `id` and its `patent_number`
    column carries no statistics, so going from a publication number to
    its bulk row (and from there to `patent_compounds()`) is otherwise a
    scan of the whole 394 MB column on every lookup. This reads the two
    columns once from EBI (about 660 MB over HTTPS), sorts by publication
    number, and writes a zstd parquet file in 50,000-row groups so that
    `patent_id_for_number()` prunes to one group (10 ms per lookup).
    Measured 2026-09-08 on the 45M-row release: 110 s to build, 236 MB
    on disk. Rebuild per release; the file name carries the release date.
    """
    release = release or latest_release()
    out = Path(dest) if dest is not None else patent_number_index_path(release)
    out.parent.mkdir(parents=True, exist_ok=True)
    cur = _con(release)
    tmp = out.with_suffix(out.suffix + ".part")
    try:
        cur.execute(
            "COPY (SELECT patent_number, id, publication_date FROM read_parquet(?) ORDER BY patent_number) "
            f"TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)",
            [table_url("patents", release)],
        )
    finally:
        cur.close()
    os.replace(tmp, out)
    return out


def patent_id_for_number(doc_id: str, index: str | Path | None = None, release: str | None = None) -> int | None:
    """The bulk `patents.id` for a publication number, from the local index
    built by build_patent_number_index() (default path for the release).
    None if the release has no such publication. Raises FileNotFoundError
    naming the build call if the index does not exist yet."""
    from ._ids import patent_number

    normalized = patent_number(doc_id)
    path = Path(index) if index is not None else patent_number_index_path(release)
    if not path.exists():
        raise FileNotFoundError(
            f"no publication-number index at {path}; build it once with "
            "scigantic_surechembl.bulk.build_patent_number_index() (reads ~660 MB from EBI, writes ~236 MB)"
        )
    duckdb = _duckdb()
    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT id FROM read_parquet(?) WHERE patent_number = ? LIMIT 1", [path.as_posix(), normalized]
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else None


def patent_record_for_number(doc_id: str, index: str | Path | None = None, release: str | None = None) -> PatentRecord | None:
    """patent_record() addressed by publication number, via the local index."""
    pid = patent_id_for_number(doc_id, index, release)
    return None if pid is None else patent_record(pid, release)


def patent_compounds_for_number(doc_id: str, index: str | Path | None = None, release: str | None = None) -> pandas.DataFrame:
    """patent_compounds() addressed by publication number, via the local
    index. An unknown number gives an empty frame."""
    pid = patent_id_for_number(doc_id, index, release)
    if pid is None:
        import pandas as pd

        empty: pd.DataFrame = pd.DataFrame({"compound_id": [], "field_id": [], "field": []})
        return empty
    return patent_compounds(pid, release)


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
    if head.status_code == 404:
        raise _client.SureChEMBLError(
            f"{url} does not exist: check releases() and release_tables()", http_status=404
        )
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
