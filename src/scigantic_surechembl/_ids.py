"""Identifier normalization for the two id systems this package touches.

SureChEMBL compound ids are plain integers in both the REST API and the
bulk parquet (`compounds.id`), but the website, UniChem, and every paper
show them as `SCHEMBL1353`. The REST API does NOT accept the prefixed
form: verified live 2026-09-08, `GET /chemical/id/SCHEMBL1353` is a 500
("For input string: SCHEMBL1353") while `/chemical/id/1353` is the
compound. Every public function here takes either form and strips the
prefix before the request goes out; every returned model carries both
`id` (int) and `schembl_id` ("SCHEMBL1353"), because SureChEMBL's own
attribution terms ask that the SCHEMBL ids be preserved downstream.

Patent numbers in SureChEMBL are `CC-NUMBER-KIND` (e.g. `US-10000000-B2`,
`WO-2016144528-A1`). The REST document endpoints want exactly that form;
the common unhyphenated `US10000000B2` and the spaced/slashed forms
found in papers and Google Patents URLs are normalized to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_COMPOUND_RE = re.compile(r"^\s*(?:SCHEMBL)?(\d+)\s*$", re.IGNORECASE)
_PATENT_RE = re.compile(r"^([A-Z]{2})[-\s]?([0-9]{4,})(?:[-\s]?([A-Z][0-9]?))?$")


def compound_id(value: int | str) -> int:
    """Return the integer SureChEMBL compound id for `1353`, `"1353"`, or
    `"SCHEMBL1353"` (case-insensitive). Raises ValueError for anything
    else, so a typo never reaches the API as a request that 500s."""
    if isinstance(value, bool):  # bool is an int subclass; never a compound id
        raise ValueError(f"not a SureChEMBL compound id: {value!r}")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"not a SureChEMBL compound id: {value!r}")
        return value
    match = _COMPOUND_RE.match(value)
    if not match:
        raise ValueError(f"not a SureChEMBL compound id: {value!r}")
    parsed = int(match.group(1))
    if parsed <= 0:
        raise ValueError(f"not a SureChEMBL compound id: {value!r}")
    return parsed


def compound_ids(values: Iterable[int | str]) -> list[int]:
    """compound_id() over an iterable, de-duplicated, input order kept."""
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        parsed = compound_id(value)
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out


def schembl_id(value: int | str) -> str:
    """The display form SureChEMBL uses everywhere but its REST API:
    `schembl_id(1353) == "SCHEMBL1353"`."""
    return f"SCHEMBL{compound_id(value)}"


def patent_number(value: str) -> str:
    """Normalize a patent publication number to SureChEMBL's
    `CC-NUMBER-KIND` form.

    Accepts `US-10000000-B2`, `US10000000B2`, `US 10000000 B2`,
    `WO2016/144528A1`, or a bare `US10000000` (kind code omitted; the
    result is then `US-10000000`, which the document endpoints may or may
    not resolve, since SureChEMBL keys documents by the full publication
    number including kind). Raises ValueError for a string that does not
    look like a publication number at all.
    """
    cleaned = re.sub(r"[/,.]", "", value.strip().upper())
    match = _PATENT_RE.match(cleaned)
    if not match:
        raise ValueError(f"not a patent publication number: {value!r}")
    country, number, kind = match.groups()
    return f"{country}-{number}-{kind}" if kind else f"{country}-{number}"
