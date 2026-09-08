"""Live reads of EBI's parquet over HTTPS. Only the row-group-pruned
paths are exercised: anything else would pull hundreds of MB per test."""

import re

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pandas")

from scigantic_surechembl import bulk  # noqa: E402


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
    assert bulk.patent_record(0) is None


def test_sql_binds_only_mentioned_tables() -> None:
    fields = bulk.sql("SELECT id, field_name FROM fields ORDER BY id")
    assert dict(zip(fields["id"], fields["field_name"])) == bulk.FIELDS
    types = bulk.sql("SELECT type_name FROM biomedical_types ORDER BY id")
    assert list(types["type_name"])[:3] == ["GeneOrProtein", "Disease", "Mechanism"]
