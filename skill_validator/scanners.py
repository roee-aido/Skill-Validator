"""Deterministic static scanners: risk patterns, secrets, references and markdown parts.

Runs as a library inside the calling process (no network). A pattern hit is evidence,
not a verdict: the agent decides relevance and safety from the purpose and context.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .secretscan import find_secrets, mask_secrets

CODE_SUFFIXES = {".py", ".sh", ".ps1", ".js", ".ts"}


@dataclass(frozen=True)
class Pattern:
    category: str
    regex: re.Pattern
    applies_to: str  # code / text / any
    description: str


PATTERNS = [
    Pattern(
        "network_send",
        re.compile(
            r"\brequests\.(post|put|get|patch)\b|\burllib\.request\b|\bhttpx\.|\bhttp\.client\b"
            r"|\bsocket\.socket\b|\bfetch\s*\(|\bcurl\s|\bwget\s|Invoke-WebRequest|Invoke-RestMethod"
        ),
        "code",
        "קוד ששולח או מקבל מידע ברשת",
    ),
    Pattern("external_url", re.compile(r"https?://[^\s)\"'>`]+"), "any", "כתובת חיצונית"),
    Pattern(
        "network_instruction",
        re.compile(
            r"(?i)\b(send|upload|post|transmit|forward)\b[^.\n]{0,80}\b(to|into)\b[^.\n]{0,40}"
            r"(https?://|server|endpoint|api|webhook)"
        ),
        "text",
        "הוראה לשלוח מידע ליעד חיצוני",
    ),
    Pattern(
        "broad_file_access",
        re.compile(
            r"(?i)(~/|%USERPROFILE%|\$HOME\b|/home/|C:\\Users\\)\S*"
            r"|(?i:\b(all|every|entire|whole)\b[^.\n]{0,30}\b(files?|folders?|director(y|ies)|documents)\b)"
            r"|os\.walk\(|\brglob\(|glob\([^)]*\*\*"
        ),
        "any",
        "גישה רחבה לקבצים או לתיקיות של המשתמש",
    ),
    Pattern(
        "concealment",
        re.compile(
            r"(?i)\b(do not|don't|never)\s+(tell|inform|mention|show|notify)\b[^.\n]{0,40}\b(user|them)\b"
            r"|without (telling|informing|notifying)|keep (this|it) (hidden|secret)|\bhide (this|the|that)\b"
            r"|אל תספר|אל תגלה|בלי לספר|להסתיר מהמשתמש"
        ),
        "text",
        "הוראה להסתיר פעולה מהמשתמש",
    ),
    Pattern(
        "reviewer_manipulation",
        re.compile(
            r"(?i)\b(note|message|instructions?)\s+(to|for)\s+(the\s+)?(reviewer|auditor|validator|ai|assistant|model)\b"
            r"|\bignore\s+(all\s+|any\s+)?(previous\s+|prior\s+|the\s+|these\s+)?(findings|instructions|warnings|issues)\b"
            r"|\bskip\s+(reviewing|checking|scanning)\b|התעלם מ"
        ),
        "text",
        "ניסיון להשפיע על הבודק",
    ),
    Pattern(
        "code_execution",
        re.compile(
            r"\bsubprocess\.|\bos\.system\(|\beval\(|\bexec\(|\bpip install\b|\bnpm install\b"
            r"|\|\s*(ba)?sh\b|Start-Process|Invoke-Expression"
        ),
        "any",
        "הרצת קוד או התקנת חבילות",
    ),
    Pattern(
        "destructive",
        re.compile(r"\brm\s+-rf\b|shutil\.rmtree|os\.remove\(|os\.unlink\(|Remove-Item|\bdel\s+/[sq]"),
        "any",
        "מחיקת קבצים",
    ),
    Pattern(
        "permission_change",
        re.compile(r"\bchmod\b|\bchown\b|icacls|Set-ExecutionPolicy|\bsudo\s"),
        "any",
        "שינוי הרשאות",
    ),
    Pattern(
        "obfuscation",
        re.compile(r"base64\.b64decode|\batob\(|fromCharCode|(\\x[0-9a-fA-F]{2}){8,}"),
        "any",
        "קוד מעורפל או מקודד",
    ),
    Pattern(
        "persistence",
        re.compile(r"crontab|schtasks|launchctl|\.bashrc|\.zshrc|systemctl enable"),
        "any",
        "פעולה מתמשכת במערכת",
    ),
]

SECRET_DESCRIPTION = "ערך שנראה כמו סוד או מפתח"


def is_code(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in CODE_SUFFIXES


def search_patterns(path: str, text: str) -> list[dict]:
    """Return pattern hits with masked line text. Line numbers are 1-based."""
    kind = "code" if is_code(path) else "text"
    hits = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for p in PATTERNS:
            if p.applies_to not in ("any", kind):
                continue
            if p.regex.search(line):
                hits.append(
                    {
                        "category": p.category,
                        "path": path,
                        "line": lineno,
                        "text": mask_secrets(line.strip())[:300],
                        "description": p.description,
                    }
                )
        if find_secrets(line):
            hits.append(
                {
                    "category": "secret",
                    "path": path,
                    "line": lineno,
                    "text": mask_secrets(line.strip())[:300],
                    "description": SECRET_DESCRIPTION,
                }
            )
    return hits


_URL = re.compile(r"https?://\S+")
_PATH_TOKEN = re.compile(
    r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\.(?:md|txt|py|sh|ps1|js|ts|json|ya?ml|toml))(?![\w/])"
)


def _candidate_refs(line: str) -> list[str]:
    return [m.group(1) for m in _PATH_TOKEN.finditer(_URL.sub(" ", line))]


def _resolve_ref(from_path: str, token: str, existing: set[str]) -> str | None:
    token = token.lstrip("./") if token.startswith("./") else token
    base = PurePosixPath(from_path).parent
    for cand in (str(PurePosixPath(base, token)) if str(base) != "." else token, token):
        if cand in existing:
            return cand
    return None


def reference_graph(files: dict[str, str]) -> dict:
    """Build references between files of one package.

    Returns {"edges": [{from, line, target}], "broken": [{from, line, token}]}.
    A token is reported as broken only if it contains a "/" (a path, not a bare file name).
    """
    existing = set(files)
    edges, broken = [], []
    for path, text in files.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in _candidate_refs(line):
                target = _resolve_ref(path, token, existing)
                if target and target != path:
                    edges.append({"from": path, "line": lineno, "target": target})
                elif target is None and "/" in token:
                    broken.append({"from": path, "line": lineno, "token": token})
    return {"edges": edges, "broken": broken}


def references_for(path: str, graph: dict) -> dict:
    return {
        "outgoing": [e for e in graph["edges"] if e["from"] == path],
        "incoming": [e for e in graph["edges"] if e["target"] == path],
        "broken": [b for b in graph["broken"] if b["from"] == path],
    }


def markdown_parts(text: str) -> list[dict]:
    """Split markdown into meaningful parts: frontmatter, then blocks under each heading.

    Each part: {"kind", "heading", "start", "end"} with 1-based inclusive line numbers.
    Blank lines split blocks; a heading starts a new section.
    """
    lines = text.splitlines()
    parts = []
    i = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                parts.append({"kind": "frontmatter", "heading": "frontmatter", "start": 1, "end": j + 1})
                i = j + 1
                break
    heading = ""
    block_start = None
    for idx in range(i, len(lines)):
        line = lines[idx]
        lineno = idx + 1
        if line.lstrip().startswith("#"):
            if block_start is not None:
                parts.append({"kind": "block", "heading": heading, "start": block_start, "end": lineno - 1})
                block_start = None
            heading = line.lstrip("# ").strip()
            parts.append({"kind": "heading", "heading": heading, "start": lineno, "end": lineno})
        elif not line.strip():
            if block_start is not None:
                parts.append({"kind": "block", "heading": heading, "start": block_start, "end": lineno - 1})
                block_start = None
        elif block_start is None:
            block_start = lineno
    if block_start is not None:
        parts.append({"kind": "block", "heading": heading, "start": block_start, "end": len(lines)})
    return parts
