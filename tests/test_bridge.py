"""Live against scigantic-chembl's and scigantic-bindingdb's public S3
mirrors and scigantic-pubchem's PUG REST client."""

import pytest

pytest.importorskip("scigantic_chembl")
pytest.importorskip("scigantic_bindingdb")
pytest.importorskip("scigantic_pubchem")

from scigantic_surechembl import bridge  # noqa: E402


def test_chembl_compound_by_structure_for_both_duplicate_ids() -> None:
    a = bridge.chembl_compound(1353)
    b = bridge.chembl_compound("SCHEMBL29350479")
    assert a is not None and b is not None
    assert a["chembl_id"] == b["chembl_id"] == "CHEMBL25"
    assert a["pref_name"] == "ASPIRIN" and a["max_phase"] == 4.0
    assert a["surechembl_id"] == 1353 and b["surechembl_id"] == 29350479
    assert bridge.chembl_compound(999_999_999) is None


def test_chembl_activities_are_potency_ordered() -> None:
    frame = bridge.chembl_activities(5671, limit=20)  # caffeine
    assert len(frame) == 20
    assert {"standard_type", "pchembl_value", "target_name", "assay_chembl_id"} <= set(frame.columns)
    p = frame["pchembl_value"].dropna().tolist()
    assert p == sorted(p, reverse=True)


def test_chembl_matches_for_a_whole_patent() -> None:
    # The valsartan patent: 986 compounds from the 0.3 export, about 450
    # from the text-mining endpoint that replaced it.
    frame = bridge.chembl_matches_for_patent("US-5399578-A")
    assert len(frame) > 300
    assert frame["chembl_id"].notna().sum() > 100
    assert "VALSARTAN" in set(frame["pref_name"].dropna())
    top = frame.iloc[0]
    assert top["max_phase"] == 4.0 and top["n_activities"] > 1000  # approved drugs with data first
    assert (frame["n_activities"] >= 0).all()


def test_bindingdb_measurements_by_structure_and_by_patent() -> None:
    by_structure = bridge.bindingdb_measurements(1353)
    assert len(by_structure) > 50
    assert "ChEMBL" in set(by_structure["curation_source"])
    by_patent = bridge.bindingdb_measurements_for_patent("US-11566007-B2")
    assert len(by_patent) > 5000
    assert set(by_patent["patent_number"]) == {"US11566007"}
    assert bridge.bindingdb_measurements_for_patent("US-1234567-A").empty


def test_bindingdb_overlap_joins_on_full_key_and_skeleton() -> None:
    # Since 0.4.0 patent_chemistry() is text-mined only, so the overlap is
    # much smaller than the 0.3 export gave (562 of 619 skeleton matches
    # for US-11566007-B2 then; 6 on 2026-11-09). The join itself is what
    # is checked here: BindingDB drew this patent's ligands without
    # stereo, so no full key matches, and the skeleton column still finds
    # some.
    frame = bridge.bindingdb_overlap_for_patent("US-11566007-B2")
    assert frame["surechembl_id"].notna().sum() == 0
    assert frame["surechembl_skeleton_ids"].map(len).gt(0).sum() > 0
    # A patent where the full keys do agree (121 of 774 ligands on 2026-11-09).
    frame2 = bridge.bindingdb_overlap_for_patent("US-10730877-B2")
    ligands = frame2.drop_duplicates("ligand_inchi_key")
    assert ligands["surechembl_id"].notna().sum() > 50


def test_pubchem_compound_via_unichem_cid() -> None:
    c = bridge.pubchem_compound(1353)
    assert c is not None and c.cid == 2244 and c.title == "Aspirin"
    assert bridge.pubchem_compound(999_999_999) is None
