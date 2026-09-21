# Computer-Use Automation System

Back-office banking software mostly has no API. The only way in is to drive the
UI the way a human operator does — and doing that with a model in the loop on
every run is slow, expensive, and different every time.

So this system uses a model **once**, to work out how to do a task, and distils
that run into a typed, versioned **capability artifact**. Every run after that
replays the artifact deterministically, with no model in the decision loop at
all.

The artifact is not a cached answer. It is a parameterised script of UI
actions: replay re-executes the recorded steps with new inputs and scrapes
fresh values off the live screen.

```
member.read_savings_balance(member_id: string) -> {member_name: string, savings_balance: money}
```

---

## What is here

| | |
|---|---|
| `cua/surface/` | Perception and action. Accessibility tree, tiered locators, Playwright driver |
| `cua/artifact/` | The capability schema, its store, its validator, tenant overrides |
| `cua/discovery/` | The one loop with a model in it, and the distillation that follows |
| `cua/replay/` | The production path. Imports no model client, and there is a test that says so |
| `cua/session/` | Live sessions, the control token, the human handoff |
| `cua/policy/` | Where we may act, and what we may do |
| `cua/evidence/` | What every run wrote down, and what it refused to |
| `cua/operator/` | The console a person uses when a run stops |
| `targetapp/` | A deliberately hostile stand-in for a credit-union back office |
| `evidence/` | Real runs, including the genuine discovery run |

`REPORT.md` is the design write-up and the place to start if you want the
reasoning rather than the code.

---

## Setup

Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
playwright install chromium
```

Only **discovery** needs an API key. Everything else — replay, the error
taxonomy, the handoff, the console, the whole test suite — runs offline.

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # discovery only
export CUA_APP_USER=teller1             # optional; these are the defaults
export CUA_APP_PASS=demo-pass-2024
```

Those credentials are for the fake application in `targetapp/`. **There is no
real data anywhere in this repository** — the members are invented, the
balances are invented, and the sign-in is a stand-in for whatever SSO a real
deployment would put in front of it.

---

## The demo, start to finish

```bash
cua seed                                   # build the fake credit union's data
cua serve-app                              # http://localhost:5000, leave running
```

In another terminal:

```bash
# 1. Work out how to do it, once, with a model in the loop
cua discover --goal "look up member 10001 and read their current savings balance" \
             --input member_id=10001

# 2. The result is a draft capability. Read it.
cua list
cua show member.read_savings_balance
cua approve member.read_savings_balance

# 3. Replay it deterministically, with a different member. No model.
cua replay member.read_savings_balance --input member_id=10002
```

Skip step 1 if you have no API key — a hand-written reference artifact is
already in `capabilities/`, and everything below works against it.

### The parts worth actually watching

```bash
cua replay member.read_savings_balance --input member_id=99999   # BusinessOutcome
cua replay member.read_savings_balance --input member_id=10003   # BusinessOutcome
cua replay member.read_savings_balance --input member_id=10004   # Success, after a recovery
cua replay member.read_savings_balance --input member_id=10005   # Escalated
cua replay member.read_savings_balance --input member_id=10006   # Failure
cua replay member.read_savings_balance --input member_id=abc     # Failure, before the browser opens
```

Four different kinds of answer, and the difference between them is the point.
`99999` is not a failure — the application answered, correctly, that no such
member exists. `10006` *is* a failure: the automation met a world it was not
told about. Conflating those two is the mistake this design exists to avoid.

### The handoff

```bash
cua replay member.read_savings_balance --input member_id=10005
```

Member 10005 is flagged for compliance review, and nothing in the artifact
declares that state. The run stops, and the browser window stays open on the
hold screen. That window *is* the live session — acknowledge the hold in it by
hand and the run re-checks the screen for itself and finishes.

### Two tenants, one capability

```bash
cua serve-app --tenant riverbend --port 5001
cua replay member.read_savings_balance --input member_id=10001 \
    --target http://localhost:5001
```

Riverbend calls the field "Member Number" instead of "Member ID". The
capability still works — and the tier log says it degraded, which is the
signal that something changed underneath it. Then:

```bash
cua replay member.read_savings_balance --input member_id=10001 \
    --target http://localhost:5001 --tenant cu-riverbend
```

and the step returns to its primary rule. One artifact, one sparse override of
a single field, three deployments. See `evidence/tenant-tier-log.md`.

---

## Tests

```bash
pytest                      # everything
pytest -m "not browser"     # no Playwright browsers needed
```

No test needs an API key. The discovery loop is tested end to end against the
live application with a rule-based stand-in for the model, so everything
except the model's own judgement is covered offline.

---

## Triggering the failure modes by hand

The target application can be made to misbehave on cue — the whole reason it is
ours rather than somebody else's demo server.

| | |
|---|---|
| `10001`, `10002` | ordinary members |
| `10003` | not authorised to view |
| `10004` | a routine interstitial the artifact knows how to dismiss |
| `10005` | a compliance hold nothing declared |
| `10006` | a member with no savings account |
| `99999` | no such member |
| `GET /debug/expire` | drops the session mid-flow |
| `GET /debug/slow?ms=5000&path=/members/10001` | stalls one page |
| `GET /debug/reset` | clears everything, including the sign-in |

These are deliberately **off the policy allowlist**, so a run cannot reach
them. Arming a condition is something an operator does to the application, not
something a capability does.

---

## Configuration

| variable | |
|---|---|
| `ANTHROPIC_API_KEY` | discovery only |
| `CUA_MODEL` | defaults to `claude-sonnet-5` |
| `CUA_APP_USER` / `CUA_APP_PASS` | the target app's credentials, never an artifact input |
| `CUA_HEADLESS` | `1` to run browsers headless; headed by default, because the handoff is real |
| `CUA_EVIDENCE_ROOT` | where run bundles are written |
| `CUA_EVIDENCE_SCREENSHOTS` / `_FAILURE_BUNDLE` / `_PAGE_SOURCE` | retention switches |
| `CUA_REDACTION_SALT` | salts the PII tokens in evidence |
| `CUA_HANDOFF_TIMEOUT` | how long a paused run waits for a person (default 900s) |
| `TENANT` | which tenant `cua serve-app` serves |
