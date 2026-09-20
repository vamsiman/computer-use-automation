# Build plan — Computer-Use Automation System

## Context

Take-home for interface.ai (`D:\Downloads\Assignment A — Computer-Use Automation System.pdf`).
Banks and credit unions run back-office apps with no API; the only way in is to drive
the UI like a human operator. The system uses an LLM to work out how to do a task
**once**, distils that run into a typed, versioned **capability artifact**, and thereafter
replays it **deterministically with no model in the decision loop**.

The artifact is not a cached answer — it is a parameterised script of UI actions.
Replay re-executes the recorded steps with new inputs and scrapes fresh values.

Issue 1 (scaffold and shared types) is already done and pushed. This plan covers the
remaining 18 issues and exists so each GitHub issue can be rewritten with enough
detail to survive a context compaction between sessions.

**Grading order** (brief §7): system design > core-loop correctness > robustness and
error handling > escalation > generalisation > safety > code quality > communication.
Breadth and scaling infrastructure are explicitly *not* rewarded.

## Locked decisions

| Area | Decision |
|---|---|
| Target app | Build our own, deliberately hostile legacy markup |
| Domain | Credit-union back-office |
| Capabilities | **Two**: `member.read_savings_balance` (read) and `member.open_subaccount` (write, final step irreversible) |
| Auth | **Bootstrap outside the artifact.** Credentials from env, never an artifact input. Artifact declares `preconditions: authenticated`. Reused by the `SESSION_EXPIRED` recovery |
| Stack | Python 3.11, Playwright (sync API), Pydantic v2, Flask, Typer |
| Perception | Accessibility tree primary, rendered as compact text. Screenshots are evidence + tier-4 locators only |
| Locators | Ordered fallback chain; the tier that resolved is logged = drift signal |
| Session | Long-lived **headed** browsers in a Session Manager, explicit control token |
| Discovery model | `claude-sonnet-5`, overridable via `CUA_MODEL` |
| Operator console | Build adjacent-window + sidebar. Embedded viewport designed in REPORT.md only |
| Error taxonomy | Declarative in the artifact (`outcomes` / `recoveries`) |
| Multi-tenant | **Built as a demo**: one app, two tenant configs, one artifact + sparse override |
| Artifact storage | Filesystem YAML under `capabilities/`, committed to git so diffs are reviewable |
| Process model | Single process. Operator console is Flask on a thread; automation blocks on a `threading.Event` |

## Invariants

1. Replay never imports an LLM client (asserted by test).
2. The system never fabricates an input value — missing input is a contract failure
   before the browser opens, or an escalation. Never a default, never a guess.
3. Never correct the human, always re-verify the world after a handoff.
4. A business outcome is not a failure.
5. Redaction is declarative, driven by `sensitivity` on artifact inputs/outputs.
6. Discovery and replay talk only to the `Surface` protocol; neither imports Playwright.

---

## Revised issue list

GitHub epic is #1. Board item *N* = GitHub issue *N+1*. Issues #7 and #16 are new
(policy and evidence split apart; tenant variant promoted to its own issue), so the
existing bodies for #8–#18 shift and all get rewritten.

| Board | GH | Title | Depends on |
|---|---|---|---|
| 1 | #2 | Repo scaffold and core types | — | **done** |
| 2 | #3 | Target app: screens, data, hostile markup | 1 |
| 3 | #4 | Target app: failure modes and triggers | 2 |
| 4 | #5 | Surface abstraction, WebSurface, locator engine | 1, 2 |
| 5 | #6 | Artifact schema, store, validator | 1, 4 |
| 6 | #7 | Session manager, control token, auth bootstrap | 1, 4 |
| 7 | #8 | Policy engine: allowlist and risk gating | 1, 5 |
| 8 | #9 | Evidence recorder and declarative redaction | 1, 5 |
| 9 | #10 | Discovery engine: LLM observe/decide/act loop | 4, 5, 6, 7, 8 |
| 10 | #11 | Distillation: trace → artifact | 5, 9 |
| 11 | #12 | Replay engine and result contract | 5, 6, 7, 8 |
| 12 | #13 | Error taxonomy: outcomes, recoveries, budgets | 3, 11 |
| 13 | #14 | Escalation, handoff, human action capture | 6, 11, 12 |
| 14 | #15 | Operator console | 13 |
| 15 | #16 | CLI and capability catalog | 10, 11, 14 |
| 16 | #17 | Tenant variant and artifact overrides | 11, 15 |
| 17 | #18 | Evidence runs | 12, 13, 15 |
| 18 | #19 | Tests where they count | 11, 12, 13 |
| 19 | #20 | REPORT.md and README.md | 17 |

---

## Issue 2 (#3) — Target app: screens, data, hostile markup

**Goal.** A runnable fake credit-union back-office console. Happy path only;
failure modes are Issue 3.

**Files.** `targetapp/app.py`, `targetapp/data.py`, `targetapp/seed.py`,
`targetapp/templates/*.html`, `targetapp/static/legacy.css`, `targetapp/README.md`.
SQLite at `targetapp/cu.db` (gitignored, rebuilt by `seed.py`).

**Schema.** `members(member_no PK, first, last, status, branch, flags)`,
`accounts(id PK, member_no FK, acct_type, acct_number, balance_cents, opened_at)`.
Seed ~10 obviously-fake members. No real PII.

**Routes.**
- `GET/POST /` — sign in, sets a session cookie
- `GET /home` — shell page containing `<iframe name="mainFrame">`
- `GET/POST /members/search`
- `GET /members/<member_no>` — detail, accounts table with balances
- `GET/POST /members/<member_no>/subaccount/new` — 4-field form
- `POST /members/<member_no>/subaccount/review` — confirmation screen
- `POST /members/<member_no>/subaccount/commit` — **irreversible**, creates the account
- `GET /logout`

**Hostile markup — the point of this issue.** Mix good and bad deliberately, so
tier 1 genuinely works in places and tiers 2–3 genuinely fire in others. If
everything resolves on tier 1 the fallback chain is decoration; if nothing does,
the accessibility-tree argument looks wrong.

- Main content inside an `<iframe>` — the surface driver must walk frames
- Nested `<table>` layout with spacer `<td>`s, no semantic containers
- ASP.NET-style ids: `ctl00_ContentPlaceHolder1_txtMbrId`
- **Search field**: bare `<span class="lbl">Member ID:</span>` with no `<label for>`,
  so the accessible name is empty → forces **tier 2 label_proximity**
- **Search button**: a real `<input type="submit" value="Search">` → **tier 1 works**
- **Accounts table**: no `<th>` elements → forces **tier 3 row_cell**
- At least one control as `<a href="javascript:__doPostBack(...)">` → tier 2/3
- No `data-testid` anywhere

**Tenant hook.** Read `TENANT` env (`base` | `riverbend`) now and thread it into the
templates, but only implement `base`. Issue 16 fills in the variant.

**Done when.** `flask --app targetapp.app run` serves the full flow and a human can
sign in, search a member, read a balance, and open a sub-account through the
confirmation screen.

---

## Issue 3 (#4) — Target app: failure modes and triggers

**Goal.** Make the app fight back like a real bank system. Every condition
deterministically triggerable, because §3.3 is graded on handling these and we
cannot demo what we cannot summon.

**Trigger table** (document in `targetapp/README.md`):

| Trigger | Behaviour | Class |
|---|---|---|
| `10001`, `10002` | Normal member, savings + checking | happy path |
| `10003` | "You are not authorized to view this member" | declared outcome `PERMISSION_DENIED` |
| `10004` | "System Notice" interstitial before detail renders | recovery `dismiss` |
| `10005` | "Account flagged — contact compliance", only `[Acknowledge]` | **undeclared** → escalation demo |
| `10006` | Member exists but has no savings account | extract target missing → `Failure` |
| `99999` | "No records found" | declared outcome `MEMBER_NOT_FOUND` |
| `abc`, `123` | "Member ID must be 5 digits" | declared outcome `VALIDATION_ERROR` |
| `GET /debug/expire` | Drops the session; next request bounces to Sign In | recovery `reauthenticate_then_resume` |
| `GET /debug/slow?ms=8000` | Next page render sleeps | recovery `retry_with_backoff` |
| 3 failed sign-ins | Account lockout | bootstrap hard failure |

`10005` is the load-bearing one: it must be a *blocking* state the artifact never
declared, so replay cannot classify it and must escalate rather than guess.

**Done when.** Each row is reachable by hand and documented.

---

## Issue 4 (#5) — Surface abstraction, WebSurface, locator engine

**Goal.** The seam the whole design rests on (brief §3.7). Discovery and replay
must never import Playwright.

**Files.** `cua/surface/base.py` (Protocol), `cua/surface/web.py` (`WebSurface`),
`cua/surface/a11y.py` (tree construction), `cua/surface/resolve.py` (tier resolution),
`cua/surface/inject.js`.

**Protocol.**
```python
class Surface(Protocol):
    def observe(self) -> Snapshot: ...
    def resolve(self, loc: Locator) -> Resolution: ...
    def act(self, action: Action) -> ActResult: ...
    def screenshot(self, path: str) -> str: ...
    def close(self) -> None: ...
```

**Tree construction — do not use `page.accessibility.snapshot()`.** It is
deprecated, returns no element handles, and cannot cross frames. Instead inject
`inject.js`, which walks the DOM and for each element:
- computes a role from tag + `type` + explicit `role`
- computes an accessible name in ARIA precedence order: `aria-label` →
  `aria-labelledby` → associated `<label for>` → `alt` / `title` → `value` →
  trimmed text content
- stamps `data-cua-ref="n<i>"` so the node maps back to a real element
- records bounding box and disabled state

Recurse into every frame via `page.frames`, merging into one tree with frame
boundary nodes. This is what makes an `<iframe>`-based legacy app perceivable at all.

**Locator resolution**, in `LOCATOR_TIERS` order, returning the tier that hit:
1. `role_name` — match role + exact/contains name, `nth` disambiguates
2. `label_proximity` — find the text node, then the nearest matching control in
   the given direction by geometry
3. `region_path` — structural path scoped to a semantically located region
4. `row_cell` — find the table, match a row by text, pick the column by heading or index
5. `anchor_offset` — pixel offset from a text anchor; always reported degraded

`Resolution.match_count > 1` means ambiguity — record it, do not silently take the first.

**Playwright setup.** `playwright install chromium` has not been run yet. Headed by
default (`headless=False`); a `CUA_HEADLESS` env override for CI.

**Done when.** Locators resolve against the Issue 2 app across tiers 1, 2 and 4
(row_cell arrives with the accounts table), with tests proving the fallback order
and that the reported tier is correct.

---

## Issue 5 (#6) — Artifact schema, store, validator

**Goal.** The focal point of the evaluation. Design it deliberately.

**Files.** `cua/artifact/models.py`, `cua/artifact/store.py`,
`cua/artifact/validate.py`, `capabilities/` (storage root).

**Schema** (Pydantic v2, YAML round-trip):
- `schema_version`
- `capability`: `id`, `version` (semver), `title`, `description` (written for the
  *calling agent*), `app` {`vendor`, `product`, `version_range`}, `tenant`,
  `extends`, `status` (`draft` | `approved`), `provenance`
  {`discovered_at`, `model`, `run_id`, `step_count_raw`, `step_count_final`}
- `inputs`: name → {`type`, `pattern`, `required`, `sensitivity`, `description`}
- `outputs`: name → {`type`, `sensitivity`, `currency`}
- `preconditions`: e.g. `authenticated: true`
- `steps[]`: `id`, `intent`, `action`, `target` (Locator), `args`, `risk`, `checkpoint`
- `success`: `checkpoint`, `require_outputs[]`
- `outcomes[]`: `code`, `detect` (LocatorSpec), `terminal`, `message`
- `recoveries[]`: `code`, `detect`, `strategy`, `max_occurrences`, strategy args

**Store.** `capabilities/<capability_id>/<version>.yaml`. Filesystem, not a database
— the brief requires artifacts be *reviewable*, and a YAML diff in a pull request is
the most reviewable form there is. `load(id, version=None)` resolves latest semver.

**Validator rejects:**
- `{{ inputs.x }}` referencing an undeclared input
- a step with no `intent`
- an output never produced by an `extract` step
- a locator with no `primary`
- `success.require_outputs` naming an undeclared output
- a `recovery` with no `max_occurrences`
- `extends` set without `tenant`

**Done when.** Round-trip is lossless, every rejection rule has a test, and a
hand-written `member.read_savings_balance` fixture validates.

---

## Issue 6 (#7) — Session manager, control token, auth bootstrap

**Files.** `cua/session/manager.py`, `cua/session/state.py`, `cua/session/auth.py`.

**Session.** `id`, `surface`, `state: RunState`, `control: ControlOwner`, `run_id`,
`created_at`, `tenant`. Held in a dict on the manager so something other than the
running function can reach it — that single change is what makes handoff possible.

**State machine.** Legal transitions only; anything else raises `IllegalTransition`.
```
PENDING → RUNNING → AWAITING_HUMAN → HUMAN_CONTROL → VERIFYING → RUNNING
RUNNING → SUCCEEDED | BUSINESS_OUTCOME | FAILED | ABORTED
VERIFYING → AWAITING_HUMAN        (re-verify failed: escalate, never guess)
any non-terminal → ABORTED        (human cancel)
```

**Control token.** `session.assert_control(ControlOwner.AUTOMATION)` runs at the top
of every `act()`. Flips only on `grant()` / `release()`. This is the brief's
"way to know who is (or should be) in control".

**Auth bootstrap** (`auth.py`). Reads `CUA_APP_USER` / `CUA_APP_PASS` from env,
signs in, confirms authentication, returns. **Not an artifact, never recorded, never
logged.** Credentials are structurally incapable of reaching an artifact because they
are never an artifact input. Called at session creation and re-called by the
`SESSION_EXPIRED` recovery.

**Done when.** Transitions are tested including illegal ones, and `assert_control`
refuses when the human holds the token.

---

## Issue 7 (#8) — Policy engine: allowlist and risk gating

**Files.** `cua/policy/engine.py`, `cua/policy/config.py`, `policy.yaml`.

**Config** (`policy.yaml`, checked in):
```yaml
allowlist:
  origins: ["http://localhost:5000", "http://localhost:5001"]
  paths: ["/members/**", "/home", "/"]
  actions: [navigate, click, type, select, press_key, extract, wait_for]
risk:
  unattended_max: caution     # irreversible always needs approval
  require_approval: [irreversible]
```

**Decision.** `check(action, context) -> Allow | Deny(reason) | RequireApproval(reason)`.
Evaluated **before** every act, never after. A denial is
`Failure(category=POLICY)` and the action is not performed.

Two independent gates, deliberately: *where* we may act (origins/paths) and *what*
we may do (action types, risk level). A flow can be on an allowed page and still be
blocked from submitting an irreversible form.

**Risk source.** Read off the step's `risk` field in the artifact during replay; during
discovery, inferred conservatively — any action on a control whose name matches a
configurable pattern (`confirm|submit|commit|delete|transfer|post`) is treated as
irreversible until a human says otherwise.

**Done when.** A navigate off-allowlist is denied; an irreversible step returns
`RequireApproval`; both have tests.

---

## Issue 8 (#9) — Evidence recorder and declarative redaction

**Files.** `cua/evidence/recorder.py`, `cua/evidence/redact.py`.

**Layout.** `evidence/runs/<run_id>/`
- `run.jsonl` — one structured record per step: step id, intent, action, locator
  description, **tier resolved**, duration, outcome
- `steps/NNN.png` — screenshot per step
- `failure/` — screenshot, a11y snapshot, page HTML, on failure only
- `result.json` — the final typed result
- `meta.json` — capability, version, tenant, inputs (redacted), model, timings

**Redaction is declarative.** The `Redactor` is constructed from the artifact's
`inputs`/`outputs` sensitivity map plus the set of env secret values. Anything marked
`pii` is masked, anything `secret` never reaches a record at all. This is deliberately
*not* pattern-matching for things that look sensitive — the schema already says what
is sensitive, and guessing is how leaks happen.

Screenshots are the known limit: a balance is visible in a PNG. Document this in
REPORT.md §6 under limits, and gate screenshot retention behind a config flag.

**Done when.** A run whose outputs are marked `pii` produces logs with nothing
sensitive in the clear, proven by test.

---

## Issue 9 (#10) — Discovery engine: LLM observe/decide/act loop

**Goal.** Brief §3.1, and the one part that must be genuinely real (§4).

**Files.** `cua/discovery/agent.py`, `cua/discovery/prompt.py`,
`cua/discovery/tools.py`, `cua/discovery/trace.py`.

**Loop.** Anthropic tool-use with `claude-sonnet-5`. Tools mirror `ActionType`
one-for-one, plus `done` and `stuck`. Each turn: render `Snapshot.text_view()` →
model emits a tool call → build `Action` → **policy check** → `surface.act()` →
record to trace → observe again.

The model sees the compact accessibility tree, never raw HTML and never a screenshot.
That is what keeps perception cheap and makes the thing it points at a *named control*
we can record semantically instead of a pixel.

**Stop conditions.** Max steps (default 40), wall-clock timeout, model emits `done`
or `stuck`, or **stuck detection**: the same action repeated N times with an unchanged
tree hash, or a revisited tree hash cycle.

**Trace.** `DiscoveryTrace` = ordered records of {action, act_result, tree_hash before
and after, tier resolved, screenshot ref}. This is the raw material Issue 10 distils —
and it is kept deliberately separate from the artifact, per the brief's requirement
that the artifact be decoupled from the raw model transcript.

**Config.** `ANTHROPIC_API_KEY` required (not yet set — user must supply).
`CUA_MODEL` defaults to `claude-sonnet-5`.

**Done when.** One real run completes `"look up member 10001 and read their current
savings balance"` against the live app, with the trace on disk.

---

## Issue 10 (#11) — Distillation: trace → artifact

**Files.** `cua/discovery/distill.py`, `cua/discovery/annotate.py`.

Three passes:
1. **Prune** — drop actions whose tree hash did not change, and abandoned branches
   not on the path to success. Keep the linear successful path.
2. **Parameterise** — literals matching the goal's supplied input values become
   `{{ inputs.<name> }}`. `10001` → `{{ inputs.member_id }}`. Record the inferred
   type and pattern.
3. **Annotate** — **one** LLM call, outside the action loop, that produces: capability
   id and title, the agent-facing description, per-step `intent` lines, and proposed
   `outcomes` from any exceptional states seen while exploring.

The model is a *writer* here, never a decider. Output is `status: draft` until a human
approves it, which is where artifact review lives.

**Done when.** A discovery run yields a schema-valid artifact with a parameterised
input, and replaying it with a different member id returns that member's balance.

---

## Issue 11 (#12) — Replay engine and result contract

**Goal.** Brief §3.3, the production path.

**Files.** `cua/replay/engine.py`, `cua/replay/result.py`.

**Result union** — exactly one of:
- `Success(outputs, steps_executed, tier_log, evidence_ref)`
- `BusinessOutcome(code, message, partial_outputs, evidence_ref)`
- `Escalated(intervention_id, step_id, reason, evidence_ref)`
- `Failure(step_id, intent, expected, observed, category, evidence_ref)`

**Order of operations per step.** contract check (once, up front) → policy check →
resolve locator (log tier) → act → wait for and verify checkpoint →
evaluate `outcomes` then `recoveries`.

**Contract enforcement happens before the browser opens.** A missing or ill-typed
required input is `Failure(category=CONTRACT)` immediately. Never a default, never an
empty string — fabricating an input is how you silently corrupt a record.

**No LLM.** `cua/replay/` imports no Anthropic client, asserted by a test in Issue 18.
Determinism by construction rather than by discipline.

**Done when.** Replaying the discovered artifact with `member_id=10002` returns that
member's balance, and `tier_log` shows which tier each step resolved on.

---

## Issue 12 (#13) — Error taxonomy: outcomes, recoveries, budgets

**Files.** `cua/replay/conditions.py`.

**Evaluation.** After each act and on every checkpoint timeout: evaluate `outcomes`
detectors first (→ stop, return `BusinessOutcome`), then `recoveries` detectors
(→ handle, continue). Neither matched but the checkpoint failed → decide between
`Failure` and `Escalated`: a *blocking* undeclared state escalates, a missing expected
element fails.

**Recovery strategies.**
- `dismiss` — click the named control, re-verify, continue
- `retry_with_backoff` — re-observe with exponential backoff
- `reauthenticate_then_resume` — call the Issue 6 auth bootstrap, resume from the
  declared step

Each carries `max_occurrences`; exceeding the budget converts it to
`Failure(category=RECOVERY_EXHAUSTED)`. Budgets exist so a recovery loop cannot
become an infinite one.

**Mapping to verify** (each a test against the Issue 3 triggers):

| Trigger | Expected result |
|---|---|
| `99999` | `BusinessOutcome(MEMBER_NOT_FOUND)` |
| `abc` | `BusinessOutcome(VALIDATION_ERROR)` |
| `10003` | `BusinessOutcome(PERMISSION_DENIED)` |
| `10004` | `Success` — interstitial dismissed, recorded in `tier_log` |
| `/debug/slow` | `Success` after backoff |
| `/debug/expire` | `Success` after re-auth |
| `10005` | `Escalated` — undeclared blocking state |
| `10006` | `Failure(LOCATOR_UNRESOLVED)` |

**Done when.** That table passes as a test suite.

---

## Issue 13 (#14) — Escalation, handoff, human action capture

**Goal.** Brief §3.6. Weighted fourth and explicitly "not just a TODO".

**Files.** `cua/session/intervention.py`, `cua/session/human.py`,
`cua/surface/watch.js`.

**Detect and route.** On stuck or on an undeclared blocking state, write an
`InterventionRequest`: capability id and version, step id and intent, why it stopped,
screenshot ref, a11y snapshot, typed parameters so far (redacted), session id.

**Take control.** `grant()` → state `HUMAN_CONTROL`, control token to `HUMAN`,
automation blocks on a `threading.Event`. The human clicks directly in the Chromium
window that is already open on screen — that *is* the live session, which is why the
browser is headed. No streaming, no co-browsing server.

**Capture what the human did.** `watch.js` is injected for the duration of the
handoff and listens for `click`, `change` and `input`, buffering
{timestamp, role, accessible name, value-redacted} records. Drained on release and
appended to the evidence log, so the handoff is auditable.

**Hand back.** `release()` → `VERIFYING`. Re-verify the current step's checkpoint.
Pass → `RUNNING` and continue. Fail → `AWAITING_HUMAN` again. **Never correct the
human, always re-verify the world**: the human is the higher authority, but the
automation must not resume on an assumption.

**Cancel.** Any non-terminal state → `ABORTED`.

**Done when.** A replay against `10005` pauses, a human acknowledges the dialog in the
same window, and the run resumes and completes — with the human's clicks in the log.

---

## Issue 14 (#15) — Operator console

**Files.** `cua/operator/server.py`, `cua/operator/templates/console.html`.

Flask on a thread, polling session state. Lists open interventions; for the selected
one shows capability, step, intent, why it stopped, the last screenshot, and the typed
parameters. Buttons: **Grant**, **Resume**, **Cancel**. When a value is required it is
requested as a *typed field* validated against the artifact's input schema — never a
free-text guess.

The live session is the adjacent Chromium window. The embedded streamed viewport is
**designed in REPORT.md, not built** — the brief descopes full co-browsing. The seam
is that the console talks to the session only through the control-token API, so
swapping a screenshot for a streamed interactive viewport changes one component.

**Done when.** An intervention can be granted, completed and resumed from the console.

---

## Issue 15 (#16) — CLI and capability catalog

**Files.** `cua/cli/main.py` (Typer).

```
cua seed                                              rebuild the target app DB
cua serve-app [--tenant base|riverbend]               run the target app
cua discover --goal "..." --target <url> [--input k=v] discovery run → draft artifact
cua list                                              catalog with typed signatures
cua show <capability-id>                              full contract, human-readable
cua approve <capability-id> --version <v>             draft → approved
cua replay <capability-id> --input member_id=10002 [--tenant ...]
cua operator                                          start the console
```

`cua list` is the §8 stretch goal — saved artifacts exposed as a catalog of callable
capabilities an agent could discover by name with typed args. It prints the same
contract an agent would consume, which is the cheapest possible proof that the
artifact really is an agent-invocable capability and not just a step list.

`cua replay` refuses a `draft` artifact unless `--allow-draft` is passed.

**Done when.** The README demo path runs start to finish from a clean checkout.

---

## Issue 16 (#17) — Tenant variant and artifact overrides

**Goal.** Demonstrate cross-tenant reuse — brief §3.7 and the §8 canonicalisation
stretch goal — without building multi-tenant infrastructure.

**Target app.** `TENANT=riverbend` changes: branding and colours, the search field
label `Member ID:` → `Member Number:`, and the accounts table column order. Runs on
port 5001. Same vendor product, configured differently — exactly the brief's scenario.

**Overrides.** `cua/artifact/overrides.py`. A tenant artifact is a *sparse* document:
```yaml
extends: member.read_savings_balance@1.0.0
tenant: cu-riverbend
steps:
  s2:
    target:
      primary: {strategy: role_name, role: textbox, name: "Member Number"}
```
Merge is by step id, field-level, base first. Stored at
`capabilities/<id>/tenants/<tenant>.yaml`.

**The payoff to measure.** Replay the *base* artifact against riverbend with no
override and record which tiers resolve — the label change should push step 2 from
tier 1 to tier 2 while still succeeding. That is graceful degradation demonstrated
rather than asserted, and it is the evidence that the fallback chain earns its cost.
Then apply the override and watch it return to tier 1.

**Done when.** One artifact replays against both tenants, and the tier log shows the
degradation and its repair.

---

## Issue 17 (#18) — Evidence runs

Populate `/evidence/` — a required deliverable that must tell the whole story without
anything being run.

- one genuine LLM discovery run, with trace (non-negotiable, §4)
- the resulting artifact, and the approved version
- a clean replay with a different input
- a business-outcome replay (`99999` → `MEMBER_NOT_FOUND`)
- a recovery replay (`10004` interstitial; `/debug/expire` re-auth)
- an escalation run (`10005`) showing handoff, human actions, and resume
- the cross-tenant tier-degradation log from Issue 16
- `evidence/README.md` indexing all of it

Optional: a short screen recording of the handoff.

---

## Issue 18 (#19) — Tests where they count

Not coverage theatre. The load-bearing pieces:

- artifact schema round-trip; every validator rejection rule
- locator tier fallback resolution order and reported tier
- **assert `cua/replay/` imports no LLM client** — determinism by construction
- the Issue 12 trigger → result-variant table
- control-token state machine, including illegal transitions and refusal to act
  while the human holds the token
- redaction: no `pii` or `secret` value reaches any record
- contract enforcement: missing input fails before the browser opens

**Done when.** `pytest` is green and fast. Browser-dependent tests marked so they can
be skipped without Playwright browsers installed.

---

## Issue 19 (#20) — REPORT.md and README.md

**REPORT.md, seven headings verbatim** (brief §6): Architecture · Artifact schema ·
Determinism & error handling · Heterogeneity & multi-tenant · Escalation & handoff ·
Safety · Cuts.

Must defend: accessibility-tree-primary perception and the determinism tension it
resolves; tiered locators with tier logging as the drift signal; the declarative error
taxonomy; auth as bootstrap rather than artifact steps; adjacent-window handoff with
the embedded viewport as the designed next step; the deliberately hostile target app;
what was cut and what comes next.

**README.md**: setup, keys, how to run without live services, the exact demo commands.

**Final step: flip the repo public**, then the submission email to
`assignments@interface.ai` with the URL on its own line.

---

## Verification

End-to-end acceptance, runnable from a clean checkout:

```bash
cua seed && cua serve-app &
cua discover --goal "look up member 10001 and read their current savings balance" \
             --target http://localhost:5000 --input member_id=10001
cua list                       # capability appears with a typed signature
cua approve member.read_savings_balance --version 1.0.0
cua replay member.read_savings_balance --input member_id=10002   # → Success
cua replay member.read_savings_balance --input member_id=99999   # → BusinessOutcome
cua replay member.read_savings_balance --input member_id=10005   # → Escalated
cua operator                   # grant, act in the live window, resume
pytest
```

Per-issue acceptance is the "Done when" line in each section above.

## Open item

`ANTHROPIC_API_KEY` is not set. Needed before Issue 9 (#10); everything up to that
point can be built and tested without it.
