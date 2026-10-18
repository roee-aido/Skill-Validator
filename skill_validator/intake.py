"""Deterministic intake (SPEC section 10, Architecture section 6). No model involved.

Every check runs before anything is written. Archive members are read one by one and
written through Storage.resolve, never with ZipFile.extractall.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from .config import IntakeLimits, SupportedFormats
from .storage import PathViolation, Storage, normalize_relative, sha256_bytes, tree_hash, write_read_only


class IntakeRejected(Exception):
    """The upload was rejected. Nothing was written to storage."""


@dataclass
class IntakeFile:
    path: str
    data: bytes
    type: str
    supported: bool

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def hash(self) -> str:
        return sha256_bytes(self.data)


@dataclass
class IntakeResult:
    files: list[IntakeFile]
    source_hash: str
    skill_name: str


def classify(path: str, data: bytes, formats: SupportedFormats) -> tuple[str, bool]:
    name = PurePosixPath(path).name
    suffix = PurePosixPath(path).suffix.lower()
    known = suffix in formats.extensions or name in formats.file_names
    if b"\x00" in data:
        return "binary", False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", False
    return (suffix or name), known


def _members_from_zip(blob: bytes, limits: IntakeLimits) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise IntakeRejected("הקובץ אינו ארכיון ZIP תקין") from e

    infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    if len(infos) > limits.max_files:
        raise IntakeRejected(f"הארכיון מכיל יותר מ־{limits.max_files} קבצים")

    total = 0
    planned = []
    for info in infos:
        if info.flag_bits & 0x1:
            raise IntakeRejected(f"קובץ מוצפן אינו נתמך: {info.filename}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise IntakeRejected(f"קישור סמלי אינו מותר: {info.filename}")
        try:
            rel = normalize_relative(info.filename)
        except PathViolation as e:
            raise IntakeRejected(f"נתיב חורג או לא תקין בארכיון: {info.filename}") from e
        if info.file_size > limits.max_file_bytes:
            raise IntakeRejected(f"קובץ גדול מדי: {rel}")
        if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
            raise IntakeRejected(f"יחס דחיסה חריג (חשד ל־zip bomb): {rel}")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise IntakeRejected("הגודל הכולל של הארכיון אחרי פתיחה חורג מהמגבלה")
        planned.append((rel, info))

    members = []
    for rel, info in planned:
        with zf.open(info) as fh:
            data = fh.read(limits.max_file_bytes + 1)
        if len(data) != info.file_size or len(data) > limits.max_file_bytes:
            raise IntakeRejected(f"גודל הקובץ בארכיון אינו תואם: {rel}")
        members.append((rel, data))
    return members


def _strip_common_root(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    firsts = {m[0].split("/", 1)[0] for m in members}
    if len(firsts) == 1 and all("/" in m[0] for m in members):
        return [(m[0].split("/", 1)[1], m[1]) for m in members]
    return members


def _skill_name(files: list[IntakeFile], fallback: str) -> str:
    for f in files:
        if f.path == "SKILL.md":
            m = re.search(r"(?m)^name:\s*(.+)$", f.data.decode("utf-8", "replace"))
            if m:
                return m.group(1).strip()[:200]
    return fallback[:200]


def collect_upload(
    uploads: list[tuple[str, bytes]], limits: IntakeLimits, formats: SupportedFormats
) -> IntakeResult:
    """Validate an upload: one ZIP, one SKILL.md, or the files of a folder (names with relative paths)."""
    if not uploads:
        raise IntakeRejected("לא הועלה קובץ")

    if len(uploads) == 1 and uploads[0][0].lower().endswith(".zip"):
        fallback = PurePosixPath(uploads[0][0].replace("\\", "/")).stem
        members = _members_from_zip(uploads[0][1], limits)
    else:
        fallback = "skill"
        if len(uploads) > limits.max_files:
            raise IntakeRejected(f"הועלו יותר מ־{limits.max_files} קבצים")
        members = []
        total = 0
        for name, data in uploads:
            try:
                rel = normalize_relative(name)
            except PathViolation as e:
                raise IntakeRejected(f"נתיב לא תקין: {name}") from e
            if len(data) > limits.max_file_bytes:
                raise IntakeRejected(f"קובץ גדול מדי: {rel}")
            total += len(data)
            if total > limits.max_total_bytes:
                raise IntakeRejected("הגודל הכולל חורג מהמגבלה")
            members.append((rel, data))

    members = _strip_common_root(members)
    seen = set()
    for rel, _ in members:
        if len(PurePosixPath(rel).parts) > limits.max_depth:
            raise IntakeRejected(f"עומק תיקיות חורג מהמגבלה: {rel}")
        if rel.lower() in seen:
            raise IntakeRejected(f"נתיב כפול: {rel}")
        seen.add(rel.lower())
    if "SKILL.md" not in {rel for rel, _ in members}:
        raise IntakeRejected("לא נמצא קובץ SKILL.md בשורש החבילה")

    files = []
    for rel, data in members:
        ftype, supported = classify(rel, data, formats)
        files.append(IntakeFile(path=rel, data=data, type=ftype, supported=supported))
    source_hash = tree_hash([(f.path, f.hash) for f in files])
    return IntakeResult(files=files, source_hash=source_hash, skill_name=_skill_name(files, fallback))


def write_source(storage: Storage, user_id: str, session_id: str, result: IntakeResult) -> None:
    """Write validated files to the quarantine area as read-only."""
    for f in result.files:
        write_read_only(storage.resolve("source", user_id, session_id, f.path), f.data)
