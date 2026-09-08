"""Compound lookup: by SureChEMBL id, by name, by SMILES, and by InChIKey.

Three of the four are direct REST calls. InChIKey is not: SureChEMBL has
no InChIKey endpoint at all (its own FAQ says to go through UniChem), so
`by_inchikey()` asks UniChem for the key's SureChEMBL source entries and
then fetches those ids. That two-hop is the reason the function exists.
"""

from __future__ import annotations

import re
import time
import warnings
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import requests

from . import _client, cache
from ._ids import compound_id, compound_ids
from .models import Compound

# SureChEMBL's source id in UniChem's registry (verified live 2026-09-08:
# the aspirin InChIKey resolves to two entries under this source,
# compoundId 1353 and 29350479, name "surechembl").
UNICHEM_SURECHEMBL_SOURCE = 15
_UNICHEM_V1_URL = "https://www.ebi.ac.uk/unichem/api/v1/compounds"
_UNICHEM_LEGACY_URL = "https://www.ebi.ac.uk/unichem/rest/inchikey/"

# Standard InChIKey: 14 letters, hyphen, 8 letters + S/N (standard flag) +
# version letter, hyphen, protonation letter.
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{8}[SN][A-Z]-[A-Z]$")

# UniChem hangs (2026-09-08, measured): 3 of 16 legacy GETs and 2 of 16 v1
# POSTs for a valid key never answered within 20 s, and a v1 request that
# does answer late answers as an HTML 500 after ~32 s; across 30 keys, 8
# lookups hit at least one hang. A request that does answer, answers in
# under a second (median 0.28 s). Neither endpoint is reliably better.
#
# So a lookup is hedged: the legacy GET goes first, and if it has not
# answered within _UNICHEM_HEDGE_DELAY the v1 POST is sent too, and the
# first good answer wins (the other is abandoned to its timeout in the
# background). The delay keeps the second request off UniChem in the
# common case, so this does not double the load on EBI's service; it only
# pays for the ~15% of requests that would otherwise wait out a hang.
# Two hedged rounds bound the worst case at roughly 2 x the read timeout.
_UNICHEM_ROUNDS = 2
_UNICHEM_HEDGE_DELAY = 1.5
_UNICHEM_TIMEOUT = 8.0

# Verified live 2026-09-08: 1,000 ids in one POST works; 2,000 drops the
# connection. 500 leaves headroom for ids that are longer than the
# 5-6 digit ones tested.
_BATCH_SIZE = 500


def compound(id: int | str) -> Compound | None:
    """One compound by id (`1353` or `"SCHEMBL1353"`), or None if the id
    is not in SureChEMBL. Carries the full property set (formula, logP,
    HBD/HBA, PSA, QED, Lipinski counts, global_frequency)."""
    data = _client.request("GET", f"/chemical/id/{compound_id(id)}")
    records = data if isinstance(data, list) else []
    return Compound.from_api(records[0]) if records else None


def compounds(ids: Iterable[int | str]) -> dict[int, Compound]:
    """Many compounds by id in batched POSTs, returned as a dict keyed by
    integer id. An id that is not in SureChEMBL is simply absent (the API
    returns what it found and nothing for the rest; a probe of ids
    1000-1299 returned 295 records)."""
    wanted = compound_ids(ids)
    found: dict[int, Compound] = {}
    for start in range(0, len(wanted), _BATCH_SIZE):
        chunk = wanted[start : start + _BATCH_SIZE]
        data = _client.request("POST", "/chemical/id", data={"ids": ",".join(map(str, chunk))})
        for record in data if isinstance(data, list) else []:
            c = Compound.from_api(record)
            found[c.id] = c
    return found


def by_name(name: str) -> list[Compound]:
    """Compounds matching a chemical name (SureChEMBL's name index: IUPAC,
    trivial and trade names it extracted from patents). Several ids can
    share a name; an unknown name returns []. The name endpoint returns
    only structure and weight, not the property set `compound()` gets."""
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    if "/" in name:
        # Tomcat rejects a percent-encoded slash in the path with an HTML
        # 400, and the name endpoint has no query-parameter form (verified
        # live 2026-09-08 with "cis/trans-stilbene"), so such a name cannot
        # be looked up at all rather than silently missing.
        raise ValueError(f"SureChEMBL's name endpoint cannot accept a name containing '/': {name!r}")
    try:
        data = _client.request("GET", f"/chemical/name/{_path_segment(name)}")
    except _client.SureChEMBLError as exc:
        # Verified live: an unknown name is HTTP 400, status ERROR,
        # "There was an error retrieving chemical data: <name>".
        if exc.http_status == 400 and exc.api_status == "ERROR":
            return []
        raise
    return [Compound.from_api(r) for r in data] if isinstance(data, list) else []


def by_smiles(smiles: str) -> Compound | None:
    """The compound whose canonical structure matches a SMILES, or None.

    Sent as a form field rather than in the URL path: a SMILES with
    stereo bonds (`C/C=C/C(=O)O`) contains slashes, and the path form of
    this endpoint rejects a percent-encoded slash with a 400 (verified
    live). None also comes back for an unparseable SMILES: the API
    answers both "not in SureChEMBL" and "not a SMILES" with an empty
    object, so validate the input first if that distinction matters.
    """
    data = _client.request("POST", "/chemical/smiles/", data={"smiles": smiles})
    if not isinstance(data, dict) or not data:
        return None
    record = next(iter(data.values()))
    return Compound.from_api(record) if isinstance(record, dict) else None


def by_inchikey(inchi_key: str) -> list[Compound]:
    """Compounds carrying a standard InChIKey, resolved through UniChem.

    Returns a list because SureChEMBL does hold more than one id for a
    single InChIKey (aspirin is both 1353 and 29350479; the bulk
    parquet confirms both rows). Each hop is cached and paced like any
    other request. An unknown key returns []."""
    key = inchi_key.strip()
    if key.upper().startswith("INCHIKEY="):
        key = key[len("InChIKey="):]
    key = key.upper()
    if not _INCHIKEY_RE.match(key):
        # Validated before any network call: UniChem answers a malformed
        # key with the same slow path as a real miss, so a typo would
        # otherwise cost up to a minute of retries to return [].
        raise ValueError(f"not a standard InChIKey: {inchi_key!r}")
    ids = _unichem_surechembl_ids(key)
    if not ids:
        return []
    found = compounds(ids)
    return [found[i] for i in ids if i in found]


def _unichem_surechembl_ids(inchi_key: str) -> list[int]:
    cache_key = cache.key("unichem", inchi_key)
    cached = cache.get(cache_key)
    if cached is not None:
        return [int(i) for i in cached]
    errors: list[str] = []
    for round_ in range(_UNICHEM_ROUNDS):
        ids = _unichem_hedged(inchi_key, errors)
        if ids is not None:
            cache.put(cache_key, ids)
            return ids
        if round_ < _UNICHEM_ROUNDS - 1:
            warnings.warn(
                f"UniChem lookup for {inchi_key} failed ({'; '.join(errors)[:120]}); retrying",
                stacklevel=3,
            )
            time.sleep(1.0)
    raise _client.SureChEMBLError(
        f"UniChem did not answer for {inchi_key} after {_UNICHEM_ROUNDS} hedged rounds: {'; '.join(errors)}"
    )


def _unichem_call(inchi_key: str, legacy: bool) -> tuple[list[int] | None, str]:
    """One UniChem request; (ids, "") on success, (None, reason) otherwise."""
    session = _client._get_session()
    _client._limiter.acquire()
    try:
        if legacy:
            response = session.get(
                f"{_UNICHEM_LEGACY_URL}{inchi_key}", timeout=(_client._CONNECT_TIMEOUT, _UNICHEM_TIMEOUT)
            )
        else:
            # UniChem's sourceID filter parameter 500s (verified live
            # 2026-09-08), so ask for every source and filter here.
            response = session.post(
                _UNICHEM_V1_URL,
                json={"type": "inchikey", "compound": inchi_key},
                timeout=(_client._CONNECT_TIMEOUT, _UNICHEM_TIMEOUT),
            )
    except requests.RequestException as exc:
        return None, f"{'legacy' if legacy else 'v1'} {type(exc).__name__}"
    if response.status_code >= 500:
        return None, f"{'legacy' if legacy else 'v1'} HTTP {response.status_code}"
    ids = _parse_unichem(response, legacy)
    if ids is None:
        return None, f"{'legacy' if legacy else 'v1'} HTTP {response.status_code}: {response.text[:80]!r}"
    return ids, ""


def _unichem_hedged(inchi_key: str, errors: list[str]) -> list[int] | None:
    """One hedged round: legacy first, v1 after _UNICHEM_HEDGE_DELAY if
    the first has not answered, first good answer wins. The executor is
    shut down without waiting so an abandoned hung request times out on
    its own thread instead of holding the caller."""
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first = executor.submit(_unichem_call, inchi_key, True)
        done, _ = wait([first], timeout=_UNICHEM_HEDGE_DELAY)
        pending: set[Future[tuple[list[int] | None, str]]] = set()
        if first in done:
            ids, reason = first.result()
            if ids is not None:
                return ids
            errors.append(reason)
        else:
            pending.add(first)
        pending.add(executor.submit(_unichem_call, inchi_key, False))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                ids, reason = future.result()
                if ids is not None:
                    return ids
                errors.append(reason)
        return None
    finally:
        executor.shutdown(wait=False)


def _parse_unichem(response: requests.Response, legacy: bool) -> list[int] | None:
    """SureChEMBL ids from either UniChem response shape, [] for a miss,
    None if the body is not the expected JSON (treated as a failed
    attempt by the caller)."""
    try:
        body = response.json()
    except ValueError:
        return None
    ids: list[int] = []
    if legacy:
        # [{"src_id": "15", "src_compound_id": "1353"}, ...] or, for a
        # miss, {"error": "InChIkey '...' not found."} (both verified live).
        if isinstance(body, dict):
            return [] if "error" in body else None
        if not isinstance(body, list):
            return None
        for entry in body:
            if str(entry.get("src_id")) == str(UNICHEM_SURECHEMBL_SOURCE):
                try:
                    ids.append(compound_id(str(entry.get("src_compound_id"))))
                except ValueError:
                    continue
    else:
        # {"compounds": [{"sources": [{"id": 15, "compoundId": "1353"}, ...]}], ...}
        if not isinstance(body, dict) or "compounds" not in body:
            return None
        for entry in body.get("compounds") or []:
            for source in entry.get("sources") or []:
                if source.get("id") == UNICHEM_SURECHEMBL_SOURCE:
                    try:
                        ids.append(compound_id(str(source.get("compoundId"))))
                    except ValueError:
                        continue
    return sorted(set(ids))


def structure_image(structure: str, width: int = 300, height: int = 300, highlight: str | None = None) -> bytes:
    """A PNG rendering of a SMILES from SureChEMBL's depiction service,
    optionally with a substructure highlighted. Returns the PNG bytes;
    write them to a file or hand them to IPython.display.Image."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    params: dict[str, Any] = {"structure": structure, "width": width, "height": height}
    if highlight:
        params["highlight_structure"] = highlight
    png = _client.request_bytes("GET", "/service/chemical/image", params=params)
    if not png.startswith(b"\x89PNG"):
        # An unparseable structure comes back as a 200 with an empty
        # body, and some bad inputs as a 200 whose body is the text
        # "Internal Server Error" (both verified live 2026-09-08).
        raise _client.SureChEMBLError(
            f"SureChEMBL returned no depiction for {structure!r}: {png[:60]!r}"
        )
    return png


def _path_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
