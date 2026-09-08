"""`scigantic-surechembl` command line: one subcommand per public
function, JSON to stdout so the output pipes into jq or a file."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import date
from typing import Any

from . import (
    __version__,
    by_inchikey,
    by_name,
    by_smiles,
    clear_cache,
    compound,
    compounds,
    count_patents,
    count_patents_for_compound,
    family_id,
    family_members,
    patent,
    patent_chemistry,
    patents_for_compound,
    search_patents,
    structure_image,
    structure_search,
)
from .search import SEARCH_MODES


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items() if k != "raw"}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _emit(value: Any) -> None:
    json.dump(_plain(value), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scigantic-surechembl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("compound", help="one compound by id (1353 or SCHEMBL1353)")
    p.add_argument("id")
    p = sub.add_parser("compounds", help="many compounds by id")
    p.add_argument("ids", nargs="+")
    p = sub.add_parser("name", help="compounds by chemical name")
    p.add_argument("name")
    p = sub.add_parser("smiles", help="the compound with this exact structure")
    p.add_argument("smiles")
    p = sub.add_parser("inchikey", help="compounds with this InChIKey (via UniChem)")
    p.add_argument("inchi_key")
    p = sub.add_parser("search", help="structure search over the compound index")
    p.add_argument("structure", help="SMILES (or SMARTS for substructure)")
    p.add_argument("--mode", choices=SEARCH_MODES, default="substructure")
    p.add_argument("--max-results", type=int, default=20)
    p = sub.add_parser("patents-for", help="patents containing compound id(s)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--max-results", type=int, default=20)
    p.add_argument("--count", action="store_true", help="print only the total")
    p = sub.add_parser("text", help="Solr full-text patent search")
    p.add_argument("query")
    p.add_argument("--max-results", type=int, default=20)
    p.add_argument("--count", action="store_true", help="print only the total")
    p = sub.add_parser("patent", help="one patent document by publication number")
    p.add_argument("doc_id")
    p.add_argument("--full-text", action="store_true", help="include description and claims")
    p = sub.add_parser("chemistry", help="compounds SureChEMBL extracted from a patent")
    p.add_argument("doc_id")
    p = sub.add_parser("family", help="DOCDB family id and members of a patent")
    p.add_argument("doc_id")
    p = sub.add_parser("image", help="write a PNG depiction of a SMILES")
    p.add_argument("smiles")
    p.add_argument("out")
    p.add_argument("--size", type=int, default=300)
    p = sub.add_parser("releases", help="bulk release dates on EBI (needs the bulk extra)")
    p = sub.add_parser("sql", help="DuckDB SQL over the bulk tables (needs the bulk extra)")
    p.add_argument("query")
    p.add_argument("--release")
    sub.add_parser("cache-clear", help="delete the local response cache")

    args = parser.parse_args(argv)
    cmd = args.command
    if cmd == "compound":
        _emit(compound(args.id))
    elif cmd == "compounds":
        _emit(compounds(args.ids))
    elif cmd == "name":
        _emit(by_name(args.name))
    elif cmd == "smiles":
        _emit(by_smiles(args.smiles))
    elif cmd == "inchikey":
        _emit(by_inchikey(args.inchi_key))
    elif cmd == "search":
        _emit(structure_search(args.structure, args.mode, args.max_results))
    elif cmd == "patents-for":
        if args.count:
            _emit(count_patents_for_compound(args.ids))
        else:
            _emit(patents_for_compound(args.ids, args.max_results))
    elif cmd == "text":
        if args.count:
            _emit(count_patents(args.query))
        else:
            _emit(search_patents(args.query, args.max_results))
    elif cmd == "patent":
        doc = patent(args.doc_id)
        if doc is None:
            _emit(None)
        else:
            payload = _plain(doc)
            if not args.full_text:
                payload.pop("description", None)
                payload.pop("claims", None)
            _emit(payload)
    elif cmd == "chemistry":
        _emit(patent_chemistry(args.doc_id))
    elif cmd == "family":
        _emit({"family_id": family_id(args.doc_id), "members": family_members(args.doc_id)})
    elif cmd == "image":
        png = structure_image(args.smiles, args.size, args.size)
        with open(args.out, "wb") as f:
            f.write(png)
        _emit({"written": args.out, "bytes": len(png)})
    elif cmd == "releases":
        from . import bulk

        _emit(bulk.releases())
    elif cmd == "sql":
        from . import bulk

        frame = bulk.sql(args.query, args.release)
        sys.stdout.write(frame.to_string(max_rows=200) + "\n")
    elif cmd == "cache-clear":
        _emit({"removed": clear_cache()})
    return 0
