from datetime import date

import scigantic_surechembl as sc


def test_patent_document_fields() -> None:
    p = sc.patent("US10000000B2")
    assert p is not None
    assert p.doc_id == "US-10000000-B2"
    assert p.title == "Coherent LADAR using intra-pixel quadrature detection"
    assert p.published == date(2018, 6, 19)
    assert p.abstract and p.abstract.startswith("A frequency modulated")
    assert p.claims and "What is claimed" in p.claims
    assert p.description and "TECHNICAL FIELD" in p.description
    assert "RAYTHEON COMPANY" in p.assignees
    assert p.inventors == ["Joseph Marron"]  # three formats of the same person, one name
    assert p.cpc and p.cpc[0].startswith("G01S")
    assert p.family_id == 55456961
    assert p.application_number == "US-201514643719-A"
    assert p.citations and all("-" in c for c in p.citations)
    assert p.legal_events and p.legal_events[0].code
    assert p.pdf_url == "https://www.surechembl.org/assets/pdf/US-10000000-B2"
    assert "contents" in p.raw


def test_patent_miss_is_none() -> None:
    assert sc.patent("US-99999999999-B2") is None


def test_patent_chemistry() -> None:
    hits = sc.patent_chemistry("WO-2016144528-A1")
    assert any(h.id == 5588 for h in hits)
    assert all(h.mol_formula for h in hits)
    assert sc.patent_chemistry("US-99999999999-B2") == []


def test_family() -> None:
    assert sc.family_id("US-10000000-B2") == 55456961
    members = sc.family_members("US10000000B2")
    assert "US-10000000-B2" in members
    assert "WO-2016144528-A1" in members
    assert sc.family_members("US-99999999999-B2") == []


def test_patent_numbers_with_letter_prefixes_resolve() -> None:
    # JP era letters, JP national-phase WO, US reissue/design/plant: all
    # real SureChEMBL publication numbers (from the bulk table) that a
    # digits-only parser rejected.
    for number, year in [("JP-S60211903-A", 1985), ("JP-WO2018116905-A1", 2018), ("US-RE30000-E", 1979), ("US-PP12345-P2", 2002)]:
        p = sc.patent(number)
        assert p is not None, number
        assert p.published is not None and p.published.year == year


def test_title_follows_surechembl_bulk_choice_on_multi_title_records() -> None:
    # Two English titles in the record; SureChEMBL's bulk patents.title is
    # the last one listed (verified on every such record found, 4 of 4).
    p = sc.patent("US-7196237-B2")
    assert p is not None
    assert len(p.titles) == 2
    assert p.title == "Method of preparing an alkyl aromatic product"
    p2 = sc.patent("US-10000000-B2")
    assert p2 is not None and p2.titles == [p2.title]
