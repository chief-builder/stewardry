import stat
from pathlib import Path

import pytest

from stewardry.config import (
    DataDirError,
    default_data_dir,
    ensure_data_dir,
    resolve_data_dir,
)


def test_new_data_dir_is_owner_only(tmp_path: Path) -> None:
    created = ensure_data_dir(tmp_path / "data")
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_existing_open_data_dir_is_refused(tmp_path: Path) -> None:
    open_dir = tmp_path / "data"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    with pytest.raises(DataDirError, match="accessible to other users"):
        ensure_data_dir(open_dir)


def test_data_dir_inside_a_git_work_tree_is_refused(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(DataDirError, match="inside the git work tree"):
        ensure_data_dir(tmp_path / "repo" / "data")


def test_data_dir_must_be_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "data"
    file.write_text("")
    with pytest.raises(DataDirError, match="not a directory"):
        ensure_data_dir(file)


def test_env_var_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEWARDRY_DATA_DIR", str(tmp_path / "custom"))
    assert resolve_data_dir() == tmp_path / "custom"


def test_default_follows_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARDRY_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert resolve_data_dir() == default_data_dir() == tmp_path / "stewardry"


def test_default_without_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_data_dir() == Path.home() / ".local" / "share" / "stewardry"
