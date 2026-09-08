import json

import pytest

from scigantic_surechembl.cli import main


def test_cli_compound(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["compound", "SCHEMBL1353"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == 1353
    assert out["inchi_key"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert "raw" not in out


def test_cli_patent_omits_full_text_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["patent", "US10000000B2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["title"].startswith("Coherent LADAR")
    assert "description" not in out and "claims" not in out
    assert out["published"] == "2018-06-19"


def test_cli_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["patents-for", "1353", "--count"]) == 0
    assert int(capsys.readouterr().out) > 100_000


def test_cli_bad_input_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["compound", "foo"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: not a SureChEMBL compound id")
    assert "Traceback" not in err


def test_cli_xrefs_forms(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["xrefs", "chembl:CHEMBL25"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert 1353 in out["surechembl"] and out["pubchem_cid"] == [2244]
    assert main(["xrefs", "SCHEMBL1353"]) == 0
    assert json.loads(capsys.readouterr().out)["chembl"] == ["CHEMBL25"]
