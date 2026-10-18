"""Detection and masking of secrets (SPEC section 10, O9)."""

import re

SECRET_PATTERNS = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(api[_\-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
        ),
    ),
]


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """Return (kind, start, end) for every secret-like match."""
    hits = []
    for kind, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((kind, m.start(), m.end()))
    return sorted(hits, key=lambda h: h[1])


def mask_secrets(text: str) -> str:
    """Replace secret-like values with a marker. Used before display, reports and the model."""
    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))
