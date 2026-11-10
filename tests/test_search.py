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
    assert all(h.mol_formula for h in hits)  # full records, fetched by id
    # The new API returns similarity hits best first (the old one interleaved).
    assert sims == sorted(sims, reverse=True)


def test_similarity_threshold_narrows_the_hits() -> None:
    loose = sc.similar_compounds(ASPIRIN, max_results=1000, threshold=0.8)
    tight = sc.similar_compounds(ASPIRIN, max_results=1000, threshold=0.95)
    assert 0 < len(tight) < len(loose)
    assert all(h.similarity is not None and h.similarity >= 0.95 for h in tight)
    with pytest.raises(ValueError):
        sc.similar_compounds(ASPIRIN, threshold=1.5)


def test_invalid_smiles_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid SMILES"):
        sc.structure_search("C1CC(((", "identical")


def test_identical_and_connectivity_modes() -> None:
    identical = {h.id for h in sc.structure_search(ASPIRIN, "identical", max_results=20)}
    connectivity = {h.id for h in sc.structure_search(ASPIRIN, "connectivity", max_results=20)}
    assert 1353 in identical
    assert identical <= connectivity  # connectivity relaxes stereo/isotopes, so it's a superset


def test_substructure_search_returns_full_records() -> None:
    hits = sc.substructure_search("c1ccc2ncccc2c1", max_results=250)
    assert len(hits) == 250
    assert len({h.id for h in hits}) == 250
    assert sum(1 for h in hits if h.smiles) >= 245  # hydrated through /chemical/id


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


def test_structure_search_pages_dedups_and_stops_on_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripted, so the paging guard is exercised deterministically: page
    size 1000, a page that repeats ids, and a stop on `total` without
    asking for the page past it."""
    import sys

    from scigantic_surechembl import _client, search

    compounds_mod = sys.modules["scigantic_surechembl.compounds"]  # the package re-exports a function of that name

    pages = {
        1: list(range(1, 1001)),
        2: list(range(990, 1990)),  # 10 repeats from page 1
        3: list(range(1990, 2000)),
    }
    calls: list[int] = []

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
        body = kwargs.get("json_body") or {}
        assert isinstance(body, dict)
        assert body["struct_search_type"] == "connectivity" and "threshold" not in body
        calls.append(int(body["page"]))
        return {"chemical_ids": pages[int(body["page"])], "total": 1999}

    monkeypatch.setattr(_client, "request", fake_request)
    monkeypatch.setattr(compounds_mod, "compounds", lambda ids: {})
    hits = search.structure_search("CCCCCC", "connectivity", max_results=10_000)
    assert [h.id for h in hits] == list(range(1, 2000))
    assert calls == [1, 2, 3]


def test_patents_for_compound_caps_page_size() -> None:
    # A page size above the cap is clamped rather than sent.
    hits = sc.patents_for_compound(1353, max_results=600, page_size=5000)
    assert len(hits) == 600
    assert len({h.doc_id for h in hits}) == 600


def test_search_patents_quoted_publication_number() -> None:
    assert sc.count_patents('pn:"US-10000000-B2"') == 1
    (hit,) = sc.search_patents('ucid:"US-8592440-B2"', max_results=5)
    assert hit.doc_id == "US-8592440-B2" and hit.family_id == 37847165
    assert hit.title == "Crystalline form I of imatinib mesylate"
    assert hit.assignees and len(hit.assignees) == len(set(hit.assignees))


def test_search_patents_errors_fail_fast() -> None:
    import time

    with pytest.raises(ValueError):
        sc.search_patents("   ")
    start = time.monotonic()
    with pytest.raises(SureChEMBLError) as info:
        sc.count_patents("ttl:kinase AND (")
    # A Solr syntax error is a 503 from the API; it must not be retried
    # like a gateway 503 (five retries would take 30 s).
    assert info.value.http_status == 503
    assert "SyntaxError" in str(info.value)
    assert time.monotonic() - start < 10


def test_many_ids_is_an_intersection() -> None:
    both = sc.count_patents_for_compound([1353, 5671])
    assert 0 < both < min(sc.count_patents_for_compound(1353), sc.count_patents_for_compound(5671))
    with pytest.raises(ValueError):
        sc.count_patents_for_compound(range(1, 502))


def test_trivially_broad_substructure_queries_are_refused_locally() -> None:
    # Never sent: a single atom or a bare small ring has taken the shared
    # substructure service down (observed twice on 2026-09-08).
    for q in ("C", "[#6]", "CC", "O=C", "C1CC1"):
        with pytest.raises(ValueError, match="broad"):
            sc.substructure_search(q)
    # Other modes are not gated: identical/similarity on a small molecule are cheap.
    assert sc.structure_search("O", "identical", 5)
