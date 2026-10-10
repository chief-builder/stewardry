"""Content-addressed artifact store (ADR 0004).

Blobs are stored by SHA-256 under the data directory, owner-only. Who may
see an artifact is decided by its metadata row, never by knowing the hash.
Reads re-hash the content, so a modified blob is detected, not served.
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, sha256: str) -> Path:
        if not SHA256_HEX.fullmatch(sha256):
            raise ValueError("not a lowercase SHA-256 hex digest")
        return self.root / sha256[:2] / sha256

    def put(self, content: bytes) -> str:
        """Store content and return its SHA-256. Storing the same bytes twice is a no-op."""
        sha256 = hashlib.sha256(content).hexdigest()
        path = self._path(sha256)
        if path.exists():
            self.get(sha256)  # an existing blob must still match its name
            return sha256
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha256

    def get(self, sha256: str) -> bytes:
        content = self._path(sha256).read_bytes()
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ArtifactIntegrityError(f"artifact {sha256} does not match its hash")
        return content

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()
