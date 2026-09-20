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
| Stack | Python 3.11 + Playwright + Pydantic |
| Perception | Accessibility tree primary; screenshots as evidence + last-resort locator tier |
| Locators | Tiered fallback chain, resolution tier logged as the drift signal |
| Session | Long-lived headed browsers owned by a Session Manager; explicit control token |
| Operator console | Build: adjacent window + sidebar. Design only: embedded streamed viewport |
| Error taxonomy | Declarative in the artifact (`outcomes` / `recoveries`), not buried in code |
| Multi-tenant | `extends` / `tenant` fields carried and defended; not built |

## Issues

| # | Title | Depends on | Status |
|---|---|---|---|
| 1 | Repo scaffold, tooling, and core shared types | — | done |
| 2 | Target app: screens and data | 1 | todo |
| 3 | Target app: failure modes and triggers | 2 | todo |
| 4 | Surface abstraction + WebSurface + locator engine | 1, 2 | todo |
| 5 | Artifact schema and validator | 1, 4 | todo |
| 6 | Session Manager + control-token state machine | 1, 4 | todo |
| 7 | Policy engine + evidence recorder with redaction | 1, 5 | todo |
| 8 | Discovery engine (LLM observe/decide/act loop) | 4, 5, 6, 7 | todo |
| 9 | Distillation: transcript -> artifact | 5, 8 | todo |
| 10 | Replay engine + result contract | 5, 6, 7 | todo |
| 11 | Error taxonomy wiring: outcomes, recoveries, budgets | 3, 10 | todo |
| 12 | Escalation, handoff, and human action capture | 6, 10, 11 | todo |
| 13 | Operator console | 12 | todo |
| 14 | CLI + capability catalog | 8, 9, 10 | todo |
| 15 | Evidence runs: discovery, clean replay, failing replay | 11, 12, 14 | todo |
| 16 | Tests where they count | 10, 11 | todo |
| 17 | REPORT.md and README.md | 15 | todo |

## Requirement coverage

Every Section 3 requirement must have a real implementation, however thin.

| Brief | Requirement | Issues |
|---|---|---|
| 3.1 | Goal-driven LLM agent loop | 8 |
| 3.2 | Structured capability artifact | 5, 9 |
| 3.3 | Deterministic replay + error handling | 10, 11 |
| 3.4 | Safety and policy guardrails | 7 |
| 3.5 | Evidence and observability | 7, 15 |
| 3.6 | Human escalation and handoff | 12, 13 |
| 3.7 | Heterogeneity and multi-tenant (design only) | 4, 5, 17 |
