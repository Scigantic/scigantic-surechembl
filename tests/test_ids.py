import pytest

from scigantic_surechembl import compound_id, patent_number, schembl_id
from scigantic_surechembl._ids import compound_ids


def test_compound_id_accepts_every_spelling() -> None:
    assert compound_id(1353) == 1353
    assert compound_id("1353") == 1353
    assert compound_id("SCHEMBL1353") == 1353
    assert compound_id("schembl1353") == 1353
    assert compound_id(" SCHEMBL1353 ") == 1353


@pytest.mark.parametrize("bad", ["", "SCHEMBL", "CHEMBL25", "1353x", 0, -1, True, "SCHEMBL0"])
def test_compound_id_rejects_non_ids(bad: object) -> None:
    with pytest.raises(ValueError):
        compound_id(bad)  # type: ignore[arg-type]


def test_compound_ids_dedups_and_keeps_order() -> None:
    assert compound_ids(["SCHEMBL5", 3, "5", 3, 9]) == [5, 3, 9]


def test_schembl_id_round_trip() -> None:
    assert schembl_id(1353) == "SCHEMBL1353"
    assert schembl_id("SCHEMBL1353") == "SCHEMBL1353"
    assert compound_id(schembl_id(42)) == 42


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("US-10000000-B2", "US-10000000-B2"),
        ("US10000000B2", "US-10000000-B2"),
        ("us10000000b2", "US-10000000-B2"),
        ("US 10000000 B2", "US-10000000-B2"),
        ("WO2016/144528A1", "WO-2016144528-A1"),
        ("WO-2016144528-A1", "WO-2016144528-A1"),
        ("EP3268771B1", "EP-3268771-B1"),
        ("US10000000", "US-10000000"),
    ],
)
def test_patent_number_normalizes(raw: str, normalized: str) -> None:
    assert patent_number(raw) == normalized


@pytest.mark.parametrize("bad", ["", "10000000", "USB2", "SCHEMBL1353"])
def test_patent_number_rejects_non_numbers(bad: str) -> None:
    with pytest.raises(ValueError):
        patent_number(bad)
