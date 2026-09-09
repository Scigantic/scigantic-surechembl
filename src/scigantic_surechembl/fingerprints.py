"""Local similarity search over every SureChEMBL compound, from the FPSim2
fingerprint file EBI ships with each bulk release.

`fpsim2_fingerprints.h5` (1.36 GB in the 2026-09-08 release) is a ready-made
FPSim2 database keyed by SureChEMBL compound id. Downloaded once, it gives
Tanimoto similarity and top-k over all 31M compounds in memory in well under
a second per query, with no dependence on the server-side structure-search
worker (which a single broad query has been seen to take down for an hour)
and no 10,000-hit cap. The tradeoff is the one-time download and about
1.4 GB of RAM while the engine is loaded.

EBI's file is Morgan radius 2 at 256 bits (built with RDKit 2021.09; FPSim2
prints a version warning on load, harmless for Morgan). 256 bits is coarse:
many distinct structures tie at similarity 1.0 (aspirin's dimer and its
13C isotopologue both score 1.0 against aspirin), so treat `similar()` as a
neighbourhood, not a ranking. Its counts match the server exactly (232
aspirin hits at 0.7 both ways), which says the server searches the same
fingerprints.

Substructure here is a fingerprint screen, and with Morgan fingerprints it
is INCOMPLETE: FPSim2's `substructure()` returns compounds whose bits
contain the query's, which is a superset of true matches only for pattern
fingerprints. Measured 2026-09-08: quinoline gave 4,346 candidates of which
RDKit confirmed 89% of a sample, while the server's substructure search
returned over 10,000 (its cap). So `substructure_candidates()` is a fast
partial screen (benzene: 11.2M candidates in 0.35 s), not a search; use the
server's `substructure_search()` for recall and confirm candidates with
RDKit on their SMILES (`compounds()` fetches them in batches of 500).

Needs the `fingerprints` extra: `pip install "scigantic-surechembl[fingerprints]"`
(FPSim2, RDKit, PyTables).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _client, bulk, cache

if TYPE_CHECKING:
    import pandas

FINGERPRINT_FILE = "fpsim2_fingerprints.h5"


def fingerprint_url(release: str | None = None) -> str:
    return f"{bulk.BULK_BASE}{release or bulk.latest_release()}/{FINGERPRINT_FILE}"


def fingerprint_path(release: str | None = None) -> Path:
    """Default local location for a release's fingerprint file, under this
    package's cache directory."""
    return cache.cache_dir() / f"fpsim2_fingerprints_{release or bulk.latest_release()}.h5"


def download_fingerprints(dest: str | Path | None = None, release: str | None = None) -> Path:
    """Download a release's FPSim2 file (resumable, size-verified), to
    `dest` or the default cache path. Returns the path; a complete file
    already there is not re-downloaded."""
    release = release or bulk.latest_release()
    url = fingerprint_url(release)
    out = Path(dest) if dest is not None else fingerprint_path(release)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    head = _client.send("HEAD", url, timeout=60.0)
    if head.status_code == 404:
        raise _client.SureChEMBLError(f"{url} does not exist; check bulk.releases()", http_status=404)
    total = int(head.headers.get("Content-Length") or 0)
    if out.exists() and total and out.stat().st_size == total:
        return out
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have and total and have < total else {}
    with _client._get_session().get(url, headers=headers, stream=True, timeout=(10.0, 120.0)) as response:
        if response.status_code == 416:
            pass  # already complete on disk
        elif response.status_code not in (200, 206):
            raise _client.SureChEMBLError(f"download of {url} failed: HTTP {response.status_code}", http_status=response.status_code)
        else:
            with part.open("ab" if response.status_code == 206 else "wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
    size = part.stat().st_size
    if total and size != total:
        raise _client.SureChEMBLError(f"download of {url} incomplete: {size} of {total} bytes; re-run to resume")
    os.replace(part, out)
    return out


class FingerprintIndex:
    """An FPSim2 engine over a SureChEMBL fingerprint file.

    `FingerprintIndex()` with no path loads the default file for the
    latest release from the cache directory, downloading it first if it
    is not there (1.36 GB; pass `download=False` to refuse). Loading reads
    the whole file into memory: 2 s and about 1.7 GB of RSS for the 41.2M
    fingerprints of the 2026-09-08 release; a similarity query then takes
    20 to 500 ms with four workers.
    """

    def __init__(self, path: str | Path | None = None, release: str | None = None, download: bool = True) -> None:
        try:
            from FPSim2 import FPSim2Engine  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "the fingerprints module needs FPSim2 and RDKit: pip install 'scigantic-surechembl[fingerprints]'"
            ) from exc
        if path is None:
            path = fingerprint_path(release)
            if not Path(path).exists():
                if not download:
                    raise FileNotFoundError(f"no fingerprint file at {path}; call download_fingerprints() first")
                download_fingerprints(path, release)
        self.path = Path(path)
        self.engine: Any = FPSim2Engine(str(self.path))

    @property
    def fp_type(self) -> str:
        return str(self.engine.fp_type)

    @property
    def fp_params(self) -> dict[str, Any]:
        return dict(self.engine.fp_params)

    @property
    def rdkit_version(self) -> str:
        """The RDKit version EBI built the file with; FPSim2 warns when the
        loaded RDKit differs, since fingerprints may not be bit-identical."""
        return str(self.engine.rdkit_ver)

    def __len__(self) -> int:
        return int(len(self.engine.fps))

    def similar(self, structure: str, threshold: float = 0.7, n_workers: int = 1) -> pandas.DataFrame:
        """Every compound with Tanimoto similarity >= threshold to a SMILES,
        as a DataFrame of (compound_id, similarity) sorted by similarity
        descending. All 31M compounds, no result cap."""
        import pandas as pd

        result = self.engine.similarity(structure, threshold=threshold, n_workers=n_workers)
        frame: pd.DataFrame = pd.DataFrame({"compound_id": result["mol_id"].astype("int64"), "similarity": result["coeff"].astype("float64")})
        return frame.sort_values("similarity", ascending=False).reset_index(drop=True)

    def top_k(self, structure: str, k: int = 10, threshold: float = 0.0, n_workers: int = 1) -> pandas.DataFrame:
        """The k most similar compounds to a SMILES (Tanimoto), same shape as
        similar()."""
        import pandas as pd

        result = self.engine.top_k(structure, k=k, threshold=threshold, n_workers=n_workers)
        frame: pd.DataFrame = pd.DataFrame({"compound_id": result["mol_id"].astype("int64"), "similarity": result["coeff"].astype("float64")})
        return frame

    def substructure_candidates(self, structure: str, n_workers: int = 1) -> list[int]:
        """Compound ids whose fingerprint contains the query's: the screen
        stage of a substructure search, incomplete for this Morgan file
        (see module docstring: 4,346 quinoline candidates against over
        10,000 true matches). Confirm with RDKit on the candidates'
        SMILES; use substructure_search() when recall matters."""
        result = self.engine.substructure(structure, n_workers=n_workers)
        return [int(x) for x in result.tolist()]
