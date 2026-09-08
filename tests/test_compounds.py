"""Live against SureChEMBL's REST API and UniChem, no mocks, matching the
rest of the scigantic-* family: a test that passes against a fixture
proves nothing about an API whose miss/error behaviour is this uneven."""

import scigantic_surechembl as sc

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def test_compound_by_id_has_full_property_set() -> None:
    c = sc.compound(1353)
    assert c is not None
    assert c.schembl_id == "SCHEMBL1353"
    assert c.inchi_key == ASPIRIN_KEY
    assert c.mol_formula == "C9H8O4"
    assert c.hbd == 1 and c.hba == 3
    assert c.global_frequency is not None and c.global_frequency > 0


def test_compound_accepts_schembl_prefix() -> None:
    # The API itself 500s on "SCHEMBL1353"; the package must strip it.
    assert sc.compound("SCHEMBL1353") == sc.compound(1353)


def test_compound_miss_is_none() -> None:
    assert sc.compound(999_999_999) is None


def test_compounds_batch_keys_by_id_and_drops_misses() -> None:
    found = sc.compounds(["SCHEMBL1353", 2871, 999_999_999])
    assert set(found) == {1353, 2871}
    assert found[1353].inchi_key == ASPIRIN_KEY


def test_compounds_batch_chunks_large_requests() -> None:
    ids = list(range(1000, 1600))  # two chunks at the 500 batch size
    found = sc.compounds(ids)
    assert 500 < len(found) <= 600
    assert all(1000 <= i < 1600 for i in found)


def test_by_name() -> None:
    hits = sc.by_name("aspirin")
    assert any(h.id == 1353 for h in hits)
    assert sc.by_name("zzzzqqqqnotaname") == []


def test_by_smiles_with_stereo_slashes() -> None:
    c = sc.by_smiles("C/C=C/C(=O)O")
    assert c is not None
    assert c.inchi_key == "LDHQCZJRKDOVOX-NSCUHMNNSA-N"


def test_by_smiles_miss_and_invalid_are_none() -> None:
    assert sc.by_smiles("notasmiles((") is None


def test_by_inchikey_via_unichem_returns_every_duplicate() -> None:
    hits = sc.by_inchikey(ASPIRIN_KEY)
    ids = {h.id for h in hits}
    assert 1353 in ids
    assert len(ids) >= 2  # SureChEMBL holds aspirin under more than one id
    assert all(h.inchi_key == ASPIRIN_KEY for h in hits)


def test_by_inchikey_unknown() -> None:
    assert sc.by_inchikey("AAAAAAAAAAAAAA-UHFFFAOYSA-N") == []


def test_structure_image_is_png() -> None:
    png = sc.structure_image(ASPIRIN, 120, 120)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_by_inchikey_rejects_malformed_keys_before_any_request() -> None:
    # A malformed key would otherwise cost UniChem's slow miss path.
    import pytest

    for bad in ("BSYNRYMUTXBXSQ", "not a key", "", "BSYNRYMUTXBXSQ-UHFFFAOYSA"):
        with pytest.raises(ValueError):
            sc.by_inchikey(bad)
    # Case and an "InChIKey=" prefix are normalized rather than rejected.
    assert {c.id for c in sc.by_inchikey("InChIKey=bsynrymutxbxsq-uhfffaoysa-n")} >= {1353}


def test_by_name_rejects_what_the_endpoint_cannot_take() -> None:
    import pytest

    with pytest.raises(ValueError):
        sc.by_name("")
    with pytest.raises(ValueError):
        sc.by_name("cis/trans-stilbene")  # a slash cannot travel in the path


def test_structure_image_rejects_bad_input() -> None:
    import pytest

    with pytest.raises(ValueError):
        sc.structure_image(ASPIRIN, 0, 100)
    with pytest.raises(sc.SureChEMBLError):
        sc.structure_image("notasmiles((", 100, 100)  # 200 with an empty body, verified live
