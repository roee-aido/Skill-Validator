"""Loading and validation of the agent configuration (SPEC section 12, Architecture section 7)."""

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, ValidationError

from . import PROJECT_ROOT, policy
from .secretscan import contains_secret

TBD = "<TBD>"


class ConfigError(Exception):
    """The configuration is missing, invalid, or not ready to run."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pricing(Strict):
    input_per_mtok: PositiveFloat
    output_per_mtok: PositiveFloat
    source: str


class ModelSettings(Strict):
    provider: Literal["anthropic", "mock"]
    model_id: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None
    max_output_tokens: PositiveInt
    pricing: Pricing


class Secrets(Strict):
    api_key_env: str | None


class Prompts(Strict):
    system: str


class StageSettings(Strict):
    prompt: str
    tools: list[str]
    read_areas: list[str]


class Budget(Strict):
    tokens: PositiveInt
    cost_usd: PositiveFloat
    processing_minutes: PositiveFloat


class Limits(Strict):
    per_task: Budget
    per_review: Budget
    max_approved_ceiling: Budget


class StopConditions(Strict):
    max_agent_turns_per_task: PositiveInt
    max_tool_calls_per_task: PositiveInt
    max_attempts_per_task: PositiveInt
    on_budget_exceeded: Literal["checkpoint_and_mark_partial"]
    on_tool_error: Literal["retry_then_checkpoint"]
    on_model_error_or_refusal: Literal["checkpoint_and_mark_partial"]


class Resume(Strict):
    required_remaining: Literal["limits.per_task"]
    on_insufficient_budget: Literal["require_user_ceiling_approval"]


class AgentConfig(Strict):
    config_version: str
    agent_name: str
    model: ModelSettings
    secrets: Secrets
    prompts: Prompts
    stages: dict[str, StageSettings]
    limits: Limits
    stop_conditions: StopConditions
    resume: Resume

    # Filled by the loader, not by the file.
    source_path: str = Field(default="", exclude=True)
    file_sha256: str = Field(default="", exclude=True)

    def stage(self, name: str) -> StageSettings:
        return self.stages[name]

    def prompt_text(self, relative: str) -> str:
        return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _walk_strings(value, path="") -> list[tuple[str, str]]:
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += _walk_strings(v, f"{path}[{i}]")
    elif isinstance(value, str):
        out.append((path, value))
    return out


def _check_policy(cfg: AgentConfig) -> list[str]:
    errors = []
    if set(cfg.stages) != set(policy.STAGES):
        errors.append(f"stages must be exactly {list(policy.STAGES)}, got {sorted(cfg.stages)}")
    for name, st in cfg.stages.items():
        if name not in policy.STAGES:
            continue
        unknown = set(st.tools) - set(policy.TOOL_NAMES)
        if unknown:
            errors.append(f"stages.{name}.tools: unknown tools {sorted(unknown)}")
        over = set(st.tools) - policy.STAGE_TOOL_CEILING[name] - unknown
        if over:
            errors.append(f"stages.{name}.tools: not allowed in this stage {sorted(over)}")
        bad_areas = set(st.read_areas) - set(policy.AREAS)
        if bad_areas:
            errors.append(f"stages.{name}.read_areas: unknown areas {sorted(bad_areas)}")
        over_areas = set(st.read_areas) - policy.STAGE_AREA_CEILING[name] - bad_areas
        if over_areas:
            errors.append(f"stages.{name}.read_areas: not allowed in this stage {sorted(over_areas)}")
        if not (PROJECT_ROOT / st.prompt).is_file():
            errors.append(f"stages.{name}.prompt: file not found {st.prompt}")
    if not (PROJECT_ROOT / cfg.prompts.system).is_file():
        errors.append(f"prompts.system: file not found {cfg.prompts.system}")

    lim = cfg.limits
    for field in ("tokens", "cost_usd", "processing_minutes"):
        a = getattr(lim.per_task, field)
        b = getattr(lim.per_review, field)
        c = getattr(lim.max_approved_ceiling, field)
        if not a <= b <= c:
            errors.append(f"limits.{field}: need per_task <= per_review <= max_approved_ceiling")

    if cfg.model.provider == "anthropic" and not cfg.secrets.api_key_env:
        errors.append("secrets.api_key_env: required for provider anthropic")
    if cfg.model.provider == "mock" and cfg.model.effort is not None:
        errors.append("model.effort: the mock model has no effort setting; use null")
    return errors


def load_agent_config(path: str | Path) -> AgentConfig:
    """Load, validate and return the agent configuration. Raises ConfigError on any problem."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        raise ConfigError(f"configuration file not found: {p}")
    raw_bytes = p.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping")

    strings = _walk_strings(data)
    tbd = [path for path, v in strings if v.strip() == TBD]
    if tbd:
        raise ConfigError("configuration has open values (<TBD>): " + ", ".join(tbd))
    secret_like = [path for path, v in strings if contains_secret(v)]
    if secret_like:
        raise ConfigError("configuration contains secret-like values: " + ", ".join(secret_like))

    try:
        cfg = AgentConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"configuration does not match the schema:\n{e}") from e

    errors = _check_policy(cfg)
    if errors:
        raise ConfigError("configuration violates policy:\n- " + "\n- ".join(errors))

    cfg.source_path = str(p)
    cfg.file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return cfg


class IntakeLimits(Strict):
    max_files: PositiveInt
    max_total_bytes: PositiveInt
    max_file_bytes: PositiveInt
    max_depth: PositiveInt
    max_compression_ratio: PositiveFloat


class SupportedFormats(Strict):
    extensions: list[str]
    file_names: list[str]


class Storage(Strict):
    root: str


class SystemConfig(Strict):
    storage: Storage
    intake: IntakeLimits
    supported_formats: SupportedFormats


def load_system_config(path: str | Path = "config/system.yaml") -> SystemConfig:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        return SystemConfig.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"invalid system configuration: {e}") from e
