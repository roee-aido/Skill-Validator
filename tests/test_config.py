import copy
from pathlib import Path

import pytest
import yaml

from skill_validator import PROJECT_ROOT
from skill_validator.config import ConfigError, load_agent_config, load_system_config

MOCK = PROJECT_ROOT / "config" / "agent.mock.yaml"


def write_variant(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(MOCK.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    mutate(data)
    p = tmp_path / "agent.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_mock_config_loads():
    cfg = load_agent_config(MOCK)
    assert cfg.model.provider == "mock"
    assert cfg.stage("revalidate").read_areas == ["source", "candidate"]
    assert len(cfg.file_sha256) == 64


def test_real_config_has_the_approved_experiment_values():
    cfg = load_agent_config(PROJECT_ROOT / "config" / "agent.yaml")
    assert cfg.model.provider == "anthropic"
    assert cfg.model.model_id == "claude-opus-5-5"
    assert cfg.model.effort == "medium"
    assert cfg.secrets.api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.limits.per_review.cost_usd == 3.0
    assert cfg.limits.max_approved_ceiling.cost_usd == 5.0
    assert (cfg.model.pricing.input_per_mtok, cfg.model.pricing.output_per_mtok) == (4.0, 20.0)


def test_open_values_block_loading(tmp_path):
    p = write_variant(tmp_path, lambda d: d["model"].update(model_id="<TBD>"))
    with pytest.raises(ConfigError, match="<TBD>"):
        load_agent_config(p)


def test_unknown_tool_is_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["stages"]["analyze"]["tools"].append("run_shell"))
    with pytest.raises(ConfigError, match="unknown tools"):
        load_agent_config(p)


def test_config_cannot_widen_stage_tools(tmp_path):
    p = write_variant(tmp_path, lambda d: d["stages"]["analyze"]["tools"].append("compare_versions"))
    with pytest.raises(ConfigError, match="not allowed in this stage"):
        load_agent_config(p)


def test_candidate_area_only_in_revalidate(tmp_path):
    p = write_variant(tmp_path, lambda d: d["stages"]["analyze"]["read_areas"].append("candidate"))
    with pytest.raises(ConfigError, match="read_areas: not allowed"):
        load_agent_config(p)


def test_config_may_narrow_tools(tmp_path):
    p = write_variant(tmp_path, lambda d: d["stages"]["analyze"]["tools"].remove("get_references"))
    cfg = load_agent_config(p)
    assert "get_references" not in cfg.stage("analyze").tools


def test_resume_rule_is_fixed(tmp_path):
    p = write_variant(tmp_path, lambda d: d["resume"].update(on_insufficient_budget="continue"))
    with pytest.raises(ConfigError, match="schema"):
        load_agent_config(p)


def test_secret_like_value_is_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["secrets"].update(api_key_env="sk-ant-FAKE-test-value-abcdefgh"))
    with pytest.raises(ConfigError, match="secret-like"):
        load_agent_config(p)


def test_missing_stage_is_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["stages"].pop("propose_fix"))
    with pytest.raises(ConfigError, match="stages must be exactly"):
        load_agent_config(p)


def test_limits_must_be_ordered(tmp_path):
    p = write_variant(tmp_path, lambda d: d["limits"]["per_review"].update(cost_usd=99.0))
    with pytest.raises(ConfigError, match="per_task <= per_review <= max_approved_ceiling"):
        load_agent_config(p)


def test_unexpected_field_is_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d.update(network={"allow": ["*"]}))
    with pytest.raises(ConfigError, match="schema"):
        load_agent_config(p)


def test_system_config_loads():
    sc = load_system_config()
    assert ".md" in sc.supported_formats.extensions
    assert sc.intake.max_files == 100
