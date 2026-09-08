from datetime import date

from scigantic_surechembl.models import Compound, PatentHit, _to_bool, _to_date


def test_compound_from_api_coerces_string_numbers_and_flags() -> None:
    # The shape /chemical/name returns: numbers as strings, flags as "0"/"1".
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
    assert c.mol_formula is None  # not on the name endpoint
    assert c.raw is record


def test_compound_from_api_similarity_hit() -> None:
    c = Compound.from_api({"id": "7", "similarity": "0.87", "organic": 1, "ro3_pass": 0})
    assert c.similarity == 0.87
    assert c.organic is True
    assert c.ro3_pass is False


def test_patent_hit_prefers_english_title_and_maps_null_strings() -> None:
    hit = PatentHit.from_api(
        {
            "docId": "CN-106420664-A",
            "metadata": {
                "pd": "20170222",
                "titles": [
                    {"lang": "zh", "titles": ["阿司匹林"]},
                    {"lang": "en", "titles": ["Application of aspirin conjugate"]},
                ],
            },
            "pa": "UNIV FUZHOU",
        }
    )
    assert hit.title == "Application of aspirin conjugate"
    assert hit.publication_date == date(2017, 2, 22)
    assert hit.assignee == "UNIV FUZHOU"

    bare = PatentHit.from_api({"docId": "X-1-A", "metadata": {"pd": "null", "titles": []}, "pa": "null"})
    assert bare.title is None and bare.publication_date is None and bare.assignee is None


def test_date_and_bool_coercion() -> None:
    assert _to_date("20180619") == date(2018, 6, 19)
    assert _to_date("2018-06-19") == date(2018, 6, 19)
    assert _to_date("null") is None
    assert _to_date("") is None
    assert _to_date("19990230") is None  # not a real date, not an exception
    assert _to_bool("1") is True and _to_bool(0) is False and _to_bool(None) is None
