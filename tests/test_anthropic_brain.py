"""The real-model path (SDK Tool Runner) tested offline with a mocked HTTP transport.

No network and no cost: every response below is a canned Messages API reply.
"""

import json

import anthropic
import httpx2 as httpx
import pytest

from skill_validator import PROJECT_ROOT
from skill_validator.anthropic_model import AnthropicBrain
from skill_validator.config import load_agent_config
from skill_validator.db import AgentRun, ReviewSession, ReviewTask
from skill_validator.orchestrator import run_until_idle, start_review

from .conftest import DEMO_ZIP

MODEL = "claude-opus-5-5"


def msg(content, stop_reason="tool_use", usage=(1000, 200), model=MODEL, stop_details=None):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def tool_use(name, inp, i=1):
    return {"type": "tool_use", "id": f"toolu_{name}_{i}", "name": name, "input": inp}


PURPOSE = {"purpose": "שכתוב טקסט כך שיישמע טבעי", "evidence": "SKILL.md:3", "clarification_question": ""}
HAPPY = [
    msg([tool_use("list_files", {})]),
    msg([tool_use("read_file", {"path": "SKILL.md"})]),
    msg([tool_use("record_purpose", PURPOSE)]),
    msg([{"type": "text", "text": "done"}], stop_reason="end_turn", usage=(1500, 50)),
]


class Fake:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def client(self):
        def handler(request: httpx.Request):
            self.requests.append(json.loads(request.content))
            r = self.responses.pop(0)
            if isinstance(r, int):
                return httpx.Response(r, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
            return httpx.Response(200, json=r)

        return anthropic.Anthropic(
            api_key="offline-test-value", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


@pytest.fixture
def real_cfg():
    return load_agent_config(PROJECT_ROOT / "config" / "agent.yaml")


def setup(svc, real_cfg, responses, total_cap=5.0):
    fake = Fake(responses)
    client = fake.client()
    svc.agent_cfg = real_cfg
    svc.total_cap_usd = total_cap
    svc.brain_factory = lambda cfg: AnthropicBrain(cfg, client=client)
    return fake


def upload(svc, db, user):
    return start_review(svc, db, user, [(DEMO_ZIP.name, DEMO_ZIP.read_bytes())], "", True)


def tool_results(request):
    return [b for m in request["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result"]


def test_purpose_stage_runs_through_the_tool_runner(svc, svc_db, svc_user, real_cfg):
    fake = setup(svc, real_cfg, HAPPY)
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.purpose_status == "proposed" and review.purpose == PURPOSE["purpose"]
    assert review.status == "waiting_user"  # the review stops for the user after the purpose stage

    first = fake.requests[0]
    assert first["model"] == MODEL
    assert first["output_config"] == {"effort": "medium"}
    assert first["max_tokens"] == 16000
    assert "fallbacks" not in first  # no automatic model fallback
    assert sorted(t["name"] for t in first["tools"]) == ["list_files", "read_file", "record_purpose"]
    assert all(t["strict"] is True for t in first["tools"])
    assert "Skill Validator" in json.dumps(first["system"])

    results = tool_results(fake.requests[1])
    assert results and not results[0].get("is_error")
    assert "SKILL.md" in json.dumps(results[0]["content"])

    run = svc_db.query(AgentRun).filter_by(session_id=review.id).one()
    assert run.model_id == MODEL and run.outcome == "completed"
    assert (run.input_tokens, run.output_tokens) == (4500, 650)
    assert run.cost_usd == pytest.approx((4500 * 4.0 + 650 * 20.0) / 1e6)
    assert review.spent_cost_total == pytest.approx(run.cost_usd)
    assert [c["tool"] for c in run.tool_calls] == ["list_files", "read_file", "record_purpose"]


def test_guards_apply_to_real_model_calls(svc, svc_db, svc_user, real_cfg):
    fake = setup(svc, real_cfg, [
        msg([tool_use("read_file", {"path": "SKILL.md", "area": "candidate"}),
             tool_use("read_file", {"path": "../../escape.txt"}, i=2)]),
        msg([tool_use("record_purpose", PURPOSE)]),
        msg([{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    results = tool_results(fake.requests[1])
    assert len(results) == 2 and all(r.get("is_error") for r in results)
    text = json.dumps(results)
    assert "not readable in stage" in text and "escapes" in text
    run = svc_db.query(AgentRun).filter_by(session_id=review.id).one()
    assert [c["status"] for c in run.tool_calls][:2] == ["denied", "denied"]


@pytest.mark.parametrize(
    "responses,reason_text",
    [
        ([msg([], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})], "refusal"),
        ([msg([tool_use("list_files", {})], model="claude-opus-5")], "response came from"),
        ([500], "API error 500"),
    ],
)
def test_refusal_other_model_or_api_error_stops_the_review(svc, svc_db, svc_user, real_cfg, responses, reason_text):
    setup(svc, real_cfg, responses)
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "stopped" and review.stop_reason == "model_error"
    run = svc_db.query(AgentRun).filter_by(session_id=review.id).one()
    assert run.outcome == "model_error" and reason_text in run.error


def test_total_cap_blocks_a_task_before_any_request(svc, svc_db, svc_user, real_cfg):
    fake = setup(svc, real_cfg, HAPPY, total_cap=0.5)  # below one worst-case task
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "stopped" and review.stop_reason == "total_budget_cap"
    assert fake.requests == []
    assert svc_db.query(AgentRun).count() == 0


def test_tool_call_limit_ends_the_task(svc, svc_db, svc_user, real_cfg):
    cfg = real_cfg.model_copy(update={"stop_conditions": real_cfg.stop_conditions.model_copy(
        update={"max_tool_calls_per_task": 1, "max_attempts_per_task": 1})})
    two_calls = msg([tool_use("list_files", {}), tool_use("read_file", {"path": "SKILL.md"}, i=2)])
    fake = setup(svc, cfg, [two_calls])
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    task = svc_db.query(ReviewTask).filter_by(session_id=review.id).one()
    assert task.status == "skipped"
    run = svc_db.query(AgentRun).filter_by(task_id=task.id).one()
    assert run.outcome == "limit_reached" and run.tool_calls == []  # stopped before running the calls
    assert len(fake.requests) == 1


def test_real_model_requires_a_total_cap(monkeypatch):
    from skill_validator.services import build_services

    monkeypatch.delenv("SV_TOTAL_BUDGET_USD", raising=False)
    with pytest.raises(SystemExit, match="SV_TOTAL_BUDGET_USD"):
        build_services("config/agent.yaml", data_root="data/never-created")


def test_missing_api_key_stops_without_a_request(svc, svc_db, svc_user, real_cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    svc.agent_cfg, svc.total_cap_usd = real_cfg, 5.0
    svc.brain_factory = lambda cfg: AnthropicBrain(cfg)  # builds its own client from the environment
    review = upload(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "stopped"
    assert "ANTHROPIC_API_KEY" in svc_db.query(AgentRun).one().error
