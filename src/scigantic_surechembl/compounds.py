"""Compound lookup: by SureChEMBL id, by name, by SMILES, and by InChIKey.

Only the id lookup is a direct REST call since SureChEMBL's late-2026 API
change. The new API has no name endpoint and no SMILES endpoint, so:

- `by_smiles()` runs an "identical" structure search and fetches the
  hit by id (two requests where there used to be one).
- `by_name()` resolves the name through PubChem (scigantic-pubchem, the
  `names` extra) to an InChIKey, then through UniChem to SureChEMBL ids.
- `by_inchikey()` was always a UniChem two-hop (SureChEMBL has never had
  an InChIKey endpoint; its own FAQ says to go through UniChem).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import _client, _unichem
from ._ids import compound_id, compound_ids
from .models import Compound

# Verified live 2026-11-09: GET /chemical/id takes 1,000 ids in one query
# string (about 8 KB of URL) in under a second. 500 leaves headroom for
# ids longer than the 5-digit ones tested.
_BATCH_SIZE = 500


def compound(id: int | str) -> Compound | None:
    """One compound by id (`1353` or `"SCHEMBL1353"`), or None if the id
    is not in SureChEMBL. Carries the full property set (formula, logP,
    HBD/HBA, PSA, QED, Lipinski counts, global_frequency).

    The API takes bare integers only: `ids=SCHEMBL1353` is a 404 "Chemical
    not found" (verified live), so the prefix is stripped here."""
    found = compounds([id])
    return next(iter(found.values()), None)


def compounds(ids: Iterable[int | str]) -> dict[int, Compound]:
    """Many compounds by id in batched requests, returned as a dict keyed
    by integer id. An id that is not in SureChEMBL is simply absent: the
    API returns what it found (a probe of ids 1000-1299 returned 295
    records), and a batch in which no id is known is a 404, read here as
    an empty batch."""
    wanted = compound_ids(ids)
    found: dict[int, Compound] = {}
    for start in range(0, len(wanted), _BATCH_SIZE):
        chunk = wanted[start : start + _BATCH_SIZE]
        try:
            data = _client.request("GET", "/chemical/id", params={"ids": ",".join(map(str, chunk))})
        except _client.SureChEMBLError as exc:
            if exc.http_status == 404:
                continue
            raise
        for record in data if isinstance(data, list) else []:
            c = Compound.from_api(record)
            found[c.id] = c
    return found


def by_name(name: str) -> list[Compound]:
    """SureChEMBL compounds for a chemical name, resolved through PubChem.

    SureChEMBL's own name endpoint (`/chemical/name/{name}`, which
    searched the names it extracted from patents) was removed in the
    late-2026 API change and has no replacement. This resolves the name
    with PubChem (scigantic-pubchem's `resolve()`, PubChem's best match
    for the name), takes that compound's standard InChIKey, and returns
    every SureChEMBL id UniChem holds for it, as `by_inchikey()` does.

    So the answer is now "the structure PubChem means by this name", not
    "every SureChEMBL record carrying this name": a name PubChem does not
    know returns [], even if a patent uses it. Needs scigantic-pubchem
    (`pip install "scigantic-surechembl[names]"`); without it this raises
    SureChEMBLError rather than guessing."""
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    try:
        import scigantic_pubchem
    except ImportError as exc:
        raise _client.SureChEMBLError(
            "by_name() needs scigantic-pubchem: SureChEMBL removed its name endpoint "
            "(/chemical/name) in its late-2026 API change, so names are resolved through "
            'PubChem now. Install it with: pip install "scigantic-surechembl[names]"'
        ) from exc
    try:
        resolved = scigantic_pubchem.resolve(name, namespace="name")
    except Exception as exc:  # scigantic-pubchem's own error type; surfaced, not swallowed
        raise _client.SureChEMBLError(f"PubChem name lookup failed for {name!r}: {exc}") from exc
    key = getattr(resolved, "inchi_key", None) if resolved is not None else None
    if not key:
        return []
    return by_inchikey(str(key))


def by_smiles(smiles: str) -> Compound | None:
    """The compound whose structure is identical to a SMILES, or None.

    SureChEMBL removed its SMILES lookup endpoint in its late-2026 API
    change, so this runs an "identical" structure search (every feature
    must match, stereo included) and fetches the first hit. Where
    SureChEMBL holds the structure under more than one id (aspirin is
    1353 and 29350479), that is the id the search ranks first; use
    `structure_search(smiles, "identical")` for all of them.

    None for a structure SureChEMBL does not hold, and also for an
    unparseable SMILES (the search answers that with a 422, read here as
    "no compound"), matching the 0.3 behavior.
    """
    from .search import _structure_ids

    try:
        ids = _structure_ids(smiles, "identical", max_results=1)
    except ValueError:
        return None
    if not ids:
        return None
    return compound(ids[0][0])


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
    """A PNG rendering of a SMILES from SureChEMBL's depiction service.
    Returns the PNG bytes; write them to a file or hand them to
    IPython.display.Image.

    `highlight` (a substructure to highlight) is no longer supported: the
    late-2026 API's `POST /chemical/image` takes only the SMILES, width
    and height, so passing it raises SureChEMBLError rather than
    returning an unhighlighted picture."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if highlight:
        raise _client.SureChEMBLError(
            "structure_image(highlight=...) is no longer supported: SureChEMBL's late-2026 image "
            "endpoint (POST /chemical/image) has no highlight option"
        )
    body: dict[str, Any] = {"structure_smiles": structure, "width": width, "height": height}
    png = _client.request_bytes("POST", "/chemical/image", json_body=body)
    if not png.startswith(b"\x89PNG"):
        # An unparseable SMILES is a 400 (raised by request_bytes); this
        # guards any other 2xx that is not a picture.
        raise _client.SureChEMBLError(f"SureChEMBL returned no depiction for {structure!r}: {png[:60]!r}")
    return png
