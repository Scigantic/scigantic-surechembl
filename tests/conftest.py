import pytest

from scigantic_surechembl import cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every test runs with a private cache directory so a stale entry on
    the developer's machine can't mask a live regression, and the live
    tests never write into the user's real cache."""
    cache.enable_cache(str(tmp_path_factory.mktemp("cache")))
