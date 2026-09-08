"""SureChEMBL from Python: EMBL-EBI's database of chemistry extracted
from patents (31M compounds, 45M patents). Live lookups and searches
through the REST API, InChIKey and cross-reference resolution through
UniChem (ChEMBL, PubChem, DrugBank, ChEBI, PDB, BindingDB ids and the
patents for a compound held by any of them), full patent text, the bulk
parquet releases read in place from EBI with DuckDB, and, with the
`bridge` extra, joins into scigantic-chembl, scigantic-bindingdb and
scigantic-pubchem. No mirror, no download, no key."""

from ._client import BASE_URL, SureChEMBLError, __version__
from ._ids import compound_id, patent_number, schembl_id
from .cache import cache_dir, disable_cache, enable_cache, is_cache_enabled
from .cache import clear as clear_cache
from .compounds import (
    by_inchikey,
    by_name,
    by_smiles,
    compound,
    compounds,
    structure_image,
    surechembl_ids_for_inchikey,
)
from .models import Compound, LegalEvent, Patent, PatentHit, PatentRecord
from .patents import family_id, family_members, patent, patent_chemistry
from .xrefs import (
    Xrefs,
    patents_for_chembl,
    patents_for_inchikey,
    patents_for_pubchem_cid,
    surechembl_ids_for,
    xrefs,
    xrefs_for,
    xrefs_for_inchikey,
)
from .search import (
    SEARCH_MODES,
    STRUCTURE_SEARCH_CAP,
    count_patents,
    count_patents_for_compound,
    patents_for_compound,
    search_patents,
    similar_compounds,
    structure_search,
    substructure_search,
)

__all__ = [
    "__version__",
    "BASE_URL",
    "SureChEMBLError",
    "compound_id",
    "schembl_id",
    "patent_number",
    "compound",
    "compounds",
    "by_name",
    "by_smiles",
    "by_inchikey",
    "surechembl_ids_for_inchikey",
    "structure_image",
    "xrefs",
    "xrefs_for",
    "xrefs_for_inchikey",
    "surechembl_ids_for",
    "patents_for_chembl",
    "patents_for_pubchem_cid",
    "patents_for_inchikey",
    "Xrefs",
    "structure_search",
    "similar_compounds",
    "substructure_search",
    "SEARCH_MODES",
    "STRUCTURE_SEARCH_CAP",
    "patents_for_compound",
    "count_patents_for_compound",
    "search_patents",
    "count_patents",
    "patent",
    "patent_chemistry",
    "family_id",
    "family_members",
    "Compound",
    "PatentHit",
    "Patent",
    "PatentRecord",
    "LegalEvent",
    "enable_cache",
    "disable_cache",
    "is_cache_enabled",
    "cache_dir",
    "clear_cache",
]
