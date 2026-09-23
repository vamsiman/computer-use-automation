# Design report

A model works out how to do a UI task **once**. That run is distilled into a
typed, versioned capability artifact, and every run after it replays the
artifact deterministically with no model in the decision loop.

Claims below that are measurable are measured. Longer reasoning per decision
is in [`docs/DESIGN-NOTES.md`](docs/DESIGN-NOTES.md); evidence for every number
is in [`evidence/`](evidence/).

## 1. Architecture

```
goal ──► Discovery (model, once) ──► trace ──► Distillation ──► Artifact (draft)
                                                                     │ a person approves
inputs ─────────────────────────────────────────────► Replay (no model) ──► Result
                                          guarded by Policy · Surface · Evidence · Session
```

**The seam everything rests on** is `Surface`: five methods — `observe`,
`resolve`, `act`, `screenshot`, `close`. Neither discovery nor replay imports
Playwright. That is only worth anything because of *what crosses the boundary*:
an accessibility tree and a locator chain, both of which mean the same thing on
a web page and a native window. Had the currency been CSS selectors or pixels,
the seam would be decorative.

**Perception is the accessibility tree, not pixels.** The model never sees a
screenshot or raw HTML — it sees a compact text tree from a script that walks
every frame computing ARIA roles and names. (Playwright's own
`accessibility.snapshot()` is deprecated, hands back no element handles, and
cannot cross frames.) So perception is cheap, what the model points at is a
*named control* that can be recorded semantically, and replay perceives
identically to discovery.

**`ModelClient` has one method**, so the whole discovery loop is tested
offline against the live app with a rule-based stand-in. Only the model's
judgement is unproven by tests, which is the right thing to leave unproven.

## 2. Artifact schema

YAML at `capabilities/<id>/<version>.yaml`, in git: the brief requires
artifacts be reviewable, and a diff in a pull request is the most reviewable
form there is.

```yaml
capability:   id, version, title, description, app, tenant, extends, status, provenance
inputs:       name -> {type, pattern, required, sensitivity}
outputs:      name -> {type, sensitivity, currency}
preconditions: {authenticated, entry_checkpoint}
steps[]:      {id, intent, action, target: Locator, args, risk, checkpoint}
success:      {checkpoint, require_outputs[]}
outcomes[]:   {code, detect, message}     # answers, not errors
recoveries[]: {code, detect, strategy, max_occurrences}
```

**Locators are an ordered chain and the tier that resolved is logged** — on
successful runs too, because a capability sliding onto its third fallback is
degrading weeks before it breaks. `row_cell` outranks `region_path`
deliberately: "the Balance on the Savings row" survives a column reorder, and
"row 1, cell 2" does not fail there — it reads the wrong column and returns a
plausible number. **A rule that can be confidently wrong belongs below one that
can only be right or absent.**

**Every locator is synthesised and verified at record time.** The model points
by `ref`; each candidate spec must resolve to exactly one node, and that node
must be the one pointed at. So a discovered artifact contains only locators
that were *executed* against the real application.

**That was not enough, and finding out is the most useful thing here.** The
first real discovery run recorded `cell "4,210.33"` as the primary locator for
the balance — the value it was there to read. It verified perfectly, and worked
on exactly one member; replaying with another fell to tier 3 and reported
itself degraded on a brand-new artifact. At record time a circular locator *is*
a correct locator. The missing property was never "does this resolve" but "is
this description independent of the thing it describes". Both artifacts are in
`evidence/discovery/`, and that pair is the strongest argument here for
`status: draft` and human review.

## 3. Determinism & error handling

`cua/replay/` imports no model client — asserted against the live import graph
in a fresh interpreter, not by grepping source.

Per step: **policy → resolve (log tier) → act → outcomes → checkpoint →
recoveries only if the checkpoint failed.** Recoveries last because a detector
evaluated on a healthy screen burns its budget on every successful run — which
it did, in an earlier draft. And **the checkpoint outranks the driver's return
value**: Playwright reports a timeout for a click that was accepted, and the
world is the authority, not the library's opinion of it.

Four results, in the type system, because conflating them is the mistake the
brief names: `Success`, `BusinessOutcome`, `Escalated`, `Failure`.
**`BusinessOutcome.ok` is `False`** — the run worked, the caller did not get a
balance, and code that treats "not found" as a balance is the bug that property
prevents.

Every row below is a live test (`tests/test_taxonomy_live.py`), and one test
asserts all four buckets at once:

| trigger | result |
|---|---|
| `99999` / `10003` | `BusinessOutcome` (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`) |
| `abc` | `Failure(CONTRACT)`, 0 steps — refused before the browser moved |
| `10004`, `/debug/slow`, `/debug/expire` | `Success` via recovery, re-auth, or the checkpoint's own poll |
| `10005` | `Escalated` — an undeclared blocking state |
| `10006` | `Failure(LOCATOR_UNRESOLVED)` |
| wrong start screen / wrong app version | `Failure(PRECONDITION)` / `Failure(INCOMPATIBLE_APP)`, 0 steps |

**There is no `SLOW_LOAD` recovery, deliberately.** It could never fire: a
stall has nothing of its own to detect, the driver blocks until navigation
completes, and a checkpoint polling to a deadline *already is* a bounded retry.
Deleting a recovery that looked thorough and was decorative beat keeping it.

## 4. Heterogeneity & multi-tenant

**Desktop and legacy surfaces — designed, not built.** A driver on UI
Automation or the macOS AX API is a third implementation of five methods;
nothing above that line changes. `A11yNode.role/name/value/ref/box` map onto
`ControlType/Name/ValuePattern/RuntimeId/BoundingRectangle`. Four of five
locator strategies port directly, and `row_cell` gets *better* — UIA has a real
`GridPattern`.

**What does not port is `navigate`**: a desktop app has no URLs. The steps
differ; the schema does not. The design already leans that way for an unrelated
reason — `entry_point` was a path and proved useless on the web too, since the
target app is frameset-based and the URL never changes. The entry condition is
a **landmark** asserted as a checkpoint, which is what a desktop capability
would use. A real defect produced the abstraction the unbuilt surface needs.

**Tenant reuse is a sparse override** — the base it extends, plus only what
differs, merged by step id (never position). A tenant may vary where a control
is; it may not add steps or change inputs and outputs. Every tenant answers to
the same signature, or the catalogue is lying about one of them.

One artifact, three runs (`evidence/tenant-tier-log.md`):

| deployment | s2 rule | tier | |
|---|---|---|---|
| base | `label_proximity` | 0 | |
| riverbend, **no override** | `region_path` | 1 | **degraded** |
| riverbend + override | `label_proximity` | 0 | |

All three return `4210.33`. The unprepared run **succeeds** — so the fallbacks
are not decoration — and **says it degraded** — so the tier log is a real drift
signal. `s5` needed no override at all: keyed on the column *name*, it followed
riverbend's reorder, where its structural fallback would have read the account
number.

**Drift** is detected from data every run already writes — tier, `degraded`,
recovery counts, step duration, and `match_count > 1` (the dangerous one: still
resolves, no longer means one control). `cua drift` reads it back across all 31
bundles on disk and finds exactly the two degraded riverbend runs. Repair is a
field (override), a file (new version), or a re-recording — and
`app.version_range` is enforced before the browser opens, using the version
read off the sign-in screen, the one place legacy software says what it is.

## 5. Escalation & handoff

```
RUNNING → AWAITING_HUMAN → HUMAN_CONTROL → VERIFYING → RUNNING
```

`AWAITING_HUMAN` belongs to **nobody**: the automation stopped, no operator has
picked it up. The automation parks on an `Event`, so a paused run costs a
thread and its browser stays open.

This works only because **a session is an object with an id in a
dictionary**: a browser in a local variable inside a blocked function is
unreachable by anything else, and the brief wants a human in *the same live
session*.

**Control is derived from state, never stored**, so no state can have the
machine driving while a token says otherwise — and it is *enforced*, by a
`GuardedSurface` checked on every `act`. `assert_control` existing and nothing
calling it is how that rule quietly stops being true.

**Never correct the human; always re-verify the world.** Control returns to
`VERIFYING`, not `RUNNING`. The engine re-checks the step's checkpoint and
tells the handler; a failure goes straight back to `AWAITING_HUMAN`. Not
distrust — the person may have done something entirely reasonable and
unrelated, and an automation that assumes otherwise acts on the wrong page.
Nothing compares what they did against what the automation would have done, and
a run resumes fine when they clicked nothing.

**The console shows why the run stopped, not the application** — the live
session is the browser window beside it. "Hand back" returns the token and
**cannot** declare a step complete; a test presses it without fixing anything
and asserts `Escalated`. A missing input becomes a *typed field* validated by
the capability's own `InputSpec`, so the console is not a laxer front door than
the API.

**Where that model stops working, and why it does not matter much.** Acting in
the adjacent window assumes the operator is at the machine the browser is on —
true for attended automation on a desktop, false the moment this runs
server-side, which is the commoner shape. Then you need a streamed viewport or
VNC into the session.

The reason that is a component swap rather than a redesign: **the console
reaches the session only through the control-token API** — `take_control`,
`hand_back`, `abort`. Nothing in it touches the browser. So where the human's
hands are is invisible to the state machine, the engine and the evidence log
alike. That, rather than "we cut the viewport", is the claim worth making.

**Known gap, stated plainly:** human-action capture works in tests and in an
isolated live check, and recorded nothing in one real handoff where a person
clicked while the automation thread was parked. That shape is untested — every
test operator clicks synchronously on the automation's own thread. The state
machine, control token and re-verification are unaffected.

## 6. Safety

**Credentials cannot reach an artifact.** Auth is a bootstrap reading the
environment, never an artifact input — structurally incapable of being
recorded, which beats redacting them well.

**Redaction is declared, never guessed.** Pattern-matching for things that look
sensitive fails in both directions and fails silently. A test asserts that
`member_id` appears **in the clear**, because it is declared `internal` — a
five-digit number beside "member" is what a clever regex would mask, and that
would make every intervention unreadable to the operator. Another flips it to
`pii` and shows it masked.

**Limits, named.** Declared classification only protects a value arriving
under its own name; `learn()` scrubs known values from records written *after*
they are read, and cannot clean an earlier dump. Discovery cannot be protected
by the contract at all, because it *produces* it — a test asserts a name the
model only ever *saw* is still in the log. Screenshots show balances in the
clear. These are governed by retention, not masking.

**Two policy gates** before every act: *where* we may act (origins, paths) and
*what* we may do (action type, risk). Debug endpoints are off the allowlist, so
a run cannot expire its own session. Discovery infers risk pessimistically and
stops when there is nobody to approve.

**Nothing is ever fabricated.** A missing input is a contract failure before
the browser opens, or a question for a person. In an application that writes to
member records, a defaulted value is a correct-looking operation performed on
the wrong person.

## 7. Cuts

**Cut deliberately:** the second capability (`member.open_subaccount`, the
irreversible write path) — the largest gap, though the approval gate it exists
to exercise is built and tested against the read one. The **embedded operator
viewport** — frame transport and latency bought for an ergonomic gain, not a
capability one, with the seam kept clean so it is one component to swap.
**Cross-vendor canonicalisation** — the schema permits it, nothing exercises
it. **Anything that scales** — one process with a dict of sessions.

**What I would build next:** the write capability, so the irreversible path is
exercised by a real capability; a second discovery run against a different
goal, to find the next class of defect the first could not; turning `cua drift`
from a report into something that watches; then the viewport.

**Two things I could not close.** Human-action capture under real concurrency
(§5). And a live test file that once ran in 4771s instead of 106s, never
reproduced — the arithmetic fits five handoff timeouts, I found no mechanism,
and the mitigation is that the test environment now bounds that timeout so the
same situation fails in seconds. An unexplained outlier recorded as unexplained
beats a plausible story.

**A recurring mistake of mine:** three bugs from `x or default` where `x` was
a legitimately empty container, one of which silently made two tests assert
nothing; and a safety check whose regex lost its raw-string prefix, so it
failed *permissively*. Both fail quietly toward "everything is fine", which is
the direction that matters.
