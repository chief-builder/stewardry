"""Where Stewardry keeps its data.

The data directory holds the database and artifacts. It must live outside
any git work tree, so synthetic task data, drafts, and audit records can
never be committed by accident, and it must be private to the owner (0700).
"""

import os
import stat
from pathlib import Path

DATA_DIR_ENV = "STEWARDRY_DATA_DIR"


class DataDirError(RuntimeError):
    pass


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "stewardry"


def resolve_data_dir() -> Path:
    configured = os.environ.get(DATA_DIR_ENV)
    return Path(configured).expanduser() if configured else default_data_dir()


def _inside_git_work_tree(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def ensure_data_dir(path: Path) -> Path:
    """Create the data directory (mode 0700) or check an existing one."""
    path = path.expanduser().resolve()
    repo = _inside_git_work_tree(path)
    if repo is not None:
        raise DataDirError(f"data directory {path} is inside the git work tree {repo}")
    if path.exists():
        if not path.is_dir():
            raise DataDirError(f"data directory {path} is not a directory")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise DataDirError(
                f"data directory {path} is accessible to other users (mode {mode:o}); "
                f"run: chmod 700 {path}"
            )
    else:
        path.mkdir(mode=0o700, parents=True)
        path.chmod(0o700)  # mkdir's mode is filtered by the umask
    return path


def database_path(data_dir: Path) -> Path:
    return data_dir / "stewardry.db"


def artifacts_path(data_dir: Path) -> Path:
    return data_dir / "artifacts"
