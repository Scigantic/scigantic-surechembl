import pytest

import scigantic_surechembl as sc
from scigantic_surechembl import SureChEMBLError

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


def test_similarity_search_hits_carry_similarity() -> None:
    hits = sc.similar_compounds(ASPIRIN, max_results=10)
    assert 0 < len(hits) <= 10
    sims = [h.similarity for h in hits]
    assert all(s is not None and 0 < s <= 1 for s in sims)
    assert any(h.id == 1353 for h in hits)
    # Not asserted: descending order. Verified live 2026-09-08 that the
    # server interleaves (1.0, 1.0, 0.96, 1.0, ...); sort client-side.


def test_identical_and_connectivity_modes() -> None:
    identical = {h.id for h in sc.structure_search(ASPIRIN, "identical", max_results=20)}
    connectivity = {h.id for h in sc.structure_search(ASPIRIN, "connectivity", max_results=20)}
    assert 1353 in identical
    assert identical <= connectivity  # connectivity relaxes stereo/isotopes, so it's a superset


def test_substructure_search_pages_past_one_request() -> None:
    try:
        hits = sc.substructure_search("c1ccc2ncccc2c1", max_results=250)
    except SureChEMBLError as exc:
        if "failed on the server" in str(exc):
            # Observed live 2026-09-08: the substructure worker can report
            # "Search not complete due to internal error." for every query
            # while similarity keeps working. The package's job is to fail
            # fast and clearly there, which the raise above just verified.
            pytest.skip(f"SureChEMBL substructure search is down: {exc}")
        raise
    assert len(hits) == 250
    assert len({h.id for h in hits}) == 250


def test_structure_search_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        sc.structure_search(ASPIRIN, "fuzzy")


def test_patents_for_compound_and_count() -> None:
    total = sc.count_patents_for_compound(1353)
    assert total > 100_000  # aspirin is in hundreds of thousands of patents
    hits = sc.patents_for_compound("SCHEMBL1353", max_results=3)
    assert len(hits) == 3
    assert all("-" in h.doc_id for h in hits)


def test_patents_for_compound_exhausts_a_rare_compound() -> None:
    # A compound found in a few dozen documents (15067423, 44 documents on
    # 2026-09-08) ends the loop on total_hits, not on max_results.
    total = sc.count_patents_for_compound(15067423)
    assert 0 < total < 500
    hits = sc.patents_for_compound(15067423, max_results=1000, page_size=20)
    assert len(hits) == total
    assert len({h.doc_id for h in hits}) == total


def test_search_patents_with_solr_fields() -> None:
    hits = sc.search_patents("ttl:aspirin AND pdyear:2024", max_results=5)
    assert 0 < len(hits) <= 5
    assert all(h.publication_date is not None and h.publication_date.year == 2024 for h in hits)
    assert sc.count_patents("ttl:aspirin AND pdyear:2024") >= len(hits)


def test_structure_search_dedups_and_stops_on_num_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verified live 2026-09-08: a 232-hit similarity search paged at 100
    returns 98, 99, 31 records, and page 4 repeats page 3. Scripted here
    so the guard is exercised deterministically."""
    from scigantic_surechembl import _client, search

    pages = {
        1: [{"id": str(i)} for i in range(1, 99)],
        2: [{"id": str(i)} for i in range(99, 198)],
        3: [{"id": str(i)} for i in range(198, 229)],
        4: [{"id": str(i)} for i in range(198, 229)],  # the repeat
    }
    calls: list[int] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        params = kwargs.get("params") or {}
        assert isinstance(params, dict)
        page = int(params["page"])
        calls.append(page)
        return {"results": {"structures": pages[page]}, "pagination": {"num_pages": 3}}

    monkeypatch.setattr(search, "_submit_and_wait", lambda *a, **k: ("hash", 232))
    monkeypatch.setattr(_client, "request", fake_request)
    hits = sc.structure_search("CC", "similarity", max_results=10_000)
    assert len(hits) == 228
    assert len({h.id for h in hits}) == 228
    assert calls == [1, 2, 3]  # never asked for the repeating page 4


def test_patents_for_compound_caps_page_size() -> None:
    # itemsPerPage=500 fails server-side with a Solr "414 URI Too Long"
    # (verified live); the package caps the page size, so this must work.
    hits = sc.patents_for_compound(1353, max_results=300, page_size=1000)
    assert len(hits) == 300
    assert len({h.doc_id for h in hits}) == 300


def test_search_patents_quoted_publication_number() -> None:
    assert sc.count_patents('pn:"US-10000000-B2"') == 1
