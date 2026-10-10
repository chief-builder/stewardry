from pathlib import Path

import pytest

from stewardry import __version__
from stewardry.cli import main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"stewardry {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: stewardry" in capsys.readouterr().out


def test_db_upgrade_creates_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "upgrade"]) == 0
    assert (tmp_path / "data" / "stewardry.db").is_file()
    assert "latest schema" in capsys.readouterr().out


def test_db_upgrade_refuses_an_unsafe_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "upgrade"]) == 2
    assert "inside the git work tree" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()
