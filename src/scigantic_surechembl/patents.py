"""Patent documents: full text and bibliographic data, the chemistry
SureChEMBL extracted from a document, and DOCDB family membership.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any

from . import _client
from ._ids import patent_number
from .models import Compound, LegalEvent, Patent, _to_date, _to_int


def patent(doc_id: str) -> Patent | None:
    """A full patent document, or None if SureChEMBL has no document with
    that publication number (a 404 from the API, verified live).

    `doc_id` is normalized to `CC-NUMBER-KIND` first, so `US10000000B2`
    and `US-10000000-B2` are the same request. The document endpoint is
    keyed by the exact publication including kind code, so
    `US-10000000` without a kind will not resolve.
    """
    normalized = patent_number(doc_id)
    try:
        data = _client.request("GET", f"/document/{normalized}/contents")
    except _client.SureChEMBLError as exc:
        if exc.http_status == 404:
            return None
        raise
    return _parse_patent(normalized, data if isinstance(data, dict) else {})


def patent_chemistry(doc_id: str) -> list[Compound]:
    """Every compound SureChEMBL extracted from a document (text names,
    images and MOL attachments combined), with the full property set.

    Served by the export endpoint, which returns a zip containing one
    CSV; unpacked here. A document with no chemistry, or an unknown
    document (the endpoint answers both with a header-only CSV, verified
    live), returns [].
    """
    normalized = patent_number(doc_id)
    payload = _client.request_bytes(
        "POST", "/export/document-chemistry", params={"docID": normalized, "output_type": "csv"}
    )
    rows = _read_zipped_csv(payload)
    out: list[Compound] = []
    for row in rows:
        try:
            out.append(Compound.from_api(row))
        except ValueError:
            continue
    return out


def family_id(doc_id: str) -> int | None:
    """The DOCDB simple-family id for a publication, or None if
    SureChEMBL has no family for it."""
    normalized = patent_number(doc_id)
    data = _client.request("GET", f"/document/{normalized}/family/id")
    if not isinstance(data, dict):
        return None
    value = data.get(normalized)
    return _to_int(value)


def family_members(doc_id: str) -> list[str]:
    """Every publication in the same DOCDB simple family, including the
    query document itself, as publication numbers. [] for an unknown
    document (the API answers that with a 500 "SQL exception detected
    while reading family members", verified live; treated as a miss here
    rather than raised, since it is the endpoint's only miss signal)."""
    normalized = patent_number(doc_id)
    try:
        data = _client.request("GET", f"/document/{normalized}/family/members")
    except _client.SureChEMBLError as exc:
        if exc.http_status == 500 and "family members" in str(exc):
            return []
        raise
    entry = (data or {}).get(normalized) if isinstance(data, dict) else None
    members: list[str] = []
    for item in (entry or {}).get("members") or []:
        if isinstance(item, dict):
            members.extend(str(k) for k in item.keys())
    return members


# --- parsing the document record --------------------------------------------

_CLASS_SYMBOL_RE = re.compile(r"^([A-H][0-9]{2}[A-Z]\s*[0-9]+/[0-9]+)")
_TAG_RE = re.compile(r"</?p>|<br\s*/?>")


def _read_zipped_csv(payload: bytes) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise _client.SureChEMBLError(f"document-chemistry export is not a zip: {payload[:80]!r}") from exc
    rows: list[dict[str, Any]] = []
    for name in archive.namelist():
        if not name.lower().endswith(".csv"):
            continue
        text = archive.read(name).decode("utf-8", errors="replace")
        rows.extend(csv.DictReader(io.StringIO(text)))
    return rows


def _first(items: Any) -> dict[str, Any]:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    if isinstance(items, dict):
        return items
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _section_text(sections: Any) -> str | None:
    """English text of a list of {lang, section: {content}} entries, or
    the first available language. The content carries residual <p>
    markup from the office XML; stripped to newlines."""
    chosen: str | None = None
    for entry in _as_list(sections):
        if not isinstance(entry, dict):
            continue
        content = (entry.get("section") or {}).get("content")
        if not content:
            continue
        if str(entry.get("lang", "")).upper() == "EN":
            chosen = str(content)
            break
        if chosen is None:
            chosen = str(content)
    if chosen is None:
        return None
    return _TAG_RE.sub("\n", chosen).strip() or None


def _party_names(parties: Any, key: str, inner: str) -> list[str]:
    """Names of one party kind, de-duplicated across the record's copies
    of each party. A record carries the same person or company up to
    three times (format "original" as filed, "epo"/DOCDB, and
    "intermediate"), with the name reordered and re-cased each time
    ("Joseph Marron" / "MARRON JOSEPH" / "MARRON, JOSEPH"), so identity
    is the sorted set of name tokens, and the as-filed "original" copy
    is preferred as the spelling to keep."""
    entries: list[tuple[int, str]] = []
    for group in _as_list(parties):
        block = (group or {}).get(key) or {}
        for person in _as_list(block.get(inner)):
            book = (person or {}).get("addressbook") or {}
            name = book.get("name") or " ".join(
                part for part in (book.get("firstName"), book.get("lastName")) if part
            )
            if not name:
                continue
            rank = 0 if (person or {}).get("format") == "original" else 1
            entries.append((rank, " ".join(str(name).split())))
    names: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for _, name in sorted(entries, key=lambda e: e[0]):
        tokens = tuple(sorted(re.sub(r"[.,]", " ", name).upper().split()))
        if tokens not in seen:
            seen.add(tokens)
            names.append(name)
    return names


def _classifications(technical: dict[str, Any], outer: str, inner: str) -> list[str]:
    symbols: list[str] = []
    for group in _as_list(technical.get(outer)):
        for entry in _as_list((group or {}).get(inner)):
            text = str((entry or {}).get("classification", ""))
            match = _CLASS_SYMBOL_RE.match(text)
            symbol = re.sub(r"\s+", "", match.group(1)) if match else text.split()[0] if text.split() else ""
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _citations(technical: dict[str, Any]) -> list[str]:
    cited: list[str] = []
    for group in _as_list(technical.get("citations")):
        for cit in _as_list(((group or {}).get("patentCitations") or {}).get("patcit")):
            ucid = (cit or {}).get("ucid")
            if ucid and ucid not in cited:
                cited.append(str(ucid))
    return cited


def _legal_events(status: Any) -> list[LegalEvent]:
    events: list[LegalEvent] = []
    for group in _as_list(status):
        for ev in _as_list((group or {}).get("legal-event")):
            if not isinstance(ev, dict):
                continue
            body = ev.get("legal-event-body") or {}
            title = (body.get("event-title") or {}).get("$") if isinstance(body, dict) else None
            events.append(
                LegalEvent(
                    code=ev.get("@code"),
                    date=_to_date(ev.get("@date")),
                    country=ev.get("@country"),
                    impact=ev.get("@impact"),
                    title=str(title) if title else None,
                    raw=ev,
                )
            )
    return events


def _parse_patent(doc_id: str, data: dict[str, Any]) -> Patent:
    doc = ((data.get("contents") or {}).get("patentDocument")) or {}
    biblio = doc.get("bibliographicData") or {}
    technical = biblio.get("technicalData") or {}
    parties = biblio.get("parties")

    titles: list[str] = []
    english: list[str] = []
    for entry in _as_list(technical.get("inventionTitles")):
        if isinstance(entry, dict) and entry.get("title"):
            text = str(entry["title"]).strip()
            titles.append(text)
            if str(entry.get("lang", "")).upper() == "EN":
                english.append(text)
    # Last English title, matching SureChEMBL's own bulk `patents.title`
    # (see Patent docstring); first title of any language otherwise.
    title = english[-1] if english else (titles[0] if titles else None)

    app_ref = _first(biblio.get("applicationReference"))
    priorities: list[str] = []
    for group in _as_list(biblio.get("priorityClaims")):
        for claim in _as_list((group or {}).get("priorityClaims")):
            ucid = (claim or {}).get("ucid")
            if ucid and ucid not in priorities:
                priorities.append(str(ucid))

    family_ids = [_to_int(f) for f in _as_list(doc.get("family"))]
    fam = next((f for f in family_ids if f is not None), None)

    return Patent(
        doc_id=str(data.get("doc_id") or doc_id),
        title=title,
        titles=titles,
        published=_to_date(doc.get("published")),
        abstract=_section_text(doc.get("abstracts")),
        claims=_section_text(doc.get("claimResponses")),
        description=_section_text(doc.get("descriptions")),
        applicants=_party_names(parties, "applicants", "applicant"),
        inventors=_party_names(parties, "inventors", "inventor"),
        assignees=_party_names(parties, "assignees", "assignee"),
        cpc=_classifications(technical, "classificationsCpc", "classificationCpc"),
        ipcr=_classifications(technical, "classificationsIpcr", "classificationIpcr"),
        family_id=fam,
        application_number=str(app_ref["ucid"]) if app_ref.get("ucid") else None,
        priority_numbers=priorities,
        citations=_citations(technical),
        legal_events=_legal_events(doc.get("legalStatus")),
        has_pdf=bool(doc.get("hasPDF")),
        raw=data,
    )
