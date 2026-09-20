# Task Board — Computer-Use Automation System

Epic: build an end-to-end vertical slice — LLM discovers a flow in a legacy-style
bank app, the run is distilled into a typed reusable capability artifact, and that
artifact replays deterministically with no LLM, with a real error taxonomy,
safety guardrails, and human escalation on the same live browser session.

Working agreement: one issue per session. On completion, write a handoff to
`C:\Desktop\cua-handoffs\` and stop for a `/compact` before starting the next.

## Locked design decisions

| Area | Decision |
|---|---|
| Target app | Build our own, deliberately hostile legacy markup (tables, iframes, no test IDs) |
| Domain | Credit-union back-office: login -> member search -> detail -> open sub-account |
| Capabilities | **Two**: `member.read_savings_balance` (read) and `member.open_subaccount` (write, final step irreversible) |
| Auth | **Bootstrap outside the artifact.** Credentials from env, never an artifact input |
| Stack | Python 3.11 + Playwright (sync) + Pydantic v2 + Flask + Typer |
| Perception | Accessibility tree primary; screenshots as evidence + last-resort locator tier |
| Locators | Tiered fallback chain, resolution tier logged as the drift signal |
| Session | Long-lived headed browsers owned by a Session Manager; explicit control token |
| Discovery model | `claude-sonnet-5`, overridable via `CUA_MODEL` |
| Operator console | Build: adjacent window + sidebar. Design only: embedded streamed viewport |
| Error taxonomy | Declarative in the artifact (`outcomes` / `recoveries`), not buried in code |
| Multi-tenant | **Built as a demo**: one app, two tenant configs, one artifact + sparse override |
| Artifact storage | Filesystem YAML under `capabilities/`, committed so diffs are reviewable |
| Process model | Single process; operator console on a thread, automation blocks on an Event |

## Issues

| # | Title | GitHub | Depends on | Status |
|---|---|---|---|---|
| 1 | Repo scaffold and core types | #2 | — | done |
| 2 | Target app: screens, data, hostile markup | #3 | 1 | done |
| 3 | Target app: failure modes and triggers | #4 | 2 | done |
| 4 | Surface abstraction, WebSurface, locator engine | #5 | 1, 2 | done |
| 5 | Artifact schema, store, validator | #6 | 1, 4 | done |
| 6 | Session manager, control token, auth bootstrap | #7 | 1, 4 | done |
| 7 | Policy engine: allowlist and risk gating | #8 | 1, 5 | done |
| 8 | Evidence recorder and declarative redaction | #9 | 1, 5 | done |
| 9 | Discovery engine: LLM observe/decide/act loop | #10 | 4, 5, 6, 7, 8 | done |
| 10 | Distillation: trace to artifact | #11 | 5, 9 | done |
| 11 | Replay engine and result contract | #12 | 5, 6, 7, 8 | done |
| 12 | Error taxonomy: outcomes, recoveries, budgets | #13 | 3, 11 | done |
| 13 | Escalation, handoff, human action capture | #14 | 6, 11, 12 | done |
| 13a | Discovery defects found by the first real model run | #21 | 9, 10, 11 | done |
| 14 | Operator console | #15 | 13 | todo |
| 15 | CLI and capability catalog | #16 | 10, 11, 14 | todo |
| 16 | Tenant variant and artifact overrides | #17 | 11, 15 | todo |
| 17 | Evidence runs | #18 | 12, 13, 15 | todo |
| 18 | Tests where they count | #19 | 11, 12, 13 | todo |
| 19 | REPORT.md and README.md | #20 | 17 | todo |

## Requirement coverage

Every Section 3 requirement must have a real implementation, however thin.

| Brief | Requirement | Board items |
|---|---|---|
| 3.1 | Goal-driven LLM agent loop | 9 |
| 3.2 | Structured capability artifact | 5, 10 |
| 3.3 | Deterministic replay + error handling | 11, 12 |
| 3.4 | Safety and policy guardrails | 7, 8 |
| 3.5 | Evidence and observability | 8, 17 |
| 3.6 | Human escalation and handoff | 13, 14 |
| 3.7 | Heterogeneity and multi-tenant | 4, 5, 16, 19 |
| 8 | Stretch: capability catalog, canonicalisation | 15, 16 |

Detailed per-issue specs live in [`docs/PLAN.md`](docs/PLAN.md) and are mirrored into
the GitHub issue bodies, which are generated from it.
