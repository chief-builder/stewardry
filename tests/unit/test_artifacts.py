import hashlib
import stat
from pathlib import Path

import pytest

from stewardry.platform.artifacts import ArtifactIntegrityError, ArtifactStore


def test_round_trip_returns_identical_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    content = b"synthetic questionnaire bytes"
    sha = store.put(content)
    assert sha == hashlib.sha256(content).hexdigest()
    assert store.get(sha) == content
    assert store.exists(sha)


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    assert store.put(b"same") == store.put(b"same")
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_blobs_are_owner_only(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = store.put(b"private")
    blob = tmp_path / sha[:2] / sha
    assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    assert stat.S_IMODE(blob.parent.stat().st_mode) == 0o700


def test_modified_blob_is_detected_on_read(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = store.put(b"original")
    (tmp_path / sha[:2] / sha).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get(sha)
    with pytest.raises(ArtifactIntegrityError):
        store.put(b"original")  # re-storing does not silently accept the bad blob


@pytest.mark.parametrize("bad", ["../../etc/passwd", "A" * 64, "abc", ""])
def test_non_hash_names_are_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        ArtifactStore(tmp_path).get(bad)


def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("stewardry.platform.artifacts.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        ArtifactStore(tmp_path).put(b"content")
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
