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

## Watch it work

![the compliance hold, handed to a person and handed back](evidence/media/03-escalation-handoff.gif)

The run meets a compliance hold nothing in the capability declares. It does not
guess — it pauses, a person deals with it in the same live browser window, and
the engine re-checks the screen for itself before carrying on.

More in [`evidence/media/`](evidence/media/): the ordinary path, a business
outcome, a genuine failure, and the same capability running against a second
tenant that renamed one of its fields.

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

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

Activating matters: `cua` is installed *into* the virtual environment, so it
is only on your PATH once that has run. If it comes back "command not found"
or "not recognized", that is why -- and this form needs no activation at all:

```bash
.venv/bin/python -m cua.cli.main list          # macOS / Linux
.venv\Scripts\python -m cua.cli.main list      # Windows
```

Everything below is written as `cua ...`; substitute that longer form if you
would rather not activate anything.

Only **discovery** needs an API key. Everything else — replay, the error
taxonomy, the handoff, the console, the whole test suite — runs offline, so
you can skip this entirely and still run every demo below except step 1.

```bash
cp .env.example .env          # macOS / Linux -- then put YOUR OWN key in it
copy .env.example .env        # Windows
```

Or set it in the environment directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here            # macOS / Linux
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"          # Windows PowerShell
```

`.env` is gitignored. No key is committed to this repository and none ever
should be.

Those credentials are for the fake application in `targetapp/`. **There is no
real data anywhere in this repository** — the members are invented, the
balances are invented, and the sign-in is a stand-in for whatever SSO a real
deployment would put in front of it.

---

## The demo, start to finish

Commands below are written for **bash / zsh** (macOS, Linux). They work
verbatim in a Mac terminal. On Windows PowerShell the `cua ...` commands are
identical; only two things differ, and both appear below:

| | bash / zsh | PowerShell |
|---|---|---|
| line continuation | `\` at end of line | `` ` `` at end of line, or put it on one line |
| an environment variable | `export NAME=value` | `$env:NAME = "value"` |

> **On macOS, port 5000 is taken by AirPlay Receiver** (Monterey and later).
> If `cua serve-app` fails to bind, or the pages come back oddly, use another
> port — `cua serve-app --port 5055`, then pass
> `--target http://localhost:5055` to every `cua replay`. Or turn AirPlay
> Receiver off in System Settings → General → AirDrop & Handoff.

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

### Has anything started drifting?

```bash
cua drift
```

Every replay records which locator rule found each control — on successful runs
as much as failed ones, because a capability sliding onto its third fallback is
degrading weeks before it breaks. This reads that back across every run on disk
and says which steps are resolving worse than they used to.

---

## Seeing it by hand

Five scripts, none of which need anything on `PYTHONPATH` — they find the
project from their own location, so `python scripts/<name>.py` works from
anywhere on any platform.

```bash
python scripts/hitl.py              # the human handoff, narrated, no time pressure
python scripts/uat.py --list        # eleven acceptance cases
python scripts/uat.py --case handoff
python scripts/record_media.py      # regenerate evidence/media/
python scripts/make_evidence.py     # regenerate evidence/runs/
```

**`hitl.py` is the one to run** if you only run one. It drives itself into a
compliance hold, stops, and hands you the session — printing every state change
as it happens and, while you hold control, proving the automation is *refused*
rather than merely discouraged.

Browsers are headed for these on purpose: the window that opens **is** the live
session, which is what makes the handoff real rather than simulated. If you
would rather they were not:

```bash
export CUA_HEADLESS=1                  # macOS / Linux
$env:CUA_HEADLESS = "1"                # Windows PowerShell
```

`python` here means the one in the activated virtual environment. If you have
not activated it, `.venv/bin/python scripts/hitl.py` (macOS / Linux) or
`.venv\Scripts\python scripts\hitl.py` (Windows) does the same thing.

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

Set them the way your shell does it:

```bash
export CUA_HEADLESS=1                  # macOS / Linux
$env:CUA_HEADLESS = "1"                # Windows PowerShell
```
