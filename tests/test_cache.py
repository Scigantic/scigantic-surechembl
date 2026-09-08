from unittest import mock

import pytest

import scigantic_surechembl as sc
from scigantic_surechembl import _client, cache


def test_cache_key_is_stable_and_order_independent() -> None:
    assert cache.key("a", {"x": 1, "y": 2}) == cache.key("a", {"y": 2, "x": 1})
    assert cache.key("a", 1) != cache.key("a", 2)


def test_second_lookup_makes_no_network_call() -> None:
    first = sc.compound(1353)
    assert first is not None and first.id == 1353
    with mock.patch.object(_client, "send", side_effect=AssertionError("network call on cached lookup")):
        again = sc.compound("SCHEMBL1353")
    assert again == first


def test_disable_cache_hits_network() -> None:
    sc.compound(1353)
    cache.disable_cache()
    try:
        with mock.patch.object(_client, "send", side_effect=RuntimeError("expected")):
            with pytest.raises(RuntimeError):
                sc.compound(1353)
    finally:
        cache.enable_cache()


def test_expired_entry_is_refetched(tmp_path: pytest.TempPathFactory) -> None:
    cache.enable_cache(ttl_days=0)  # everything expires immediately
    sc.compound(1353)
    with mock.patch.object(_client, "send", side_effect=RuntimeError("expected")):
        with pytest.raises(RuntimeError):
            sc.compound(1353)


def test_clear_counts_files() -> None:
    sc.compound(1353)
    assert cache.clear() >= 1
    assert cache.clear() == 0
