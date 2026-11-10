from datetime import date

import scigantic_surechembl as sc


def test_patent_document_fields() -> None:
    p = sc.patent("US10000000B2")
    assert p is not None
    assert p.doc_id == "US-10000000-B2"
    assert p.title == "Coherent LADAR using intra-pixel quadrature detection"
    assert p.published == date(2018, 6, 19)
    assert p.abstract and p.abstract.startswith("A frequency modulated")
    assert p.claims and p.claims.startswith("What is claimed is:")
    assert "<" not in p.claims  # the office XML markup is stripped
    assert p.description and p.description.startswith("TECHNICAL FIELD")
    assert "RAYTHEON COMPANY" in p.assignees
    assert p.inventors == ["Joseph Marron"]  # three formats of the same person, one name
    assert p.family_id == 55456961  # from the search index; the record has none
    assert p.application_number == "US-201514643719-A"
    # Gone from the late-2026 API: empty, never invented.
    assert p.cpc == [] and p.citations == [] and p.legal_events == []
    assert p.has_pdf is None
    assert p.pdf_url == "https://www.surechembl.org/api/patent/pdf/US-10000000-B2"
    assert "parties" in p.raw


def test_patent_parties_priorities_and_ipc() -> None:
    p = sc.patent("US-8592440-B2")
    assert p is not None
    assert p.inventors == ["Michael Mutz"]
    assert "NOVARTIS AG" in p.assignees
    assert "GB-0524062-A" in p.priority_numbers
    assert p.family_id == 37847165
    v = sc.patent("US-5399578-A")  # one of the few records with a classification block
    assert v is not None and "C07D257/04" in v.ipcr


def test_patent_miss_is_none() -> None:
    assert sc.patent("US-99999999999-B2") is None


def test_patent_pdf() -> None:
    pdf = sc.patent_pdf("US-8592440-B2")
    assert pdf is not None and pdf.startswith(b"%PDF")
    assert sc.patent_pdf("US-99999999999-B2") is None


def test_patent_chemistry_from_text_mining() -> None:
    hits = sc.patent_chemistry("US-8592440-B2")
    ids = {h.id for h in hits}
    assert 36204112 in ids  # "imatinib mesylate" in the title
    assert len(ids) == len(hits)  # de-duplicated
    assert all(h.inchi_key for h in hits)
    assert sc.patent_chemistry("US-99999999999-B2") == []
    # A document whose text names no compound is [], not an error.
    assert sc.patent_chemistry("US-10000000-B2") == []


def test_family() -> None:
    assert sc.family_id("US-10000000-B2") == 55456961
    members = sc.family_members("US10000000B2")
    assert "US-10000000-B2" in members
    assert "WO-2016144528-A1" in members
    assert len(members) == len(set(members))
    assert sc.family_id("US-99999999999-B2") is None
    assert sc.family_members("US-99999999999-B2") == []


def test_family_is_limited_to_five_offices() -> None:
    members = sc.family_members("US-8592440-B2")
    assert len(members) >= 20
    assert {m.split("-")[0] for m in members} <= {"US", "EP", "WO", "JP", "CN"}


def test_patent_numbers_with_letter_prefixes_resolve() -> None:
    # JP era letters, JP national-phase WO, US reissue/design/plant: all
    # real SureChEMBL publication numbers (from the bulk table) that a
    # digits-only parser rejected.
    for number, year in [("JP-S60211903-A", 1985), ("JP-WO2018116905-A1", 2018), ("US-RE30000-E", 1979), ("US-PP12345-P2", 2002)]:
        p = sc.patent(number)
        assert p is not None, number
        assert p.published is not None and p.published.year == year


def test_title_is_the_office_title_on_multi_title_records() -> None:
    # Two English titles in the record, the office's and a vendor's
    # descriptive one, and the record lists them in either order. The
    # search index holds the office's, which is what `title` takes.
    p = sc.patent("US-7196237-B2")
    assert p is not None
    assert len(p.titles) == 2
    assert p.title == "Method of preparing an alkyl aromatic product"
    v = sc.patent("US-5399578-A")
    assert v is not None and v.title == "Acyl compounds"
    assert v.titles[0].startswith("Hypotensive")
    p2 = sc.patent("US-10000000-B2")
    assert p2 is not None and p2.titles == [p2.title]


def test_patent_outside_the_index_still_parses() -> None:
    # DE is not in the search index, so no family and the record's title.
    p = sc.patent("DE-102005001234-A1")
    assert p is not None
    assert p.title == "Bremszylinder mit Parkbremsfunktion"
    assert p.family_id is None
