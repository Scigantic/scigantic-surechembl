"""Live against UniChem and SureChEMBL."""

import pytest

import scigantic_surechembl as sc
from scigantic_surechembl import _unichem

ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def test_xrefs_for_a_surechembl_compound() -> None:
    x = sc.xrefs("SCHEMBL1353")
    assert x is not None
    assert x.inchi_key == ASPIRIN_KEY
    assert set(x.surechembl) >= {1353, 29350479}
    assert x.chembl == ["CHEMBL25"]
    assert x.pubchem_cid == [2244]
    assert x.drugbank == ["DB00945"]
    assert x.chebi == ["CHEBI:15365"]
    assert x.pdb_ligand == ["AIN"]
    assert x.schembl_ids[0] == "SCHEMBL1353"
    assert 1 in x.sources and 15 in x.sources  # every UniChem source kept


def test_xrefs_from_other_sources_agree() -> None:
    from_chembl = sc.xrefs_for("chembl", "CHEMBL25")
    from_cid = sc.xrefs_for("pubchem", 2244)
    from_key = sc.xrefs_for_inchikey(ASPIRIN_KEY)
    assert from_chembl is not None and from_cid is not None and from_key is not None
    assert from_chembl.surechembl == from_cid.surechembl == from_key.surechembl
    assert from_chembl.inchi_key == ASPIRIN_KEY  # recovered from the SureChEMBL compound
    assert sc.surechembl_ids_for("chembl", "CHEMBL113") == [5671]  # caffeine


def test_xrefs_misses_and_bad_source() -> None:
    assert sc.xrefs_for("chembl", "CHEMBL999999999") is None
    assert sc.xrefs(999_999_999) is None
    assert sc.surechembl_ids_for("chembl", "CHEMBL999999999") == []
    with pytest.raises(ValueError):
        sc.xrefs_for("nope", 1)
    assert _unichem.source_id("PubChem") == 22


def test_patents_by_foreign_identifier_are_a_union_over_surechembl_ids() -> None:
    hits = sc.patents_for_chembl("CHEMBL25", max_results=30)
    assert len(hits) == 30
    assert len({h.doc_id for h in hits}) == 30
    caffeine = sc.patents_for_pubchem_cid(2519, max_results=5)
    assert len(caffeine) == 5
    assert sc.patents_for_inchikey(ASPIRIN_KEY, max_results=3)
    assert sc.patents_for_chembl("CHEMBL999999999") == []
