"""Free pre-flight checks against the Claude API. No message is generated, so nothing is billed.

    python -m skill_validator.preflight --config config/agent.yaml

1. Models API: the configured model exists for this API key; prints its limits and capabilities.
2. Token counting (free, rate-limited): the exact input size of the first request of each stage
   for the demo skill, with the system prompt and that stage's tools.

Requires the API key in the environment variable named in the configuration.
"""

import argparse
import json

from . import PROJECT_ROOT
from .anthropic_model import make_client
from .config import load_agent_config
from .tools import TOOL_SCHEMAS


def main() -> None:
    parser = argparse.ArgumentParser(description="Free pre-flight checks (Models API, token counting)")
    parser.add_argument("--config", default="config/agent.yaml")
    args = parser.parse_args()
    cfg = load_agent_config(args.config)
    client = make_client(cfg)

    model = client.models.retrieve(cfg.model.model_id)
    info = model.model_dump(exclude_none=True)
    print("Model available:", info.get("id"), "|", info.get("display_name"))
    print("  max_input_tokens:", info.get("max_input_tokens"), " max_tokens:", info.get("max_tokens"))
    caps = info.get("capabilities") or {}
    print("  capabilities:", json.dumps(caps, ensure_ascii=False)[:600])

    system = cfg.prompt_text(cfg.prompts.system)
    demo = PROJECT_ROOT / "demo" / "human-writing-skill"
    skill_text = "\n\n".join(
        f"--- {p.relative_to(demo).as_posix()} ---\n{p.read_text(encoding='utf-8')}"
        for p in sorted(demo.rglob("*")) if p.is_file()
    )
    print("\nInput tokens of the first request per stage (+ demo files as if read once):")
    for stage in ("detect_purpose", "analyze", "propose_fix", "revalidate"):
        st = cfg.stage(stage)
        tools = [{"name": n, "strict": True, **TOOL_SCHEMAS[n]} for n in st.tools]
        prompt = cfg.prompt_text(st.prompt)
        base = client.messages.count_tokens(
            model=cfg.model.model_id, system=system, tools=tools,
            messages=[{"role": "user", "content": prompt}],
        ).input_tokens
        with_files = client.messages.count_tokens(
            model=cfg.model.model_id, system=system, tools=tools,
            messages=[{"role": "user", "content": prompt + "\n\n" + skill_text}],
        ).input_tokens
        print(f"  {stage:15s} first request: {base:6d}   with demo files: {with_files:6d}")


if __name__ == "__main__":
    main()
