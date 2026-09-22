# Design notes

**`REPORT.md` is the write-up.** This is the long version — the same seven
sections with the reasoning left in, for anyone who wants to know why a
particular decision went the way it did.

Everything below is either implemented or explicitly named as cut. Where a
claim is measurable it is measured, and the measurement is quoted rather than
described.

---

## 1. Architecture

### The shape

```
                    ┌───────────────┐
  goal + inputs ───►│  Discovery    │  model in the loop, ONCE
                    │  observe/     │
                    │  decide/act   │
                    └───────┬───────┘
                            │ trace
                    ┌───────▼───────┐
                    │ Distillation  │  prune, parameterise, annotate
                    └───────┬───────┘
                            │
                    ┌───────▼───────┐
                    │  Artifact     │  typed, versioned, reviewable YAML
                    │  (draft)      │  ── a person approves it ──►
                    └───────┬───────┘
                            │
  inputs ───────────►┌──────▼────────┐
                     │   Replay      │  NO model, ever
                     │   engine      │
                     └──────┬────────┘
                            │
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
          Policy        Surface        Evidence
       (allowlist,   (a11y tree,     (redacted log,
        risk)         locators)       screenshots)
                            │
                     ┌──────▼────────┐
                     │   Session     │  control token, state machine
                     │   manager     │
                     └──────┬────────┘
                            │ when it stops
                     ┌──────▼────────┐
                     │   Operator    │  grant / hand back / cancel
                     │   console     │
                     └───────────────┘
```

### The seam everything rests on

`Surface` is five methods — `observe`, `resolve`, `act`, `screenshot`, `close`.
Neither discovery nor replay imports Playwright; neither knows a browser
exists. A desktop driver built on UI Automation is a third implementation of
those five methods and nothing above the line changes.

That is only true because of **what crosses the boundary**: an accessibility
tree and a locator chain, both of which mean the same thing on a web page and
on a native window. Had the currency been CSS selectors or pixel coordinates,
the seam would be decorative. This is the single most consequential decision in
the system and it was made at the type level, before any driver was written.

### Perception: the accessibility tree, not pixels

The model never sees a screenshot and never sees raw HTML. It sees a compact
text rendering of an accessibility tree, built by a script injected into every
frame that computes a role and an accessible name in ARIA precedence order and
stamps each kept node with a `data-cua-ref`.

Playwright ships `page.accessibility.snapshot()` and we deliberately do not use
it: it is deprecated, it returns no element handles so there is no way to act on
what you found, and it cannot cross frame boundaries. The target application
puts its entire working area inside an `<iframe>`, so that last point alone
rules it out.

Three things follow, and they are why this choice resolves the determinism
tension rather than merely being tidy:

1. **Perception is cheap.** A tree is a few hundred tokens where a screenshot
   is thousands.
2. **What the model points at is a *named control*,** which can be recorded
   semantically. A model pointing at a pixel gives you a locator that breaks
   when the page reflows; a model pointing at "the button called Search" gives
   you one that survives a redesign.
3. **The same tree is how replay finds things.** Discovery and replay perceive
   identically, so a locator that worked at record time is being evaluated the
   same way at replay time.

Screenshots are evidence, and the last-resort locator tier. Nothing decides
anything from a pixel.

### The model seam

`ModelClient` has exactly one method: `reply(system, messages, tools) ->
ModelReply`. That is why the entire discovery loop is tested against the live
application with a rule-based operator reading the same rendered tree. Stuck
detection, policy denial, stale refs, locator synthesis, the trace, the
distillation — all covered offline. **Only the model's judgement is unproven by
tests**, which is the right thing to leave unproven, and is why the one genuine
run matters (§7).

---

## 2. Artifact schema

The focal point of the evaluation, so the reasoning is worth setting out.

### What it is

A YAML document on the filesystem at
`capabilities/<id>/<version>.yaml`, committed to git. Filesystem rather than a
database because the brief requires artifacts be *reviewable*, and a YAML diff
in a pull request is the most reviewable form there is.

```yaml
capability:  id, version, title, description, app, tenant, extends, status, provenance
inputs:      name -> {type, pattern, required, sensitivity, description}
outputs:     name -> {type, sensitivity, currency}
preconditions: {authenticated, entry_point, entry_checkpoint}
steps[]:     {id, intent, action, target: Locator, args, risk, checkpoint}
success:     {checkpoint, require_outputs[]}
outcomes[]:  {code, detect, terminal, message}       # answers, not errors
recoveries[]:{code, detect, strategy, max_occurrences, ...}
```

### Locators are a chain, and the tier that resolved is logged

```yaml
target:
  primary:   {strategy: label_proximity, label: "Member ID:", direction: right}
  fallbacks:
    - {strategy: region_path, region: "form[0]", path: "table[0]/row[0]/cell[2]/textbox[0]"}
```

Five strategies, most durable first: `role_name`, `label_proximity`,
`row_cell`, `region_path`, `anchor_offset`. The tier that resolved is returned
in the result, **on successful runs too** — a capability sliding from its
primary rule to its third fallback is degrading weeks before it breaks, and
nobody reads the logs of runs that worked.

`row_cell` sits above `region_path` deliberately, and §4 measures why: *"the
Balance on the Savings row"* survives a tenant that reorders its columns, and
*"row 1, cell 2"* does not fail there — it reads the wrong column and returns a
number that looks like an answer. **A rule that can be confidently wrong
belongs below one that can only be right or absent.**

### Locators are synthesised and verified at record time

This is the load-bearing idea in discovery. The model points at a control by
its `ref`; `synthesize()` turns that into a tiered semantic locator, and
**every spec is verified against the live tree before it is written down** —
it must resolve to exactly one node, and that node must be the one the model
pointed at, by identity.

So a discovered artifact contains only locators that were *executed* against
the real application. That is a much stronger claim than a design where a
second model call invents plausible selectors after the fact and nothing ever
checks them. It has to happen here, too: this is the only moment the live tree
and the model's choice exist together.

### A locator may not be defined by the data it reads

The first genuine discovery run produced this chain for the balance:

```
primary   role_name       cell "4,210.33"           <- the value it is reading
fallback  label_proximity label "1,287.50"          <- another member's balance
fallback  region_path     table[6] row[2]/cell[2]
fallback  row_cell        row "Savings", col "Balance"
```

Every one of those verified. It worked perfectly — on member 10001 and on no
other. Replaying with 10002 succeeded, fell to tier 3, and reported itself
**degraded**: the drift signal firing on the first replay of a brand-new
artifact.

**Verification held and was not enough.** At record time a circular locator *is*
a correct locator; it finds exactly the right node, exactly once. The missing
property was never "does this resolve" but "is this description independent of
the thing it describes". Synthesis now discards such specs for extract targets
— discarded rather than demoted, because a circular spec at the end of a chain
either fails or quietly resolves to some *other* row holding the number we
remembered.

Both artifacts are in `evidence/discovery/`. This pair is the strongest
argument in the repository for `status: draft` and human review: discovery
produced a locator that passed every check and was still wrong.

### The same lesson bit the hand-written artifact

The reference artifact's search-field step had an aspirational
`role_name: "Member ID"` primary above the rule that actually works. It never
resolved, so **every healthy run reported itself degraded** and the tier log
stopped meaning anything. Removed. A chain contains rules that have been seen
to work and nothing else — which discovery does automatically, since it can
only record what it verified.

### Provenance and status

```yaml
provenance: {discovered_at, model, run_id, step_count_raw: 4, step_count_final: 3}
status: draft
```

`step_count_raw` vs `step_count_final` is distillation made visible. Everything
distilled is `draft`; `cua approve` is the only thing that promotes it, and it
is a person typing a command. A capability that approved itself would make the
status decorative.

### The annotation pass is a writer, never a decider

One model call, outside the action loop, proposes the id, title, description
and per-step intents. `apply()` copies across **only** those fields, so a
hostile annotation cannot change behaviour — and there is a test that feeds it
one. It is optional: every action already carries the intent written at
decision time, so the artifact validates before annotation runs.

---

## 3. Determinism and error handling

### Replay imports no model client

Asserted by a test that walks the module graph. Determinism by construction
rather than by discipline.

### The order of operations, per step

```
contract check (once, before the browser is touched)
entry checkpoint (once)
  └─ per step:
       policy check
       resolve locator, log the tier
       act
       evaluate outcomes           ← business answers first
       verify checkpoint
       evaluate recoveries         ← ONLY if the checkpoint failed
```

Two of those placements are load-bearing.

**Recoveries are consulted only when the checkpoint has failed.** A recovery is
a response to something having gone wrong; evaluating them on a healthy screen
is how a detector that matches the working page burns its budget on every
successful run. It did, in an earlier draft, which is how the rule was found.

**The checkpoint outranks the driver's return value.** Playwright times out
waiting for a slow navigation and reports `ok=False` for a click that was
accepted. The world is the authority on whether a step worked, not the
library's opinion of it. Where no checkpoint is declared the driver's word is
all we have — which is itself the argument for declaring one.

### Four results, in the type system

```python
Success(outputs, ...)              # it did what it promises
BusinessOutcome(code, message)     # the application gave a legitimate answer
Escalated(intervention_id, ...)    # a person has to decide; the session is alive
Failure(category, expected, observed)  # the automation is broken
```

The brief calls conflating these the most common mistake in this problem, so
the distinction is a type rather than a status string somebody has to remember
to check. `Success.ok` is `True`; **`BusinessOutcome.ok` is `False`** — the run
worked, and the caller did not get a balance, and code that treats "not found"
as a balance is the bug that property exists to prevent.

### The taxonomy, demonstrated

Every row is a live test against the real application
(`tests/test_taxonomy_live.py`), and one test asserts all four buckets at once.

| trigger | result | mechanism |
|---|---|---|
| `10001` / `10002` | `Success` | — |
| `99999` | `BusinessOutcome(MEMBER_NOT_FOUND)` | declared outcome |
| `10003` | `BusinessOutcome(PERMISSION_DENIED)` | declared outcome |
| `abc` | `Failure(CONTRACT)`, `steps_executed == 0` | the input contract |
| `abc` (pattern relaxed) | `BusinessOutcome(VALIDATION_ERROR)` | the application's own check |
| `10004` | `Success` | `SYSTEM_NOTICE` dismiss recovery |
| `/debug/slow` | `Success` | the checkpoint's own bounded poll |
| `/debug/expire` | `Success` | re-authenticate, resume at `s1` |
| `10005` | `Escalated` | undeclared blocking state |
| `10006` | `Failure(LOCATOR_UNRESOLVED)` | no tier resolved |
| wrong starting screen | `Failure(PRECONDITION)`, 0 steps | entry checkpoint |

Two rows deserve a note.

**`abc` twice.** The capability declares `^[0-9]{5}$`, so the contract refuses
it before a browser opens. Two layers validate and the cheap one runs first —
checking an argument costs nothing, finding out from the application costs a
sign-in and a round trip. The application remains the only authority on what it
accepts, which the second row demonstrates with the pattern relaxed.

**There is no `SLOW_LOAD` recovery, deliberately.** It could never fire. A stall
has nothing of its own to detect — the symptom is the *absence* of what you are
waiting for — and the driver's observation blocks until navigation completes, so
the half-loaded screen a detector would key on does not exist at any observable
moment. A checkpoint that polls to a deadline **already is** a bounded retry.
The strategy stays in the vocabulary for applications that render an explicit
wait screen, with a test against one. Deleting a recovery that looked thorough
and was decorative is a better answer than keeping it.

### Budgets

Each recovery declares `max_occurrences`; past it the condition stops being a
hiccup and becomes the problem (`RECOVERY_EXHAUSTED`). A global action budget
of `len(steps) * 4 + 10` stops two recoveries feeding each other, which a
per-code budget cannot catch.

---

## 4. Heterogeneity and multi-tenancy

Section 3.7 of the brief asks this to be *designed* rather than built, and
names three questions. Two of them are answered by working code below; the
first is answered by a seam that exists and a driver that does not.

### Extending to another surface (designed, not built)

The `Surface` protocol is five methods. A desktop driver on Windows UI
Automation or the macOS AX API is a third implementation of them, and
**nothing above that line changes** — not the artifact schema, not the replay
engine, not the error taxonomy, not the session and control model, not the
console.

That is only true because of what crosses the seam. An accessibility tree and
a locator chain mean the same thing on a web page and on a native window; CSS
selectors and pixel coordinates would not. The mapping is close to mechanical:

| ours | UI Automation | AX API |
|---|---|---|
| `A11yNode.role` | `ControlType` | `AXRole` |
| `.name` | `Name` | `AXTitle` / `AXDescription` |
| `.value` | `ValuePattern` | `AXValue` |
| `.ref` | `RuntimeId` | element reference |
| `.box` | `BoundingRectangle` | `AXFrame` |

Four of the five locator strategies port directly, and `row_cell` gets
*better*: UIA has an explicit `GridPattern`, so "the Balance on the Savings
row" stops being an inference over `<table>` markup and becomes a query.

**What genuinely does not port is `navigate`.** A desktop application has no
URLs. On a legacy web app the first step is "go to this path"; on a desktop
app it is "focus this window, open this menu". This is the one place the
action vocabulary is web-shaped, and it is worth being precise about the
consequence: the *steps* differ, the *schema* does not.

The design already leans this way for a reason unrelated to desktop.
`preconditions.entry_point` is a path, and it turned out to be useless even on
the web — the target app is frameset-based, so the URL is identical on every
screen. The entry condition is therefore a **landmark**, asserted as a
checkpoint: *"this capability starts on the screen headed Member Search."*
That formulation needs no URL and is exactly what a desktop capability would
use. A real defect on the web produced the abstraction desktop would need.

Two other seams were built duck-typed for the same reason. `page_source()` is
not in the protocol, because a desktop surface has no markup and requiring one
would make the protocol describe a browser; the recorder asks for it and does
without. The same is true of the human-action watcher: a driver that cannot
report what a person did says so, and the log records that it could not rather
than implying they did nothing.

**What would have to be written:** a `DesktopSurface` (observe/resolve/act/
screenshot/close), a role vocabulary mapping, and a policy allowlist keyed on
application identity and window titles rather than origins and paths. Perhaps
a week. **What would not:** everything else in this document.

### Multi-tenant reuse

One vendor product, two deployments. Riverbend calls the field "Member
Number" instead of "Member ID" and puts its Balance column second instead of
third. Nothing about the *task* differs.

A tenant artifact is a **sparse document** — it names the base it extends and
lists only what differs. Merging is by step id, never by position: a positional
merge silently rewrites the wrong step the moment anybody inserts one, in a
document that still looks correct in review.

What a tenant may *not* do is the important half: no new steps, no changed
actions, and above all **no changed inputs or outputs**. Every tenant of a
capability answers to the same signature, or the catalogue is lying about at
least one of them.

### The measurement

One artifact, three runs (`tests/test_tenant_live.py`,
`evidence/tenant-tier-log.md`):

| deployment | step | rule | tier | |
|---|---|---|---|---|
| base | s2 | `label_proximity` | 0 | |
| base | s5 | `row_cell` | 0 | |
| **riverbend, no override** | **s2** | **`region_path`** | **1** | **degraded** |
| riverbend, no override | s5 | `row_cell` | 0 | |
| riverbend + override | s2 | `label_proximity` | 0 | |
| riverbend + override | s5 | `row_cell` | 0 | |

All three return `4210.33`.

This is the whole argument for the tiered chain, and it is a measurement rather
than an assertion:

- The unprepared run **succeeds**. If it had failed, the fallbacks would be
  decoration and the honest design would be one artifact per tenant.
- It **says it degraded**. If it had succeeded silently, the tier log would not
  be a drift signal.
- The override — one field — returns it to its primary rule.
- **`s5` needed no override at all.** It is keyed on the column *name*, so it
  followed the reorder. Its structural fallback would also have resolved, and
  read the account number. That contrast is why `row_cell` outranks
  `region_path`.

### Detecting and managing drift

The brief asks how per-tenant and per-version drift is detected and managed,
which is a different question from whether a run passed.

**Detection is already collected on every run, including successful ones**,
which is the part that matters — nobody reads the logs of runs that worked.

| signal | where | what it means |
|---|---|---|
| locator tier | `tier_log`, every step | a step on its third fallback is degrading weeks before it breaks |
| `degraded` | `result.degraded_steps` | the primary rule stopped resolving |
| recovery counts | `result.recoveries` | a notice firing twice as often this month as last is a changed application |
| per-step `duration_ms` | `run.jsonl` | a page that got slower is drift the checkpoints are absorbing |
| `match_count > 1` | `Resolution` | a locator that used to be unique now is not — the dangerous one, because it still resolves |

The tenant measurement above is this working: the same artifact against a
deployment it was not written for succeeds **and reports the degradation**,
which is the difference between "it still works" and "it still works, and
something changed underneath it".

**Management** has three tiers, and the artifact format is what makes them
cheap. A tenant whose step has degraded gets a **sparse override** of that one
step — the unit of repair is a field, not a document. A vendor version that
breaks many tenants gets a **new artifact version**, with the old one still on
disk and still runnable, because versions are files. Drift past what an
override can express means **re-running discovery for that tenant**, and the
result is a YAML diff in a pull request, which is the most reviewable form a
behavioural change can take.

**Both halves are now wired up**, because each was a promise the code had not
kept.

`app.version_range` is enforced before the browser is touched. The version is
read off the **sign-in screen**, which is the one place legacy software says
what it is and the only screen a run is guaranteed to see — so the auth
bootstrap captures it and the engine refuses a capability recorded for a
version this deployment is not:

```
Failure(INCOMPATIBLE_APP), 0 steps
  this capability was recorded for >=9.0, and the application reports
  'MemberConsole 4.3.1'. Re-record the capability against this version, or
  widen the range if it is known to be compatible.
```

The check is conservative in **one direction only**. An *unknown* version never
blocks a run: most applications never say what they are, and a system that
refused to work whenever it could not identify the software would be useless in
exactly the legacy estate it is for. A *known* version outside a *declared*
range does block, because at that point both facts are in hand and continuing
is a choice to ignore one.

`cua drift` reads the tier logs back across every bundle on disk. Against the
31 runs in `evidence/`, it finds what it should:

```
member.read_savings_balance@1.0.0  s2  label_proximity/region_path  2/26 degraded
member.read_savings_balance@1.0.0  s3  role_name                    0/26 healthy
```

Those two degraded runs are the riverbend deployment. The signal the design has
been claiming since §2 is now legible without reading a single log file.

It is a **report, not a monitor** — no daemon, no thresholds, no alerting. The
brief is explicit that building scaling infrastructure is not rewarded, and the
claim worth proving was that the answer is derivable from evidence already on
disk.

### Canonicalisation, honestly

This is the §8 stretch goal and it is only half done. Two deployments of one
product are reconciled by a sparse override. Two genuinely different vendor
products exposing the same business capability would need a shared interface
above the artifact — the same `id` and signature, different documents per
product — which the schema already permits (`app.vendor`, `app.product`) and
nothing has been built to exercise.

---

## 5. Escalation and handoff

Weighted fourth, and explicitly "not just a TODO".

### The sequence

```
RUNNING ──► AWAITING_HUMAN ──► HUMAN_CONTROL ──► VERIFYING ──► RUNNING
```

`AWAITING_HUMAN` belongs to **nobody**: the automation has stopped and no
operator has picked it up yet. That is a real state, not a gap. The automation
parks on a `threading.Event`, so a paused run costs a thread and nothing else,
and its browser stays open.

### Why this was possible at all

A session is an object with an id, held in a dictionary. That single change is
the architectural move: a browser held in a local variable inside a blocked
function is unreachable by anything else, and the brief asks for a human to
take over *the same live session, not a fresh one*.

### Control is derived, never stored

There is exactly one variable — the run state — and control is a function of
it. There is no state in which the machine thinks automation is driving while a
token says a human is. That class of bug is simply unavailable.

And the token is **enforced**, not documented: the engine is handed a
`GuardedSurface` that checks it on every `act`. `assert_control` existing and
nothing calling it is how that rule quietly stops being true. Only `act` is
guarded — the console must be able to observe the page precisely while a person
is driving it.

### Never correct the human; always re-verify the world

`HUMAN_CONTROL → VERIFYING`, not straight back to `RUNNING`. The engine
re-checks the step's checkpoint and then tells the handler the answer, and
*that* call is what moves the session onward. If it failed, the session goes
straight back to `AWAITING_HUMAN`.

This is not distrust of the person. They are free to do something entirely
reasonable and unrelated — fix the record, navigate away, answer a different
question — and an automation that assumes the screen it wanted is now in front
of it will act on the wrong page. Nothing anywhere compares what the human did
against what the automation would have done, and **a run resumes perfectly well
when they clicked nothing at all**.

### What the person did is recorded

A handoff would otherwise be the one gap in the evidence: a step that failed, a
silence, and then a step that worked. `watch.js` records every click, change
and edit in the same vocabulary the automation uses — by literally the same
code, shared with the tree builder, so the human's actions and the automation's
steps read against each other in one log.

The log lives in the tab's `sessionStorage`, not in a page variable. The first
thing a person does is press the button that navigates, which would throw away
the one action worth recording.

Watching starts at the **pause**, not at the grant: somebody who reaches over
and clicks before the paperwork is done has still acted on the live session.

### The console, and what it may not do

A small page listing paused runs with the capability, the step and its intent,
why it stopped, the application's own words verbatim, the redacted parameters,
and the accessibility tree the automation actually had. Three buttons: take
control, hand back, cancel.

**"Hand back" returns the token. It cannot declare a step complete.** A test
presses it without fixing anything and asserts the result is `Escalated`, not
`Success`. A button that could mark a step done is a button that can be wrong
about a live banking session.

### A missing input is a question, not a dead end

If a required parameter is absent and somebody is there to ask, the run asks —
as a **typed field validated by the capability's own `InputSpec`**, the same
code that checks an API caller's arguments. The console cannot be a laxer front
door than the API.

An *ill-typed* value is deliberately **not** turned into a question: the caller
believes they supplied it and they are wrong, and asking a person to retype it
hides a bug in whatever called us. Nothing is ever defaulted. This application
writes to member records, and a defaulted member number is a correct-looking
operation performed on the wrong person.

### The embedded viewport: designed, not built

The console does not show the application. The live session is the Chromium
window already open beside it, which is why browsers run headed.

The alternative — stream the page into the console so the operator never leaves
it — is frame transport, input forwarding, latency and a second rendering of an
already-rendered page, bought for an ergonomic gain rather than a capability
one. The brief puts co-browsing out of scope.

The seam is kept honest so that decision stays cheap to reverse: **the console
reaches the session only through the control-token API.** Swapping a screenshot
for a streamed interactive viewport changes that one component and nothing
else.

---

## 6. Safety

### Credentials cannot reach an artifact

Authentication is a **bootstrap**, not recorded steps. Credentials come from
the environment and are never an artifact input, so they are *structurally
incapable* of ending up in a saved artifact. That is a stronger guarantee than
redacting them well. The `SESSION_EXPIRED` recovery calls the same bootstrap.

`Credentials.__repr__` masks the password — not cosmetic: a dataclass repr
containing a password ends up in tracebacks and crash reports without anyone
deciding it should.

### Redaction is declared, never guessed

The tempting build is a list of regular expressions for things that look
sensitive. That fails in both directions at once: it misses the member name
that is sensitive because of what it *is*, and mangles the balance that is not.
Worse, it fails silently.

So the `Redactor` is built from the artifact's own `sensitivity` map. **A test
asserts that `member_id` appears in an intervention in the clear**, because it
is declared `internal` — a five-digit number beside the word "member" is
exactly what a clever regex would mask, and masking it would make every
intervention unreadable to the operator who has to act on it. A second test
flips the declaration to `pii` and shows it masked everywhere.

PII becomes a salted digest rather than a constant, so two records about
different members stay distinguishable. Its limit is worth stating plainly:
this is not encryption, and against a five-digit space anyone holding the salt
can enumerate it in milliseconds. It guards against casual exposure of a log
file, not against an adversary with the log and the salt.

### The leak that declared classification cannot catch

Classification protects a value arriving under its own name. A `pii` output
quoted inside a free-text failure reason arrives nameless — and went out in the
clear until a test that walks every byte of a run bundle caught it.
`Recorder.learn()` now scrubs known values from everything written *afterwards*.

The ordering is the honest limit: it cannot clean a page dump taken earlier.
That is why the failure bundle has its own retention switch rather than being
assumed safe.

### Discovery cannot be protected by the contract

Because discovery is what *produces* the contract. `Redactor.for_discovery()`
assumes what the run **reads** is sensitive and learns each extracted value
before the record containing it is written.

The residue is real and is asserted by a test: a member name the model only
ever *saw* and quoted in its own prose was never extracted, so it was never
learned, and it is in the log. Discovery is a high-exposure operation by
nature; the mitigation for the rest is retention, not masking, and a test that
pretended otherwise would be worse than the leak.

Screenshots are the other known limit — a balance is visible in a PNG — which
is why retention is a config flag.

### Two independent policy gates

*Where* we may act (origins, paths) and *what* we may do (action types, risk
level), evaluated **before** every act. A flow can be on an allowed page and
still be blocked from submitting an irreversible form. The debug endpoints are
off the allowlist, so a run cannot expire its own session.

Discovery infers risk **pessimistically**: any action on a control named
`confirm|submit|commit|delete|transfer|post` is irreversible until a human says
otherwise, and a `RequireApproval` with nobody to ask ends the run rather than
proceeding.

### Nothing is ever fabricated

A missing required input is a contract failure before the browser opens, or a
question for a person. Never a default, never an empty string. In an
application that writes to member records, a fabricated input is a
correct-looking operation performed on the wrong person.

---

## 7. What was cut, and what I would do next

**Cut deliberately:**

- **The embedded viewport** (§5). Designed, with the seam kept clean.
- **The second capability.** `member.open_subaccount` — the write path with an
  irreversible final step — is specified and not built. The approval gate it
  exists to exercise *is* built and tested (`RequireApproval`, `_pause_for_approval`),
  but against the read capability. This is the largest single gap.
- **Cross-vendor canonicalisation** (§4). Half done.
- **Anything that scales.** No queue, no database, no multi-process anything.
  The brief says breadth and scaling infrastructure are not rewarded, and one
  process with a dict of sessions is the honest shape for the demo.

**The one real LLM run.** Done, and it earned its place: it completed on the
first attempt and then exposed four defects no test could have found (§2). The
scripted operator used in the offline tests never makes the choices a real
model makes. If there is one process lesson here, it is that the real run
should happen *before* three more things are built on top of the loop, not
after.

**What I would do next, in order:**

1. Build `member.open_subaccount`, so the irreversible-write path is exercised
   by a real capability rather than by a unit test of the gate.
2. A second discovery run against a *different* goal, to find the next class of
   defect the first one could not.
3. Turn `cua drift` from a report into something that watches — a threshold, a
   place to send the answer. The reading is done; the noticing is not.
4. The embedded viewport, if operators ask for it.

**One thing I could not explain.** A live test file once ran in 4771s instead
of ~106s. It did not reproduce; the same code runs in 106s and the arithmetic
fits five handoff timeouts, but I could find no mechanism and I am not claiming
one. The mitigation is that the test environment now bounds the handoff timeout
so the same situation fails in seconds and names itself. An unexplained outlier
recorded as unexplained is better than a plausible story.

**A recurring mistake of mine, worth naming once.** Three separate bugs this
project came from `x or default` where `x` was a legitimately empty container —
an empty registry, an empty inputs dict. Two were in tests, and one of those
silently made two tests assert nothing. In Python that idiom is a trap wherever
emptiness is meaningful, and "is None" is the only safe form.
