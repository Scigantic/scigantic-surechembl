"""Live reads of EBI's parquet over HTTPS. Only the row-group-pruned
paths are exercised: anything else would pull hundreds of MB per test."""

import re

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pandas")

from scigantic_surechembl import SureChEMBLError, bulk  # noqa: E402


def test_releases_are_dates_ascending() -> None:
    rel = bulk.releases()
    assert len(rel) > 10
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r) for r in rel)
    assert rel == sorted(rel)
    assert bulk.latest_release() == rel[-1]
    assert bulk.table_url("fields").endswith(f"/{rel[-1]}/fields.parquet")


def test_table_url_rejects_unknown_table() -> None:
    with pytest.raises(ValueError):
        bulk.table_url("nope")


def test_compound_record_pruned_lookup() -> None:
    c = bulk.compound_record("SCHEMBL1353")
    assert c is not None
    assert c.inchi_key == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert c.mol_formula is None  # structure-only table
    assert bulk.compound_record(999_999_999) is None
    many = bulk.compound_records([1353, 2871, 999_999_999])
    assert set(many) == {1353, 2871}


def test_patent_record_and_compounds() -> None:
    p = bulk.patent_record(10)
    assert p is not None
    assert p.patent_number == "US-5399578-A"
    assert p.country == "US"
    assert p.family_id == 25684817
    assert p.assignee and p.cpc
    frame = bulk.patent_compounds(10)
    assert len(frame) > 100
    assert set(frame["field"].dropna()) <= set(bulk.FIELDS.values())
    assert bulk.patent_record(56_160_770_000) is None  # far past the last id


def test_sql_binds_only_mentioned_tables() -> None:
    fields = bulk.sql("SELECT id, field_name FROM fields ORDER BY id")
    assert dict(zip(fields["id"], fields["field_name"])) == bulk.FIELDS
    types = bulk.sql("SELECT type_name FROM biomedical_types ORDER BY id")
    assert list(types["type_name"])[:3] == ["GeneOrProtein", "Disease", "Mechanism"]


def test_release_tables_and_schema_drift() -> None:
    first = bulk.releases()[0]
    assert bulk.release_tables(first) == ["compounds", "patents", "patent_compound_map", "fields"]
    assert "biomedical_types" in bulk.release_tables()
    with pytest.raises(SureChEMBLError, match="has no biomedical_types"):
        bulk.sql("SELECT count(*) FROM biomedical_types", first)
    with pytest.raises(SureChEMBLError, match="no SureChEMBL bulk release"):
        bulk.release_tables("1999-01-01")


def test_bulk_ids_are_validated() -> None:
    with pytest.raises(ValueError):
        bulk.patent_record(0)
    with pytest.raises(ValueError):
        bulk.patent_compounds(-1)


def test_helpers_are_safe_across_threads() -> None:
    # The shared per-release DuckDB connection is not thread-safe; the
    # helpers use cursors on it. Verified failing (5 of 8) before that.
    from concurrent.futures import ThreadPoolExecutor

    ids = [10, 5000, 1_000_000, 20_000_100, 30_000_000, 45_000_000, 55_000_000, 56_000_000]
    with ThreadPoolExecutor(8) as ex:
        frames = list(ex.map(bulk.patent_compounds, ids))
    assert all(set(f.columns) >= {"compound_id", "field_id", "field"} for f in frames)
    assert len(frames[0]) > 100


def test_download_resumes(tmp_path: pytest.TempPathFactory) -> None:
    import requests
    from pathlib import Path

    dest = Path(str(tmp_path)) / "fields.parquet"
    full = requests.get(bulk.table_url("fields"), timeout=60).content
    dest.with_suffix(".parquet.part").write_bytes(full[:700])
    assert bulk.download("fields", dest) == dest
    assert dest.read_bytes() == full
    assert bulk.download("fields", dest) == dest  # already complete: no re-download
