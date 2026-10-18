# Skill Review & Repair Agent

> **Status: planning stage.** This repository contains planning documents only. Nothing has been implemented, tested against a live model, or deployed.

An agent that helps people review ready-made Claude skills before using them. The user signs in and uploads a skill (`SKILL.md`, a folder, or a ZIP). The agent statically analyzes its instructions, scripts and resources without running them. It maps each meaningful part by relevance and risk, and lets the user decide what to keep, remove or fix. After the corrected copy is re-checked, the user can download it with a review report and a change log. The original stays unchanged.

The agent offers review and repair support. It does not certify that a skill is safe.

## Assignment 6 progress

| Question | Topic | Status | Where |
|---|---|---|---|
| 3 | Agent specification | Documented (planning) | [SPEC.md](SPEC.md) |
| 4 | Triggers, data flow and system architecture | Documented (planning) | [Architecture.md](Architecture.md) |
| 5 (optional) | End-to-end agent build | Planned. Not implemented, not submitted | Demo scenario: [SPEC.md §17](SPEC.md); verification: [Architecture.md §13](Architecture.md) |

## Repository map

| Path | Purpose | Status |
|---|---|---|
| [README.md](README.md) | Project overview, file map and progress | Exists |
| [SPEC.md](SPEC.md) | What the agent must do: user experience, triggers, approval flow, agent config requirements, demo scenario and acceptance criteria | Exists |
| [Architecture.md](Architecture.md) | How the system supports the spec: decision register, components, data flow diagrams, agent tools, config structure, data model, checkpoints and demo verification | Exists |
| `config/agent.yaml`, `prompts/` | Agent configuration: model, effort, prompts, tools per stage, limits and stop conditions | Planned (name and format: O13) |
| `.env.example` | Environment variable names only, no values | Planned |
| `demo/` | Synthetic test skill, bad archive, expected and actual results for Question 5 | Planned |
| Application source code | Backend, agent worker and web interface | Not started (stack: O15) |

Both planning documents are written in Hebrew.

## Decisions

All decisions are tracked in one register: [Architecture.md §2](Architecture.md).

- **Decided (D1–D9):** Anthropic as the model provider; Claude API with the SDK Tool Runner on our own backend; user sign-in; user and operations dashboards; upload-only input in the MVP; an agent configuration file without secrets; outputs available for download only after re-checking; resuming a stopped review checks the remaining budget, never resets accumulated spend, and needs explicit approval to raise the ceiling when the balance is insufficient; the agent worker may reach only the database, storage, task queue and Claude API, with every other destination blocked, including URLs found in the reviewed skill.
- **Open (O1–O15):** includes the model ID and effort level (to be set after verifying availability and testing quality and cost), hosting, data retention, budgets, and the paid budget for the Question 5 demo.

## Secrets

No API keys or other secrets belong in this repository. The configuration file names the environment variable that holds the key; the value is provided at runtime from outside the repository.
