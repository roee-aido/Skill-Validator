"""Secret detection must keep firing on realistic formats.

The fake values are assembled at runtime from parts, so the repository never contains a
contiguous key-like string, while the scanner still sees values in the real formats.
"""

import pytest

from skill_validator.config import ConfigError, load_agent_config
from skill_validator.scanners import search_patterns
from skill_validator.secretscan import SECRET_PATTERNS, find_secrets, mask_secrets


def j(*parts: str) -> str:
    return "".join(parts)


# kind -> a line containing a fake value in that kind's real format
REALISTIC = {
    "anthropic_key": j("key = ", "sk-", "ant-", "api03-", "A1b2C3d4" * 11, "AA"),
    "openai_style_key": j("token: ", "sk-", "Zx9" * 8),
    "aws_access_key": j("aws_access_key_id = ", "AK", "IA", "IOSFODNN7EXAMPLE"),
    "github_token": j("GH=", "gh", "p_", "aB3dE5fG7h" * 4),
    "slack_token": j("SLACK=", "xo", "xb-", "1234567890-abcdefghij"),
    "private_key": j("-----BEGIN ", "RSA PRIVATE", " KEY-----"),
    "assigned_secret": j('password = "', "hunter2", "-not-real-", '"'),
}

NOT_SECRETS = [
    "Set the ANTHROPIC_API_KEY environment variable before running.",
    "api_key_env: ANTHROPIC_API_KEY",
    'password = ""',
    "skeleton key ideas: sk-short",
    "https://example.com/docs/api-keys",
]


def test_every_pattern_has_a_realistic_case():
    assert set(REALISTIC) == {kind for kind, _ in SECRET_PATTERNS}


@pytest.mark.parametrize("kind", sorted(REALISTIC))
def test_realistic_fake_secret_is_detected_and_masked(kind):
    line = REALISTIC[kind]
    assert kind in {k for k, _, _ in find_secrets(line)}
    masked = mask_secrets(line)
    assert "[REDACTED:" in masked
    secret_part = line.split("=", 1)[-1].split(":", 1)[-1].strip().strip('"')
    if kind != "private_key":
        assert secret_part not in masked


@pytest.mark.parametrize("text", NOT_SECRETS)
def test_ordinary_text_is_not_flagged(text):
    assert find_secrets(text) == []


def test_scanner_reports_secret_without_leaking_it():
    line = REALISTIC["anthropic_key"]
    hits = search_patterns("config.py", line + "\n")
    secret_hits = [h for h in hits if h["category"] == "secret"]
    assert secret_hits and all("A1b2C3d4" not in h["text"] for h in hits)


def test_config_loader_rejects_realistic_key(tmp_path):
    from skill_validator import PROJECT_ROOT

    text = (PROJECT_ROOT / "config" / "agent.mock.yaml").read_text(encoding="utf-8")
    key = REALISTIC["anthropic_key"].split("= ", 1)[1]
    p = tmp_path / "agent.yaml"
    p.write_text(text.replace("api_key_env: null", f"api_key_env: {key}"), encoding="utf-8")
    with pytest.raises(ConfigError, match="secret-like"):
        load_agent_config(p)
