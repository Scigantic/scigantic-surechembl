"""UniChem (EMBL-EBI) as the identifier bridge: InChIKey or any source's
compound id in, every registered source's ids out.

Two endpoints are used, because neither is reliable on its own (measured
2026-09-08, see compounds.by_inchikey's history in the CHANGELOG): the
legacy `rest/inchikey/{key}` GET and the v1 `POST api/v1/compounds` each
hung on roughly one request in six, a hung request either never answers
or answers as an HTML 500 after ~32 s, and one that does answer does so
in under a second. So a lookup is hedged: the first request goes out,
and if it has not answered within _HEDGE_DELAY a second one is sent
(the other endpoint for an InChIKey; the same v1 endpoint again for a
source id, since the legacy id-mapping endpoints return a 404 page).
First good answer wins, the other is abandoned to its own timeout. The
delay keeps the second request off UniChem in the common case, so this
does not double the load on a shared service.

Source ids come from UniChem's own registry (`/api/v1/sources/`), the
ones this package names verified live: 1 ChEMBL, 2 DrugBank, 3 RCSB PDB
ligand, 7 ChEBI, 14 FDA SRS (UNII), 15 SureChEMBL, 22 PubChem, 31
BindingDB, 32 CompTox.
"""

from __future__ import annotations

import re
import time
import warnings
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import requests

from . import _client, cache

V1_URL = "https://www.ebi.ac.uk/unichem/api/v1/compounds"
LEGACY_INCHIKEY_URL = "https://www.ebi.ac.uk/unichem/rest/inchikey/"

SOURCES = {
    1: "chembl",
    2: "drugbank",
    3: "pdb",
    7: "chebi",
    14: "fdasrs",
    15: "surechembl",
    22: "pubchem",
    31: "bindingdb",
    32: "comptox",
}
SOURCE_IDS = {name: sid for sid, name in SOURCES.items()}

# Standard InChIKey: 14 letters, hyphen, 8 letters + S/N + version letter,
# hyphen, protonation letter.
INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{8}[SN][A-Z]-[A-Z]$")

_ROUNDS = 2
_HEDGE_DELAY = 1.5
_TIMEOUT = 8.0

Mapping = dict[int, list[str]]


def normalize_inchikey(value: str) -> str:
    key = value.strip()
    if key.upper().startswith("INCHIKEY="):
        key = key[len("InChIKey="):]
    key = key.upper()
    if not INCHIKEY_RE.match(key):
        raise ValueError(f"not a standard InChIKey: {value!r}")
    return key


def source_id(source: int | str) -> int:
    """A UniChem source id from its number or one of the names in
    SOURCES ("chembl", "pubchem", ...)."""
    if isinstance(source, int):
        return source
    try:
        return SOURCE_IDS[source.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown UniChem source {source!r}; known names: {sorted(SOURCE_IDS)}") from None


def sources_for_inchikey(inchi_key: str) -> Mapping | None:
    """Every source's ids for a standard InChIKey, keyed by UniChem
    source id. None if UniChem does not know the key."""
    key = normalize_inchikey(inchi_key)
    return _lookup(("inchikey", key), lambda legacy: _call_inchikey(key, legacy))


def sources_for_compound(source: int | str, identifier: str | int) -> Mapping | None:
    """Every source's ids for a compound named by another source's id
    (`sources_for_compound("chembl", "CHEMBL25")`,
    `sources_for_compound("pubchem", 2244)`). None if unknown."""
    sid = source_id(source)
    ident = str(identifier).strip()
    return _lookup(("source", sid, ident), lambda _legacy: _call_source(sid, ident))


def _lookup(key_parts: tuple[Any, ...], call: Any) -> Mapping | None:
    cache_key = cache.key("unichem", *key_parts)
    cached = cache.get(cache_key)
    if cached is not None:
        if cached == {"__miss__": True}:
            return None
        return {int(k): list(v) for k, v in cached.items()}
    errors: list[str] = []
    for round_ in range(_ROUNDS):
        result, found = _hedged(call, errors)
        if found:
            cache.put(cache_key, result if result is not None else {"__miss__": True})
            return result
        if round_ < _ROUNDS - 1:
            warnings.warn(f"UniChem lookup {key_parts} failed ({'; '.join(errors)[:120]}); retrying", stacklevel=4)
            time.sleep(1.0)
    raise _client.SureChEMBLError(
        f"UniChem did not answer for {key_parts} after {_ROUNDS} hedged rounds: {'; '.join(errors)}"
    )


# (mapping or None-for-miss, answered?, reason)
_Outcome = tuple[Mapping | None, bool, str]


def _hedged(call: Any, errors: list[str]) -> tuple[Mapping | None, bool]:
    """One hedged round; returns (mapping-or-None, answered)."""
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first: Future[_Outcome] = executor.submit(call, True)
        done, _ = wait([first], timeout=_HEDGE_DELAY)
        pending: set[Future[_Outcome]] = set()
        if first in done:
            mapping, answered, reason = first.result()
            if answered:
                return mapping, True
            errors.append(reason)
        else:
            pending.add(first)
        pending.add(executor.submit(call, False))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                mapping, answered, reason = future.result()
                if answered:
                    return mapping, True
                errors.append(reason)
        return None, False
    finally:
        executor.shutdown(wait=False)


def _request(method: str, url: str, **kwargs: Any) -> requests.Response | str:
    session = _client._get_session()
    _client._limiter.acquire()
    try:
        return session.request(method, url, timeout=(_client._CONNECT_TIMEOUT, _TIMEOUT), **kwargs)
    except requests.RequestException as exc:
        return type(exc).__name__


def _call_inchikey(key: str, legacy: bool) -> _Outcome:
    label = "legacy" if legacy else "v1"
    if legacy:
        response = _request("GET", f"{LEGACY_INCHIKEY_URL}{key}")
    else:
        response = _request("POST", V1_URL, json={"type": "inchikey", "compound": key})
    if isinstance(response, str):
        return None, False, f"{label} {response}"
    if response.status_code >= 500:
        return None, False, f"{label} HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, False, f"{label} HTTP {response.status_code}: not JSON"
    if legacy:
        # [{"src_id": "15", "src_compound_id": "1353"}, ...] or, for a
        # miss, {"error": "InChIkey '...' not found."} (both verified live).
        if isinstance(body, dict):
            return (None, True, "") if "error" in body else (None, False, f"{label} unexpected object")
        if not isinstance(body, list):
            return None, False, f"{label} unexpected body"
        mapping: Mapping = {}
        for entry in body:
            try:
                mapping.setdefault(int(entry["src_id"]), []).append(str(entry["src_compound_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return _sorted(mapping), True, ""
    return _parse_v1(body, label)


def _call_source(sid: int, identifier: str) -> _Outcome:
    response = _request("POST", V1_URL, json={"type": "sourceID", "compound": identifier, "sourceID": sid})
    if isinstance(response, str):
        return None, False, f"v1 {response}"
    if response.status_code >= 500:
        return None, False, f"v1 HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, False, f"v1 HTTP {response.status_code}: not JSON"
    return _parse_v1(body, "v1")


def _parse_v1(body: Any, label: str) -> _Outcome:
    # {"compounds": [{"sources": [{"id": 15, "compoundId": "1353"}, ...]}, ...],
    #  "notFound": [...], "response": "Not found"} -- an empty compounds
    # list is a genuine miss (verified live).
    if not isinstance(body, dict) or "compounds" not in body:
        return None, False, f"{label} unexpected body"
    compounds = body.get("compounds") or []
    if not compounds:
        return None, True, ""
    mapping: Mapping = {}
    for entry in compounds:
        for source in entry.get("sources") or []:
            try:
                mapping.setdefault(int(source["id"]), []).append(str(source["compoundId"]))
            except (KeyError, TypeError, ValueError):
                continue
    return _sorted(mapping), True, ""


def _sorted(mapping: Mapping) -> Mapping:
    def _key(value: str) -> tuple[int, str]:
        return (0, f"{int(value):020d}") if value.isdigit() else (1, value)

    return {sid: sorted(set(ids), key=_key) for sid, ids in mapping.items()}
