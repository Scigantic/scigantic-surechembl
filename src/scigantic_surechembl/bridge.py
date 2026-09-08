"""Joins into the sibling packages: scigantic-chembl (ChEMBL bioactivity
from its public S3 mirror), scigantic-bindingdb (BindingDB affinities,
including the ones BindingDB curated from patents), and scigantic-pubchem
(live PubChem).

Needs the `bridge` extra: `pip install "scigantic-surechembl[bridge]"`.
Nothing else in this package imports these.

The joins are on structure, not on UniChem: the ChEMBL mirror's
`compound_structures.standard_inchi_key` and BindingDB's
`measurements.ligand_inchi_key` are joined directly against SureChEMBL's
InChIKeys, so a patent's whole compound list is one DuckDB query, not one
UniChem round trip per compound. UniChem is used only where an identifier
is needed rather than a structure (`pubchem_compound()`).

BindingDB also carries a `patent_number` column: 1.34 million of its
measurements (curation_source "US Patent", 8,886 patents in release
202608) were extracted from patent SAR tables. That is the other
direction of the join: `bindingdb_measurements_for_patent()` gives the
affinities BindingDB curated from the very document SureChEMBL extracted
the structures from.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "scigantic_surechembl.bridge needs pandas, duckdb and the sibling packages: "
        "pip install 'scigantic-surechembl[bridge]'"
    ) from exc

from ._ids import compound_id, patent_number
from .compounds import compound as _compound
from .models import Compound
from .patents import patent_chemistry
from .xrefs import xrefs

if TYPE_CHECKING:
    import duckdb

_chembl_connections: dict[str, Any] = {}
_bindingdb_connections: dict[str, Any] = {}
_lock = threading.Lock()


def _need(module: str, package: str) -> Any:
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            f"this needs {package}: pip install 'scigantic-surechembl[bridge]' (or pip install {package})"
        ) from exc


def _chembl_cursor(release: str | None) -> tuple[duckdb.DuckDBPyConnection, str]:
    """A cursor on one shared scigantic-chembl connection per release
    (its connect() registers the S3 secret and one view per table; a
    cursor shares those, verified live, and is thread-safe where the
    connection itself is not)."""
    chembl = _need("scigantic_chembl", "scigantic-chembl")
    release = release or str(chembl.latest())
    with _lock:
        con = _chembl_connections.get(release)
        if con is None:
            con = chembl.connect(release)
            _chembl_connections[release] = con
    return con.cursor(), release


def _bindingdb_cursor(release: str | None) -> tuple[duckdb.DuckDBPyConnection, str]:
    bindingdb = _need("scigantic_bindingdb", "scigantic-bindingdb")
    release = release or str(bindingdb.latest())
    with _lock:
        con = _bindingdb_connections.get(release)
        if con is None:
            con = bindingdb.connect(release)
            _bindingdb_connections[release] = con
    return con.cursor(), release


def _resolve(compound: int | str | Compound) -> Compound | None:
    return compound if isinstance(compound, Compound) else _compound(compound_id(compound))


# --- ChEMBL --------------------------------------------------------------


def chembl_compound(compound: int | str | Compound, chembl_release: str | None = None) -> dict[str, Any] | None:
    """The ChEMBL record for a SureChEMBL compound, matched on standard
    InChIKey against scigantic-chembl's mirror: chembl_id, pref_name,
    max_phase (4.0 = approved), molregno, molecule_type. None if the
    compound is unknown or not in ChEMBL."""
    c = _resolve(compound)
    if c is None or not c.inchi_key:
        return None
    cur, release = _chembl_cursor(chembl_release)
    try:
        row = cur.execute(
            "SELECT m.chembl_id, m.pref_name, m.max_phase, m.molregno, m.molecule_type "
            "FROM compound_structures s JOIN molecule_dictionary m USING (molregno) "
            "WHERE s.standard_inchi_key = ?",
            [c.inchi_key],
        ).fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    return {
        "chembl_id": row[0],
        "pref_name": row[1],
        "max_phase": row[2],
        "molregno": row[3],
        "molecule_type": row[4],
        "chembl_release": release,
        "surechembl_id": c.id,
        "inchi_key": c.inchi_key,
    }


def chembl_activities(
    compound: int | str | Compound, limit: int | None = None, chembl_release: str | None = None
) -> pd.DataFrame:
    """Every ChEMBL activity for a SureChEMBL compound, joined to its
    assay and target: standard_type/relation/value/units, pchembl_value,
    target pref_name and organism, assay description and chembl ids.
    Most potent (highest pchembl) first. Empty frame if not in ChEMBL."""
    match = chembl_compound(compound, chembl_release)
    if match is None:
        return pd.DataFrame()
    cur, _ = _chembl_cursor(match["chembl_release"])
    try:
        sql = (
            "SELECT a.activity_id, a.standard_type, a.standard_relation, a.standard_value, a.standard_units, "
            "a.pchembl_value, t.pref_name AS target_name, t.organism AS target_organism, "
            "t.chembl_id AS target_chembl_id, s.chembl_id AS assay_chembl_id, s.description AS assay_description, "
            "s.confidence_score "
            "FROM activities a JOIN assays s USING (assay_id) LEFT JOIN target_dictionary t USING (tid) "
            "WHERE a.molregno = ? ORDER BY a.pchembl_value DESC NULLS LAST"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        frame: pd.DataFrame = cur.execute(sql, [match["molregno"]]).df()
        return frame
    finally:
        cur.close()


def chembl_matches_for_patent(doc_id: str, chembl_release: str | None = None) -> pd.DataFrame:
    """Every compound SureChEMBL extracted from a patent, with what ChEMBL
    knows about it: one row per compound, with chembl_id, pref_name,
    max_phase, molecule_type, n_activities and best_pchembl (NULL where
    ChEMBL has no such structure). Approved and well-measured compounds
    first. One DuckDB query over the mirror for the whole list (4,068
    compounds of one patent in a few seconds), not a lookup per compound.

    This is "how much of this patent's chemistry is known bioactive
    matter": a patent whose compounds are mostly in ChEMBL with
    activities is a different document from one whose are not.
    """
    compounds = patent_chemistry(doc_id)
    base = pd.DataFrame(
        {
            "compound_id": [c.id for c in compounds],
            "schembl_id": [c.schembl_id for c in compounds],
            "inchi_key": [c.inchi_key for c in compounds],
            "smiles": [c.smiles for c in compounds],
            "name": [c.name for c in compounds],
        }
    )
    if base.empty:
        return base
    cur, _ = _chembl_cursor(chembl_release)
    try:
        cur.register("_sc_patent_keys", base[["inchi_key"]].dropna().drop_duplicates())
        matched = cur.execute(
            "SELECT k.inchi_key, m.chembl_id, m.pref_name, m.max_phase, m.molecule_type, m.molregno "
            "FROM _sc_patent_keys k JOIN compound_structures s ON s.standard_inchi_key = k.inchi_key "
            "JOIN molecule_dictionary m USING (molregno)"
        ).df()
        if not matched.empty:
            cur.register("_sc_patent_mols", matched[["molregno"]])
            acts = cur.execute(
                "SELECT a.molregno, count(*) AS n_activities, max(a.pchembl_value) AS best_pchembl "
                "FROM activities a JOIN _sc_patent_mols p USING (molregno) GROUP BY 1"
            ).df()
            matched = matched.merge(acts, on="molregno", how="left")
            matched["n_activities"] = matched["n_activities"].fillna(0).astype(int)
    finally:
        cur.close()
    out = base.merge(matched, on="inchi_key", how="left") if not matched.empty else base.assign(
        chembl_id=None, pref_name=None, max_phase=None, molecule_type=None, molregno=None, n_activities=0, best_pchembl=None
    )
    out["n_activities"] = out["n_activities"].fillna(0).astype(int)
    return out.sort_values(["max_phase", "n_activities"], ascending=[False, False], na_position="last").reset_index(
        drop=True
    )


# --- BindingDB -----------------------------------------------------------


def bindingdb_measurements(compound: int | str | Compound, bindingdb_release: str | None = None) -> pd.DataFrame:
    """BindingDB affinity measurements for a SureChEMBL compound, matched
    on standard InChIKey against scigantic-bindingdb's mirror: target,
    organism, Ki/IC50/Kd/EC50 in nM with qualifiers, curation source
    and, where BindingDB took the value from a patent, the patent
    number. Empty frame if none."""
    c = _resolve(compound)
    if c is None or not c.inchi_key:
        return pd.DataFrame()
    cur, _ = _bindingdb_cursor(bindingdb_release)
    try:
        frame: pd.DataFrame = cur.execute(
            "SELECT reactant_set_id, ligand_name, target_name, target_source_organism, "
            "ki_nm_value, ki_nm_qualifier, ic50_nm_value, ic50_nm_qualifier, kd_nm_value, kd_nm_qualifier, "
            "ec50_nm_value, ec50_nm_qualifier, curation_source, patent_number, pmid, article_doi "
            "FROM measurements WHERE ligand_inchi_key = ? ORDER BY reactant_set_id",
            [c.inchi_key],
        ).df()
        return frame
    finally:
        cur.close()


def bindingdb_measurements_for_patent(doc_id: str, bindingdb_release: str | None = None) -> pd.DataFrame:
    """The affinity measurements BindingDB curated from a patent's own
    SAR tables, by publication number: ligand SMILES, InChIKey and name,
    target, organism, Ki/IC50/Kd/EC50. Empty frame if BindingDB did not
    curate the patent (it covers about 8,900 US patents).

    BindingDB keys patents as country plus number with no kind code
    (`US11566007`); SureChEMBL's `US-11566007-B2` is normalized to that.
    Join the result to `patent_chemistry()` on `ligand_inchi_key` to see
    which of SureChEMBL's extracted structures carry measured affinity.
    """
    normalized = patent_number(doc_id)
    parts = normalized.split("-")
    bindingdb_number = f"{parts[0]}{parts[1]}"
    cur, _ = _bindingdb_cursor(bindingdb_release)
    try:
        frame: pd.DataFrame = cur.execute(
            "SELECT reactant_set_id, ligand_smiles, ligand_inchi_key, ligand_name, target_name, "
            "target_source_organism, ki_nm_value, ki_nm_qualifier, ic50_nm_value, ic50_nm_qualifier, "
            "kd_nm_value, kd_nm_qualifier, ec50_nm_value, ec50_nm_qualifier, curation_source, patent_number "
            "FROM measurements WHERE patent_number = ? ORDER BY reactant_set_id",
            [bindingdb_number],
        ).df()
        return frame
    finally:
        cur.close()


def bindingdb_overlap_for_patent(doc_id: str, bindingdb_release: str | None = None) -> pd.DataFrame:
    """bindingdb_measurements_for_patent() with SureChEMBL's extracted
    compounds joined on: two added columns, `surechembl_id` (a compound
    with the identical standard InChIKey, or NA) and
    `surechembl_skeleton_ids` (every compound sharing the InChIKey's
    first block, i.e. the same connectivity with stereo or isotopes
    possibly differing; empty list if none).

    Both are needed, measured 2026-09-08 on four BindingDB-curated
    patents: for US-10730877-B2, 638 of BindingDB's 774 ligands match a
    SureChEMBL structure on the full key; for US-11566007-B2, 0 of 619 do
    but 562 share a skeleton, because BindingDB drew those ligands
    without stereochemistry (`UHFFFAOYSA` keys) while SureChEMBL kept the
    stereo the text specified; for US-11053244-B2 SureChEMBL extracted no
    structures at all, so neither matches. A skeleton match says "this
    measured ligand is this extracted scaffold, up to stereo", which is
    the level at which the two databases actually agree.
    """
    frame = bindingdb_measurements_for_patent(doc_id, bindingdb_release)
    if frame.empty:
        frame["surechembl_id"] = pd.Series(dtype="Int64")
        frame["surechembl_skeleton_ids"] = pd.Series(dtype="object")
        return frame
    by_key: dict[str, int] = {}
    by_skeleton: dict[str, list[int]] = {}
    for c in patent_chemistry(doc_id):
        if c.inchi_key:
            by_key.setdefault(c.inchi_key, c.id)
            by_skeleton.setdefault(c.inchi_key[:14], []).append(c.id)
    keys = frame["ligand_inchi_key"].fillna("")
    frame["surechembl_id"] = pd.array([by_key.get(k) for k in keys], dtype="Int64")
    frame["surechembl_skeleton_ids"] = [list(by_skeleton.get(k[:14], [])) for k in keys]
    return frame


# --- PubChem -------------------------------------------------------------


def pubchem_compound(compound: int | str | Compound) -> Any | None:
    """The PubChem record (a scigantic_pubchem.Compound: cid, title,
    IUPAC name, formula, weight) for a SureChEMBL compound, via the CID
    UniChem maps its InChIKey to. None if there is no PubChem entry."""
    pubchem = _need("scigantic_pubchem", "scigantic-pubchem")
    x = xrefs(compound)
    if x is None or not x.pubchem_cid:
        return None
    return pubchem.resolve(x.pubchem_cid[0], namespace="cid")
