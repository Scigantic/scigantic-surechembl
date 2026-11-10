"""Patent documents: full text and bibliographic data, the chemistry
SureChEMBL finds in a document, and DOCDB family membership.

Rebuilt on SureChEMBL's late-2026 API (verified live 2026-11-09):

- `GET /patent/get_patent/{ucid}` returns the document with each
  bibliographic section as a string of the office XML (`parties`,
  `priority_claims`, `application_references`, `classifications`,
  `citations`) and the text sections as `[{language, text}]` lists whose
  text is still marked up. Both are parsed here. An unknown publication
  is a 404 `{"detail": "Patent not found: ..."}`.
- The record carries no family id. The content-search index does
  (`fam`), so `family_id()` and `family_members()` are answered from
  `GET /search/content` with `ucid:"..."` and `fam:N` queries, and
  `patent()` makes that one extra request to fill `family_id` and the
  index's title. The index only covers US, EP, WO, JP and CN
  publications (the server ANDs every query with that country filter),
  so family members from other offices are not listed.
- The chemistry export the 0.3 releases used (`/export/document-chemistry`,
  a zip of every compound from text, images and MOL attachments) is gone.
  `GET /patent/leadmine/{ucid}` runs SureChEMBL's text-mining (LeadMine)
  over the document and tags each entity with a SureChEMBL id where it
  resolves one, so `patent_chemistry()` now returns the compounds named
  in the text only. Structures that appear only as images or attachments
  are not included (the bulk parquet's patent_compound_map still has the
  full extraction; see the bulk module).
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

from . import _client
from ._ids import compound_id, patent_number
from .models import Compound, Patent, _to_date, _to_int


def patent(doc_id: str) -> Patent | None:
    """A full patent document, or None if SureChEMBL has no document with
    that publication number (a 404 from the API, verified live).

    `doc_id` is normalized to `CC-NUMBER-KIND` first, so `US10000000B2`
    and `US-10000000-B2` are the same request. The endpoint is keyed by
    the exact publication including kind code, so `US-10000000` without
    a kind will not resolve. Makes two requests: the document, then its
    search-index entry for the family id and title (see the module
    docstring); if the second fails, `family_id` is None and the title
    falls back to the record's own.
    """
    normalized = patent_number(doc_id)
    try:
        data = _client.request("GET", f"/patent/get_patent/{normalized}")
    except _client.SureChEMBLError as exc:
        if exc.http_status == 404:
            return None
        raise
    try:
        index = _index_record(normalized)
    except _client.SureChEMBLError:
        index = None
    return _parse_patent(normalized, data if isinstance(data, dict) else {}, index)


def patent_pdf(doc_id: str) -> bytes | None:
    """The document's PDF as bytes, or None if SureChEMBL has none for it
    (a 404). A PDF is about a megabyte, so this is a deliberate call, not
    something `patent()` does to learn whether one exists."""
    normalized = patent_number(doc_id)
    try:
        return _client.request_bytes("GET", f"/patent/pdf/{normalized}")
    except _client.SureChEMBLError as exc:
        if exc.http_status == 404:
            return None
        raise


def patent_chemistry(doc_id: str) -> list[Compound]:
    """The compounds SureChEMBL's text-mining finds in a document, with
    the full property set, in order of first mention.

    Served by `/patent/leadmine/{ucid}`: every named entity LeadMine
    resolves to a SureChEMBL id (systematic and trivial names, drug
    names, some formulas) is collected, de-duplicated, and fetched
    through `/chemical/id`. Text only: structures that appear only as
    images or MOL attachments are not included, so the count is lower
    than the 0.3 export gave (US-5399578-A, the valsartan patent: 454
    ids from text against 986 from the old export). An unknown document
    returns [], as does one whose text names no resolvable compound. The
    analysis runs live on the server and takes several seconds for a long
    document.
    """
    normalized = patent_number(doc_id)
    try:
        data = _client.request("GET", f"/patent/leadmine/{normalized}", timeout=120.0)
    except _client.SureChEMBLError as exc:
        if exc.http_status == 404:
            return []
        raise
    ids: list[int] = []
    seen: set[int] = set()
    for entity in (data.get("entities") or []) if isinstance(data, dict) else []:
        raw = entity.get("sureChemblId") if isinstance(entity, dict) else None
        if not raw:
            continue
        try:
            cid = compound_id(raw)
        except ValueError:
            continue
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    if not ids:
        return []
    from .compounds import compounds

    found = compounds(ids)
    return [found[i] for i in ids if i in found]


def family_id(doc_id: str) -> int | None:
    """The DOCDB simple-family id for a publication, from SureChEMBL's
    search index, or None if the index has no family for it. The index
    covers US, EP, WO, JP and CN publications only, so for a publication
    from any other office this is None even when the family exists."""
    record = _index_record(patent_number(doc_id))
    return _to_int(record.get("fam")) if record else None


def family_members(doc_id: str) -> list[str]:
    """The publications in the same DOCDB simple family, including the
    query document itself, newest first, as publication numbers. [] for
    a document the index does not know.

    Limited to US, EP, WO, JP and CN publications: SureChEMBL's search
    index (the only place the late-2026 API exposes families) filters
    every query to those five offices. The 0.3 releases listed members
    from every office, so a family with DE, AU or KR members is shorter
    here than it used to be. Family 37847165 (imatinib mesylate crystal
    forms) lists 21 members.
    """
    fam = family_id(doc_id)
    if fam is None:
        return []
    from .search import _page_documents

    hits = _page_documents("/search/content", {"query": f"fam:{fam}", "grouped": "false"}, 10_000, 1000)
    return [h.doc_id for h in hits]


def _index_record(normalized: str) -> dict[str, Any] | None:
    """The search index's document for one publication number, or None."""
    data = _client.request(
        "GET",
        "/search/content",
        params={"query": f'ucid:"{normalized}"', "page": 1, "itemsPerPage": 1, "grouped": "false"},
    )
    docs = (data.get("docs") or []) if isinstance(data, dict) else []
    for d in docs:
        if isinstance(d, dict) and d.get("ucid") == normalized:
            return d
    return None


# --- parsing the document record --------------------------------------------

_CLASS_SYMBOL_RE = re.compile(r"^([A-H][0-9]{2}[A-Z])\s*([0-9]+)\s*/\s*([0-9]+)")
_BLOCK_END_RE = re.compile(
    r"</(?:p|heading|claim|claim-text|claim-statement|li|tr|div)>|<br\s*/?>", re.IGNORECASE
)
_PI_RE = re.compile(r"<\?.*?\?>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANKS_RE = re.compile(r"\n\s*\n+")


def _xml(fragment: Any) -> ET.Element | None:
    if not isinstance(fragment, str) or not fragment.strip():
        return None
    try:
        return ET.fromstring(fragment)
    except ET.ParseError:
        return None


def _elements(fragments: Any) -> list[ET.Element]:
    out: list[ET.Element] = []
    for frag in fragments if isinstance(fragments, list) else [fragments]:
        el = _xml(frag)
        if el is not None:
            out.append(el)
    return out


def _clean_text(markup: str) -> str | None:
    """Plain text from a marked-up section: block ends become line
    breaks, processing instructions and every other tag are dropped, and
    entities are unescaped."""
    text = _PI_RE.sub("", markup)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    joined = "\n".join(lines)
    return _BLANKS_RE.sub("\n", joined).strip() or None


def _section_text(sections: Any) -> str | None:
    """English text of a list of {language, text} entries, or the first
    available language."""
    chosen: str | None = None
    for entry in sections if isinstance(sections, list) else []:
        if not isinstance(entry, dict) or not entry.get("text"):
            continue
        if str(entry.get("language", "")).upper() == "EN":
            chosen = str(entry["text"])
            break
        if chosen is None:
            chosen = str(entry["text"])
    return _clean_text(chosen) if chosen else None


def _party_names(parties: list[ET.Element], kind: str) -> list[str]:
    """Names of one party kind (applicant, inventor, assignee),
    de-duplicated across the record's copies of each party. A record
    carries the same person or company up to three times (format
    "original" as filed, "epo"/DOCDB, and "intermediate"), with the name
    reordered and re-cased each time ("Michael Mutz" / "MUTZ MICHAEL" /
    "MUTZ, MICHAEL"), so identity is the sorted set of name tokens, and
    the as-filed "original" copy is preferred as the spelling to keep."""
    entries: list[tuple[int, str]] = []
    for root in parties:
        for person in root.iter(kind):
            book = person.find("addressbook")
            if book is None:
                continue
            name = (book.findtext("name") or "").strip()
            if not name:
                name = " ".join(
                    part.strip() for part in (book.findtext("first-name"), book.findtext("last-name")) if part and part.strip()
                )
            if not name:
                continue
            rank = 0 if person.get("format") == "original" else 1
            entries.append((rank, " ".join(name.split())))
    names: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for _, name in sorted(entries, key=lambda e: e[0]):
        tokens = tuple(sorted(re.sub(r"[.,]", " ", name).upper().split()))
        if tokens not in seen:
            seen.add(tokens)
            names.append(name)
    return names


def _classifications(roots: list[ET.Element]) -> tuple[list[str], list[str]]:
    """(cpc, ipc) symbols with spaces removed (`C07D257/04`). The record's
    classification array was empty on most documents probed; where it is
    present it holds `<classification-ipc>` blocks of main and further
    classifications."""
    cpc: list[str] = []
    ipc: list[str] = []
    for root in roots:
        target = cpc if "cpc" in root.tag.lower() else ipc
        for el in root.iter():
            match = _CLASS_SYMBOL_RE.match((el.text or "").strip())
            if match:
                symbol = f"{match.group(1)}{match.group(2)}/{match.group(3)}"
                if symbol not in target:
                    target.append(symbol)
    return cpc, ipc


def _ucids(roots: list[ET.Element], tag: str | None = None) -> list[str]:
    out: list[str] = []
    for root in roots:
        for el in root.iter(tag) if tag else root.iter():
            ucid = el.get("ucid")
            if ucid and ucid not in out:
                out.append(ucid)
    return out


def _parse_patent(doc_id: str, data: dict[str, Any], index: dict[str, Any] | None) -> Patent:
    titles: list[str] = []
    english: list[str] = []
    for entry in data.get("title") or []:
        if isinstance(entry, dict) and entry.get("text"):
            text = " ".join(str(entry["text"]).split())
            titles.append(text)
            if str(entry.get("language", "")).upper() == "EN":
                english.append(text)
    index_title = None
    if index:
        ttl = index.get("ttl_en")
        if isinstance(ttl, list) and ttl:
            index_title = " ".join(str(ttl[0]).split()) or None
    title = index_title or (english[0] if english else (titles[0] if titles else None))

    parties = _elements(data.get("parties"))
    apps = _elements(data.get("application_references"))
    application = next((u for u in _ucids(apps, "application-reference")), None)
    cpc, ipc = _classifications(_elements(data.get("classifications")))

    return Patent(
        doc_id=str(data.get("ucid") or doc_id),
        title=title,
        titles=titles,
        published=_to_date(data.get("published_date")),
        abstract=_section_text(data.get("abstract_text")),
        claims=_section_text(data.get("claims")),
        description=_section_text(data.get("description")),
        applicants=_party_names(parties, "applicant"),
        inventors=_party_names(parties, "inventor"),
        assignees=_party_names(parties, "assignee"),
        cpc=cpc,
        ipcr=ipc,
        family_id=_to_int(index.get("fam")) if index else None,
        application_number=application,
        priority_numbers=_ucids(_elements(data.get("priority_claims")), "priority-claim"),
        citations=_ucids(_elements(data.get("citations"))),
        legal_events=[],
        has_pdf=None,
        raw=data,
    )
