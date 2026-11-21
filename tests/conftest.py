import pytest

from scigantic_surechembl import SureChEMBLError, cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every test runs with a private cache directory so a stale entry on
    the developer's machine can't mask a live regression, and the live
    tests never write into the user's real cache."""
    cache.enable_cache(str(tmp_path_factory.mktemp("cache")))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):  # type: ignore[no-untyped-def]
    """These tests are live. A 5xx from SureChEMBL, or a request that got
    no response after retries, is the service's outage and not a package
    bug, so it skips the test instead of failing it. Any other
    SureChEMBLError (a 4xx, a non-JSON body) still fails."""
    outcome = yield
    exc = outcome.excinfo[1] if outcome.excinfo else None
    if isinstance(exc, SureChEMBLError) and (
        (exc.http_status or 0) >= 500 or "request failed after" in str(exc)
    ):
        outcome.force_exception(pytest.skip.Exception(f"SureChEMBL unavailable: {str(exc).splitlines()[0][:200]}"))
