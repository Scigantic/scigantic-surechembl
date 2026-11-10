from datetime import date

from scigantic_surechembl.models import Compound, PatentHit, _to_bool, _to_date


def test_compound_from_api_coerces_string_numbers_and_flags() -> None:
    # A /chemical/id record: id as a string, flags as "0"/"1" strings.
    record = {
        "id": "1353",
        "chemical_id": "1353",
        "name": "2-(acetyloxy)benzoic acid",
        "smiles": "CC(=O)OC1=C(C=CC=C1)C(O)=O",
        "inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        "mol_weight": 180.15699768066406,
        "is_element": "0",
        "global_frequency": 22,
        "mchem_struct_alert": "1",
        "similarity": "",
    }
    c = Compound.from_api(record)
    assert c.id == 1353
    assert c.schembl_id == "SCHEMBL1353"
    assert c.url == "https://www.surechembl.org/chemical/1353"
    assert c.is_element is False
    assert c.struct_alert is True
    assert c.similarity is None
    assert c.mol_formula is None  # absent from this record
    assert c.raw is record


def test_compound_from_api_similarity_hit() -> None:
    c = Compound.from_api({"id": "7", "similarity": "0.87", "organic": 1, "ro3_pass": 0})
    assert c.similarity == 0.87
    assert c.organic is True
    assert c.ro3_pass is False


def test_patent_hit_from_solr_document() -> None:
    # The shape GET /search/content returns (verified live 2026-11-09).
    hit = PatentHit.from_api(
        {
            "pa": ["NOVARTIS AG", "NOVARTIS AG", "Novartis AG"],
            "fam": "37847165",
            "pd": 20130410,
            "ttl_de": ["G-, I- und K-Kristallformen von Imatinibmesylat"],
            "ttl_en": ["G, I and K crystal forms of imatinib mesylate"],
            "ucid": "EP-2578580-A1",
            "score": 18.3,
        }
    )
    assert hit.doc_id == "EP-2578580-A1"
    assert hit.title == "G, I and K crystal forms of imatinib mesylate"
    assert hit.publication_date == date(2013, 4, 10)
    assert hit.assignee == "NOVARTIS AG"
    assert hit.assignees == ["NOVARTIS AG", "Novartis AG"]
    assert hit.family_id == 37847165

    ja = PatentHit.from_api({"ucid": "JP-5844508-B2", "ttl_ja": ["メシル酸イマチニブのＦ結晶形"], "pd": 20160120})
    assert ja.title == "メシル酸イマチニブのＦ結晶形"  # no English title: first in any language
    assert ja.assignee is None and ja.assignees == [] and ja.family_id is None


def test_date_and_bool_coercion() -> None:
    assert _to_date("20180619") == date(2018, 6, 19)
    assert _to_date("2018-06-19") == date(2018, 6, 19)
    assert _to_date("null") is None
    assert _to_date("") is None
    assert _to_date("19990230") is None  # not a real date, not an exception
    assert _to_bool("1") is True and _to_bool(0) is False and _to_bool(None) is None
