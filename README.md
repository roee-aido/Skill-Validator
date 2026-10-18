# Skill Review & Repair Agent

> **Status: local implementation in progress.** Phases 0–6 run end to end on localhost with a rule-based **mock model**: no API calls, no cost, no deployment. The real model is not connected yet, and Question 5 has not been submitted.

An agent that helps people review ready-made Claude skills before using them. The user signs in and uploads a skill (`SKILL.md`, a folder, or a ZIP). The agent statically analyzes its instructions, scripts and resources without running them. It maps each meaningful part by relevance and risk, and lets the user decide what to keep, remove or fix. After the corrected copy is re-checked, the user can download it with a review report and a change log. The original stays unchanged.

The agent offers review and repair support. It does not certify that a skill is safe.

## Assignment 6 progress

| Question | Topic | Status | Where |
|---|---|---|---|
| 3 | Agent specification | Documented (planning) | [SPEC.md](SPEC.md) |
| 4 | Triggers, data flow and system architecture | Documented (planning) | [Architecture.md](Architecture.md) |
| 5 (optional) | End-to-end agent build | In progress. Phases 0–6 implemented and tested locally with a mock model. Real model not connected. Not submitted | [demo/RESULTS.md](demo/RESULTS.md), [Architecture.md §15](Architecture.md) |

| Phase | Scope | Status |
|---|---|---|
| 0 | Skeleton, agent configuration and loader, prompts | Done |
| 1 | Data model and storage areas | Done |
| 2 | Safe intake, static scanners, demo files | Done |
| 3 | Agent tools with stage and area permissions, local network guard | Done |
| 4 | Orchestrator, tasks, checkpoints, budget and resume, mock model | Done |
| 5 | Corrected copy, structural check, re-check, reports | Done |
| 6 | Hebrew RTL web interface and dashboards | Done |
| 7 | Real model (Claude API) | Not started: waiting for model choice (O1) and budget approval (O14) |
| 8 | Demo against the real model and submission | Not started |

## Repository map

| Path | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | What the agent must do: user experience, triggers, approval flow, config requirements, demo scenario, acceptance criteria (Hebrew) |
| [Architecture.md](Architecture.md) | How the system supports the spec: decision register, components, diagrams, tools, data model, checkpoints, verification, local implementation notes (Hebrew) |
| [config/agent.yaml](config/agent.yaml) | Agent configuration for the real model. Deliberately not runnable yet: open values are `<TBD>` |
| [config/agent.mock.yaml](config/agent.mock.yaml) | The same configuration with the mock model, used for local runs and tests |
| [config/system.yaml](config/system.yaml) | Intake limits (O4) and supported formats (O7) |
| [infra/network-allowlist.yaml](infra/network-allowlist.yaml) | Destinations the agent worker may reach (D9). Not part of the agent configuration |
| [prompts/](prompts/) | System prompt and one prompt per task stage |
| [skill_validator/](skill_validator/) | Application code: web app, orchestrator, worker, agent loop, tools, intake, scanners, generator, reports |
| [tests/](tests/) | 98 automated tests (97 pass, 1 skipped on Windows) |
| [demo/](demo/) | Synthetic demo skill, demo and bad archives, expected and actual results |
| [.env.example](.env.example) | Environment variable names only, no values |

## Agent configuration

The agent's model, effort, prompts, tools and permissions are defined in one versioned file. The loader validates it at startup and refuses to run with any `<TBD>` value, an unknown tool, a stage that widens its tool or area limits, or a secret-like value.

| Section | What it defines |
|---|---|
| `model` | Provider, model ID, effort level (only if the model supports it), max output tokens, prices with source and date |
| `secrets.api_key_env` | The name of the environment variable that holds the API key. Never the value |
| `prompts.system` | The system prompt: trust boundary, how to work, writing style |
| `stages.<stage>` | For each of `detect_purpose`, `analyze`, `propose_fix`, `revalidate`: its prompt, allowed tools and readable areas |
| `limits` | Per-task limits, cumulative per-review ceilings (never reset), and the maximum ceiling a user may approve |
| `stop_conditions` | Max turns, tool calls and attempts per task, and what happens on budget, tool or model errors |
| `resume` | Fixed rule (D8): resuming checks the balance and needs explicit approval when it is insufficient |

**Tools** (closed list, enforced in code; the configuration can only narrow it): `list_files`, `read_file`, `search_patterns`, `get_references`, `compare_versions` (re-check only), `record_purpose`, `record_finding`, `propose_patch`, `record_validation`. No tool runs code, reaches the network or writes files. The `candidate` area (the corrected copy) is readable only in the `revalidate` stage.

**Model and effort:** open decision O1. They will be set only after verifying availability, capabilities and prices against Anthropic's official documentation and running a small, approved experiment.

## Run locally (mock model, no API calls)

Requires Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pytest
```

Start the worker and the web interface in two terminals:

```bash
.venv/Scripts/python -m skill_validator.worker --config config/agent.mock.yaml
```

```bash
.venv/Scripts/python -m skill_validator.web --config config/agent.mock.yaml --port 8765
```

Open http://127.0.0.1:8765, enter any email, and use the one-time login link printed in the web server's terminal. Upload `demo/human-writing-skill.zip` and follow [demo/EXPECTED.md](demo/EXPECTED.md). To see the operations dashboard, set `SV_ADMIN_EMAILS` to your login email before starting the web server.

Local data (database, uploaded skills, outputs) is written to `data/`, which is git-ignored.

## Known limitations

- **Network restriction (D9) is local only.** The worker installs a Python audit hook that allows only `api.anthropic.com:443` and blocks starting processes. This is a local protection layer, not infrastructure-level isolation and not a full replacement for it. See [Architecture.md §15](Architecture.md).
- **Mock model.** Findings come from fixed rules over scanner hits, not from a language model. The mock proves the flow, permissions and controls, not the quality of the analysis. Token counts and costs shown are simulated.
- **Local stand-ins:** SQLite instead of a server database, local folders instead of object storage, a database table as the task queue, and one-time console login links instead of an identity provider.

## Decisions

All decisions are tracked in one register: [Architecture.md §2](Architecture.md).

- **Decided (D1–D9):** Anthropic as the model provider; Claude API with the SDK Tool Runner on our own backend; user sign-in; user and operations dashboards; upload-only input in the MVP; an agent configuration file without secrets; outputs available for download only after re-checking; resuming a stopped review checks the remaining budget, never resets accumulated spend, and needs explicit approval to raise the ceiling when the balance is insufficient; the agent worker may reach only the database, storage, task queue and Claude API, with every other destination blocked, including URLs found in the reviewed skill.
- **Open (O1–O15):** includes the model ID and effort level, hosting, data retention, budgets, and the paid budget for the Question 5 demo.

## Secrets

No API keys or other secrets belong in this repository. The configuration file names the environment variable that holds the key; the value is provided at runtime from outside the repository. `.env` and `data/` are git-ignored. The only key-like strings in the repository are obviously fake test values (`sk-ant-FAKE-...`) used to test secret detection.
