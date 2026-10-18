"""Real model brain: Claude API through the Anthropic SDK Tool Runner (D2, Phase 7).

The Tool Runner drives the request -> tool -> result loop. After each model response,
before its tool calls run, this module:
- charges the real usage to the Meter (review ceilings and per-task limits, D8);
- enforces the configured turn and tool-call limits;
- stops on a refusal, on max_tokens, or if the response comes from any model other than the
  configured one (no automatic model fallback is used or accepted).

Every tool call still goes through tools.call_tool, so stage, area and path guards apply.
Guard errors are returned to the model as error tool results, never executed.
"""

import json
import os

import anthropic
from anthropic.lib.tools import BetaFunctionTool
from anthropic.lib.tools import ToolError as SDKToolError

from .agent import LimitReached, Meter, ModelFailure, Usage
from .config import AgentConfig
from .tools import TOOL_SCHEMAS, ToolContext, ToolDenied, ToolError, call_tool

REQUEST_TIMEOUT_SECONDS = 180
# Pinned on purpose: the SDK would otherwise honour ANTHROPIC_BASE_URL from the environment,
# and the agent's only external destination must be the one in infra/network-allowlist.yaml (D9).
API_BASE_URL = "https://api.anthropic.com"


def make_client(cfg: AgentConfig) -> anthropic.Anthropic:
    env = cfg.secrets.api_key_env
    key = os.environ.get(env or "")
    if not key:
        raise ModelFailure(f"the API key environment variable {env} is not set")
    return anthropic.Anthropic(api_key=key, base_url=API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2)


def _runnable_tool(ctx: ToolContext, name: str) -> BetaFunctionTool:
    schema = TOOL_SCHEMAS[name]

    def run(**kwargs):
        try:
            result = call_tool(ctx, name, kwargs)
        except (ToolDenied, ToolError) as e:
            raise SDKToolError(f"Error: {e}") from e
        return json.dumps(result, ensure_ascii=False, default=str)

    return BetaFunctionTool(
        run, name=name, description=schema["description"], input_schema=schema["input_schema"], strict=True
    )


class AnthropicBrain:
    def __init__(self, cfg: AgentConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.model_id = cfg.model.model_id
        self._client = client

    def run(self, ctx: ToolContext, cfg: AgentConfig, meter: Meter, system_prompt: str, task_prompt: str) -> None:
        client = self._client or make_client(cfg)
        stop = cfg.stop_conditions
        params = dict(
            model=cfg.model.model_id,
            max_tokens=cfg.model.max_output_tokens,
            system=system_prompt,
            tools=[_runnable_tool(ctx, name) for name in sorted(ctx.allowed_tools)],
            messages=[{"role": "user", "content": task_prompt}],
        )
        if cfg.model.effort:
            params["output_config"] = {"effort": cfg.model.effort}

        runner = client.beta.messages.tool_runner(**params)
        turns = tool_calls = 0
        try:
            for message in runner:
                turns += 1
                u = message.usage
                meter.charge(Usage(
                    input_tokens=u.input_tokens or 0,
                    output_tokens=u.output_tokens or 0,
                    cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
                    cache_read_input_tokens=u.cache_read_input_tokens or 0,
                ))
                if message.model != cfg.model.model_id:
                    raise ModelFailure(f"response came from {message.model}, not {cfg.model.model_id}")
                if message.stop_reason == "refusal":
                    category = getattr(message.stop_details, "category", None) if message.stop_details else None
                    raise ModelFailure(f"the model declined the request (refusal, category: {category})")
                if message.stop_reason == "max_tokens":
                    raise LimitReached("the response reached max_output_tokens")
                if turns > stop.max_agent_turns_per_task:
                    raise LimitReached(f"more than {stop.max_agent_turns_per_task} turns")
                tool_calls += sum(1 for block in message.content if block.type == "tool_use")
                if tool_calls > stop.max_tool_calls_per_task:
                    raise LimitReached(f"more than {stop.max_tool_calls_per_task} tool calls")
        except anthropic.APIStatusError as e:
            raise ModelFailure(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ModelFailure(f"API connection error: {e}") from e
        finally:
            runner.close()
