"""Cross-references between SureChEMBL and the other public chemistry
identifiers, and the patent landscape of a compound you hold by one of
them. All through UniChem (EMBL-EBI), no extra dependencies.

The join that motivates this: "which patents mention this ChEMBL / PubChem
compound?" SureChEMBL is keyed by its own ids, and a structure can sit
under more than one of them (aspirin: 1353 and 29350479), so the honest
answer is the union of `patents_for_compound()` over every SureChEMBL id
UniChem maps the identifier to. `patents_for_chembl()`,
`patents_for_pubchem_cid()` and `patents_for_inchikey()` do exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import _unichem
from ._ids import compound_id
from .compounds import compound as _compound
from .models import Compound, PatentHit
from .search import patents_for_compound


@dataclass(frozen=True)
class Xrefs:
    """Every identifier UniChem holds for one structure (one standard
    InChIKey). The named fields are the sources this package's siblings
    read; `sources` has all of them keyed by UniChem source id (see
    `_unichem.SOURCES` for the names of the common ones)."""

    inchi_key: str
    surechembl: list[int] = field(default_factory=list)
    chembl: list[str] = field(default_factory=list)
    pubchem_cid: list[int] = field(default_factory=list)
    drugbank: list[str] = field(default_factory=list)
    chebi: list[str] = field(default_factory=list)
    pdb_ligand: list[str] = field(default_factory=list)
    bindingdb: list[str] = field(default_factory=list)
    unii: list[str] = field(default_factory=list)
    sources: dict[int, list[str]] = field(default_factory=dict, repr=False)

    @property
    def schembl_ids(self) -> list[str]:
        return [f"SCHEMBL{i}" for i in self.surechembl]

    @classmethod
    def from_mapping(cls, inchi_key: str, mapping: _unichem.Mapping) -> Xrefs:
        def ids(name: str) -> list[str]:
            return list(mapping.get(_unichem.SOURCE_IDS[name], []))

        def ints(name: str) -> list[int]:
            out: list[int] = []
            for v in ids(name):
                try:
                    out.append(int(v))
                except ValueError:
                    continue
            return out

        return cls(
            inchi_key=inchi_key,
            surechembl=ints("surechembl"),
            chembl=ids("chembl"),
            pubchem_cid=ints("pubchem"),
            drugbank=ids("drugbank"),
            chebi=ids("chebi"),
            pdb_ligand=ids("pdb"),
            bindingdb=ids("bindingdb"),
            unii=ids("fdasrs"),
            sources=mapping,
        )


def xrefs(compound: int | str | Compound) -> Xrefs | None:
    """Cross-references for a SureChEMBL compound (by id, `SCHEMBL` id,
    or a Compound already fetched). None if the compound is unknown or
    UniChem has no entry for its InChIKey (UniChem loads SureChEMBL, so
    that is rare, but not every id is there yet)."""
    c = compound if isinstance(compound, Compound) else _compound(compound_id(compound))
    if c is None or not c.inchi_key:
        return None
    return xrefs_for_inchikey(c.inchi_key)


def xrefs_for_inchikey(inchi_key: str) -> Xrefs | None:
    """Cross-references for any standard InChIKey. None if UniChem does
    not know it."""
    key = _unichem.normalize_inchikey(inchi_key)
    mapping = _unichem.sources_for_inchikey(key)
    return None if mapping is None else Xrefs.from_mapping(key, mapping)


def xrefs_for(source: int | str, identifier: str | int) -> Xrefs | None:
    """Cross-references starting from another source's id:
    `xrefs_for("chembl", "CHEMBL25")`, `xrefs_for("pubchem", 2244)`,
    `xrefs_for("drugbank", "DB00945")`. None if UniChem does not know it.
    The InChIKey comes back on the result; UniChem's v1 response carries
    the standard InChI but not the key, so it is taken from the
    SureChEMBL compound when there is one and left empty otherwise."""
    mapping = _unichem.sources_for_compound(source, identifier)
    if mapping is None:
        return None
    key = ""
    for value in mapping.get(_unichem.SOURCE_IDS["surechembl"], []):
        c = _compound(compound_id(value))
        if c is not None and c.inchi_key:
            key = c.inchi_key
            break
    return Xrefs.from_mapping(key, mapping)


def surechembl_ids_for(source: int | str, identifier: str | int) -> list[int]:
    """SureChEMBL ids for another source's compound id, e.g.
    `surechembl_ids_for("chembl", "CHEMBL25")` -> [1353, 29350479]."""
    mapping = _unichem.sources_for_compound(source, identifier)
    if not mapping:
        return []
    out: list[int] = []
    for value in mapping.get(_unichem.SOURCE_IDS["surechembl"], []):
        try:
            out.append(compound_id(value))
        except ValueError:
            continue
    return out


def patents_for_chembl(chembl_id: str, max_results: int = 100) -> list[PatentHit]:
    """Patents mentioning a ChEMBL compound: the union over every
    SureChEMBL id UniChem maps the ChEMBL id to, merged on doc_id.
    `xrefs_for("chembl", id).surechembl` shows which ids that was."""
    return _union_patents(surechembl_ids_for("chembl", chembl_id), max_results)


def patents_for_pubchem_cid(cid: int, max_results: int = 100) -> list[PatentHit]:
    """Patents mentioning a PubChem compound, same shape as
    patents_for_chembl()."""
    return _union_patents(surechembl_ids_for("pubchem", int(cid)), max_results)


def patents_for_inchikey(inchi_key: str, max_results: int = 100) -> list[PatentHit]:
    """Patents mentioning a structure given by standard InChIKey."""
    from .compounds import surechembl_ids_for_inchikey

    return _union_patents(surechembl_ids_for_inchikey(inchi_key), max_results)


def _union_patents(ids: list[int], max_results: int) -> list[PatentHit]:
    """Union of per-id patent searches. Per id because a many-id call
    to SureChEMBL is an intersection (see search.patents_for_compound).
    Each id is asked for up to max_results, so with several ids the
    result is complete only when every id had fewer hits than that;
    a caller wanting everything for a common compound should raise
    max_results or count first."""
    if not ids or max_results <= 0:
        return []
    merged: list[PatentHit] = []
    seen: set[str] = set()
    for sid in ids:
        for hit in patents_for_compound(sid, max_results=max_results):
            if hit.doc_id not in seen:
                seen.add(hit.doc_id)
                merged.append(hit)
    return merged[:max_results]
