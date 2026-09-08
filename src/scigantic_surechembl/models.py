"""Typed records for what SureChEMBL returns.

Every model keeps the raw API dict it was built from in `raw`, since the
document endpoint in particular returns a deeply nested XML-to-JSON dump
(bibliographic data, parties, classifications, citations, legal events,
drawings) and the typed fields here are the parts most callers want, not
a claim to have modeled all of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ._ids import schembl_id as _schembl_id


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    """SureChEMBL encodes its flags as the strings "0"/"1" (is_element,
    mchem_struct_alert) or the ints 0/1 (organic, ro3_pass)."""
    parsed = _to_int(value)
    return None if parsed is None else bool(parsed)


def _to_date(value: Any) -> date | None:
    """`20180619` (the document endpoint), `2018-06-19`, or the literal
    string "null" (the search endpoints, for a hit with no date)."""
    if value is None or value in ("", "null"):
        return None
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class Compound:
    """One SureChEMBL compound. `id` is the integer the API and the bulk
    parquet use; `schembl_id` is the `SCHEMBL1353` form the website,
    UniChem and the literature use.

    The property fields (mol_formula through struct_alert) are present on
    the id/SMILES/structure-search endpoints and absent (None) on the
    name endpoint and the bulk parquet, which carry only structure and
    weight. `similarity` is set only on similarity-search hits.
    `global_frequency` is SureChEMBL's own counter passed through as-is;
    it is NOT the number of documents the compound appears in (aspirin,
    id 1353, reports 22 and is in 694,428 documents; id 29350479 reports
    0 and is in 1,891). Use `count_patents_for_compound()` for that.
    """

    id: int
    name: str | None = None
    smiles: str | None = None
    inchi: str | None = None
    inchi_key: str | None = None
    mol_weight: float | None = None
    mol_formula: str | None = None
    log_p: float | None = None
    hbd: int | None = None
    hba: int | None = None
    psa: float | None = None
    rtb: int | None = None
    heavy_atoms: int | None = None
    aromatic_rings: int | None = None
    qed_weighted: float | None = None
    num_ro5_violations: int | None = None
    ro3_pass: bool | None = None
    organic: bool | None = None
    is_element: bool | None = None
    global_frequency: int | None = None
    struct_alert: bool | None = None
    similarity: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def schembl_id(self) -> str:
        return _schembl_id(self.id)

    @property
    def url(self) -> str:
        return f"https://www.surechembl.org/chemical/{self.id}"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Compound:
        """Build from a /chemical/... or search-result record. The API
        sends every number as a string in some endpoints and as a number
        in others (`mol_weight` is "180.157" on one and 180.157 on the
        next), so everything is coerced here rather than trusted."""
        raw_id = d.get("id", d.get("chemical_id"))
        parsed_id = _to_int(raw_id)
        if parsed_id is None:
            raise ValueError(f"compound record has no id: {d!r}")
        return cls(
            id=parsed_id,
            name=d.get("name") or None,
            smiles=d.get("smiles") or None,
            inchi=d.get("inchi") or None,
            inchi_key=d.get("inchi_key") or None,
            mol_weight=_to_float(d.get("mol_weight")),
            mol_formula=d.get("mol_formula") or None,
            log_p=_to_float(d.get("log_p")),
            hbd=_to_int(d.get("hbd")),
            hba=_to_int(d.get("hba")),
            psa=_to_float(d.get("psa")),
            rtb=_to_int(d.get("rtb")),
            heavy_atoms=_to_int(d.get("heavy_atoms")),
            aromatic_rings=_to_int(d.get("aromatic_rings")),
            qed_weighted=_to_float(d.get("qed_weighted")),
            num_ro5_violations=_to_int(d.get("num_ro5_violations")),
            ro3_pass=_to_bool(d.get("ro3_pass")),
            organic=_to_bool(d.get("organic")),
            is_element=_to_bool(d.get("is_element")),
            global_frequency=_to_int(d.get("global_frequency")),
            struct_alert=_to_bool(d.get("mchem_struct_alert")),
            similarity=_to_float(d.get("similarity")),
            raw=d,
        )


@dataclass(frozen=True)
class PatentHit:
    """One document from a search (`patents_for_compound()`,
    `search_patents()`): the publication number plus whatever the search
    index attached. `assignee` and `publication_date` are None when the
    index has none (the API sends the literal string "null" there)."""

    doc_id: str
    title: str | None = None
    publication_date: date | None = None
    assignee: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def url(self) -> str:
        return f"https://www.surechembl.org/patent/{self.doc_id}"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> PatentHit:
        meta = d.get("metadata") or {}
        title = None
        for entry in meta.get("titles") or []:
            titles = entry.get("titles") or []
            if titles and (entry.get("lang") == "en" or title is None):
                title = titles[0]
                if entry.get("lang") == "en":
                    break
        assignee = d.get("pa")
        return cls(
            doc_id=str(d.get("docId", "")),
            title=title or None,
            publication_date=_to_date(meta.get("pd")),
            assignee=None if assignee in (None, "", "null") else str(assignee),
            raw=d,
        )


@dataclass(frozen=True)
class LegalEvent:
    code: str | None
    date: date | None
    country: str | None
    impact: str | None
    title: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Patent:
    """A full patent document from `/document/{id}/contents`.

    `title` is the English title SureChEMBL itself uses for the document
    (a record can carry two English titles, the office's and a vendor's
    descriptive one, in either order; SureChEMBL's bulk `patents.title`
    is the last one listed, verified on every multi-title record found,
    so `title` follows the same rule and `titles` has all of them).
    `abstract`, `claims` and `description` are the English-language text
    sections when the office supplied them (first available language
    otherwise). `applicants`/`inventors`/`assignees` are de-duplicated
    name lists across the several formats the record carries for each
    party (original, DOCDB, intermediate). `cpc`/`ipcr` are the bare
    classification symbols with the trailing version/position codes
    stripped. `citations` are the cited publication numbers. The whole
    record is in `raw` for anything not lifted out here.
    """

    doc_id: str
    title: str | None = None
    titles: list[str] = field(default_factory=list)
    published: date | None = None
    abstract: str | None = None
    claims: str | None = None
    description: str | None = None
    applicants: list[str] = field(default_factory=list)
    inventors: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    cpc: list[str] = field(default_factory=list)
    ipcr: list[str] = field(default_factory=list)
    family_id: int | None = None
    application_number: str | None = None
    priority_numbers: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    legal_events: list[LegalEvent] = field(default_factory=list)
    has_pdf: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def url(self) -> str:
        return f"https://www.surechembl.org/patent/{self.doc_id}"

    @property
    def pdf_url(self) -> str | None:
        return f"https://www.surechembl.org/assets/pdf/{self.doc_id}" if self.has_pdf else None


@dataclass(frozen=True)
class PatentRecord:
    """One row of the bulk `patents` table: the bibliographic subset
    SureChEMBL publishes for every patent it extracted chemistry from.
    `id` is the bulk-only integer key `patent_compound_map` joins on
    (the REST API never exposes it); `patent_number` is the publication
    number the REST document endpoints take. `family_id` is -1 in the
    source when DOCDB supplied none; None here."""

    id: int
    patent_number: str
    country: str | None = None
    publication_date: date | None = None
    family_id: int | None = None
    title: str | None = None
    assignee: list[str] = field(default_factory=list)
    cpc: list[str] = field(default_factory=list)
    ipcr: list[str] = field(default_factory=list)
    ipc: list[str] = field(default_factory=list)
    ecla: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.surechembl.org/patent/{self.patent_number}"
