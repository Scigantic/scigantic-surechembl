"""The FingerprintIndex wrapper against a five-compound FPSim2 file built
here, so CI never downloads the 1.36 GB release file. The real file was
loaded and queried by hand (see README, Fingerprints)."""

from pathlib import Path

import pytest

pytest.importorskip("FPSim2")
pytest.importorskip("rdkit")

from scigantic_surechembl import fingerprints  # noqa: E402

MOLS = [
    ["CC(=O)Oc1ccccc1C(=O)O", 1353],  # aspirin
    ["Cn1c(=O)c2c(ncn2C)n(C)c1=O", 5671],  # caffeine
    ["CCO", 463],
    ["c1ccc2ncccc2c1", 2774],  # quinoline
    ["OC(=O)c1ccccc1O", 3636],  # salicylic acid
]


@pytest.fixture(scope="module")
def small_index(tmp_path_factory: pytest.TempPathFactory) -> fingerprints.FingerprintIndex:
    from FPSim2.io import create_db_file  # type: ignore[import-untyped]

    path = Path(str(tmp_path_factory.mktemp("fps"))) / "five.h5"
    create_db_file(mols_source=MOLS, filename=str(path), mol_format="smiles", fp_type="Morgan", fp_params={"radius": 2, "fpSize": 2048})
    return fingerprints.FingerprintIndex(path)


def test_index_metadata_and_size(small_index: fingerprints.FingerprintIndex) -> None:
    assert len(small_index) == 5
    assert small_index.fp_type == "Morgan"
    assert small_index.fp_params["radius"] == 2
    assert small_index.rdkit_version


def test_similar_and_top_k_are_keyed_by_compound_id(small_index: fingerprints.FingerprintIndex) -> None:
    frame = small_index.similar("CC(=O)Oc1ccccc1C(=O)O", threshold=0.3)
    assert list(frame.columns) == ["compound_id", "similarity"]
    assert frame.iloc[0]["compound_id"] == 1353 and frame.iloc[0]["similarity"] == pytest.approx(1.0)
    assert 3636 in set(frame["compound_id"])  # salicylic acid, the close neighbour
    assert frame["similarity"].is_monotonic_decreasing
    top = small_index.top_k("CC(=O)Oc1ccccc1C(=O)O", k=2)
    assert top["compound_id"].tolist() == [1353, 3636]


def test_substructure_candidates_is_a_screen(small_index: fingerprints.FingerprintIndex) -> None:
    cands = small_index.substructure_candidates("c1ccccc1")
    assert {1353, 3636} <= set(cands)  # a Morgan screen can miss quinoline; named "candidates" for that reason


def test_missing_file_without_download_is_a_clear_error(tmp_path: pytest.TempPathFactory) -> None:
    from scigantic_surechembl import cache

    cache.enable_cache(str(tmp_path))
    with pytest.raises(FileNotFoundError, match="download_fingerprints"):
        fingerprints.FingerprintIndex(download=False)
    assert fingerprints.fingerprint_url("2026-09-08").endswith("/2026-09-08/fpsim2_fingerprints.h5")
