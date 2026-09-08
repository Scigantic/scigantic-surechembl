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


def test_read_only_cache_dir_never_breaks_a_lookup(tmp_path: object, recwarn: pytest.WarningsRecorder) -> None:
    """Stress-test finding: a read-only cache directory raised
    PermissionError from every call. Now: one warning, cache off, live
    lookups keep working."""
    import os
    import stat
    from pathlib import Path

    ro = Path(str(tmp_path)) / "ro"
    ro.mkdir()
    cache.enable_cache(str(ro))
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert sc.compound(2871) is not None
        assert sc.compound(2871) is not None
        assert not cache.is_cache_enabled()
        assert any("cache disabled" in str(w.message) for w in recwarn.list)
    finally:
        os.chmod(ro, stat.S_IRWXU)
        cache.enable_cache()


def test_unusable_default_cache_dir_degrades(monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder) -> None:
    monkeypatch.setenv("SCIGANTIC_SURECHEMBL_CACHE", "/nonexistent_root_dir_for_test/cache")
    monkeypatch.setattr(cache, "_cache_dir", None)
    monkeypatch.setattr(cache, "_enabled", True)
    assert sc.compound(2871) is not None
    assert not cache.is_cache_enabled()


def test_corrupt_and_foreign_entries_are_misses() -> None:
    import json
    import time

    sc.compound(1353)
    (entry,) = list(cache.cache_dir().glob("*.json"))
    entry.write_text("{not json")
    assert cache.get(entry.stem) is None
    entry.write_text(json.dumps({"cached_at": time.time()}))  # no value key
    assert cache.get(entry.stem) is None
    entry.write_text(json.dumps({"cached_at": "yesterday", "value": 1}))  # bad timestamp
    assert cache.get(entry.stem) is None
    assert sc.compound(1353) is not None  # and the live path still works after all that
