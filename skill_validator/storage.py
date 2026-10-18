"""Storage areas (Architecture.md section 8): quarantine, candidate and delivery, per user and review."""

import hashlib
import shutil
from pathlib import Path, PurePosixPath

# Agent tools address areas by name; "source" is the quarantine area.
AREA_DIRS = {"source": "quarantine", "candidate": "candidate", "delivery": "delivery"}


class PathViolation(Exception):
    """A relative path tried to leave its area, or was otherwise unsafe."""


def normalize_relative(rel: str) -> str:
    """Validate a relative path from a user, an archive or the model. Returns a clean POSIX path."""
    if not isinstance(rel, str) or not rel.strip():
        raise PathViolation("empty path")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise PathViolation(f"absolute path not allowed: {rel}")
    parts = PurePosixPath(rel).parts
    if any(p in ("..", "") for p in parts):
        raise PathViolation(f"path escapes its area: {rel}")
    clean = [p for p in parts if p != "."]
    if not clean:
        raise PathViolation("empty path")
    return "/".join(clean)


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def area_dir(self, area: str, user_id: str, session_id: str) -> Path:
        if area not in AREA_DIRS:
            raise PathViolation(f"unknown area: {area}")
        return self.root / "storage" / AREA_DIRS[area] / user_id / session_id

    def resolve(self, area: str, user_id: str, session_id: str, rel: str) -> Path:
        """Return an absolute path inside the area, or raise PathViolation."""
        base = self.area_dir(area, user_id, session_id).resolve()
        target = (base / normalize_relative(rel)).resolve()
        if base != target and base not in target.parents:
            raise PathViolation(f"path escapes its area: {rel}")
        # Reject symlinks anywhere between the area root and the target.
        probe = target
        while probe != base:
            if probe.is_symlink():
                raise PathViolation(f"symbolic link not allowed: {rel}")
            probe = probe.parent
        return target

    def list_files(self, area: str, user_id: str, session_id: str) -> list[str]:
        base = self.area_dir(area, user_id, session_id)
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and not p.is_symlink()
        )

    def delete_review(self, user_id: str, session_id: str) -> None:
        for area in AREA_DIRS:
            d = self.area_dir(area, user_id, session_id)
            if d.exists():
                shutil.rmtree(d)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(entries: list[tuple[str, str]]) -> str:
    """Hash of a set of (relative_path, file_hash) pairs, independent of order."""
    h = hashlib.sha256()
    for path, fh in sorted(entries):
        h.update(f"{path}\0{fh}\n".encode())
    return h.hexdigest()


def area_hash(storage: Storage, area: str, user_id: str, session_id: str) -> str:
    base = storage.area_dir(area, user_id, session_id)
    entries = [(rel, sha256_bytes((base / rel).read_bytes())) for rel in storage.list_files(area, user_id, session_id)]
    return tree_hash(entries)
