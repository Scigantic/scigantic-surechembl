"""Compound lookup: by SureChEMBL id, by name, by SMILES, and by InChIKey.

Three of the four are direct REST calls. InChIKey is not: SureChEMBL has
no InChIKey endpoint at all (its own FAQ says to go through UniChem), so
`by_inchikey()` asks UniChem for the key's SureChEMBL source entries and
then fetches those ids. That two-hop is the reason the function exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import _client, _unichem
from ._ids import compound_id, compound_ids
from .models import Compound

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
    """Compounds carrying a standard InChIKey, resolved through UniChem
    (SureChEMBL has no InChIKey endpoint; its FAQ points at UniChem).

    Returns a list because SureChEMBL does hold more than one id for a
    single InChIKey (aspirin is both 1353 and 29350479; the bulk
    parquet confirms both rows). An unknown key returns []; a malformed
    one raises ValueError before any request, since UniChem answers a
    malformed key with the same slow path as a real miss. The UniChem
    hop is hedged across two endpoints (see _unichem)."""
    ids = surechembl_ids_for_inchikey(inchi_key)
    if not ids:
        return []
    found = compounds(ids)
    return [found[i] for i in ids if i in found]


def surechembl_ids_for_inchikey(inchi_key: str) -> list[int]:
    """The SureChEMBL ids UniChem holds for a standard InChIKey, without
    fetching the compounds."""
    mapping = _unichem.sources_for_inchikey(inchi_key)
    if not mapping:
        return []
    out: list[int] = []
    for value in mapping.get(_unichem.SOURCE_IDS["surechembl"], []):
        try:
            out.append(compound_id(value))
        except ValueError:
            continue
    return out


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
