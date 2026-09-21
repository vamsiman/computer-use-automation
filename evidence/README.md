# Evidence

Real runs against the real target application. Nothing here is staged: every
bundle was produced by the actual replay engine driving an actual browser, and
the discovery trace came from an actual model.

Regenerate with `python scripts/make_evidence.py` (and `cua discover` for the
discovery half, which needs an API key).

---

## The genuine discovery run

`discovery/` — the one run with a model in the loop.

| | |
|---|---|
| `trace.json` | every action, with the tree hash and landmark before and after |
| `capabilities/` | the artifact distilled from it |
| `annotation.json` | the single out-of-loop model call that wrote the prose |

```
model   claude-sonnet-5
goal    look up member 10001 and read their current savings balance
ended   done, 4 actions recorded, 3 kept
```

The run completed on its first attempt. It is worth reading the trace beside
`REPORT.md` §2, because **replaying what it produced is what exposed four
defects** — the most interesting of which is that every locator it recorded had
been verified against the live tree, and one of them was still wrong.

### Two discovery bundles, kept on purpose

`runs/` holds both discovery runs, named by their run id:

| | |
|---|---|
| `20260920T224032Z-run-01cc2e` | **before** the fixes |
| `20260920T224944Z-run-e6b666` | **after** |

The first is the evidence for the defects. Its `run.jsonl` carries the
member's balance **in the clear**, because at that point discovery had no
classification to work from — discovery is the thing that produces one.

The second shows the fix and, more usefully, its limit. The balance is a
`[pii:…]` token everywhere, *including inside the model's own prose summary*,
because the run learned it the moment it was extracted. The member's **name**
is still there in that same sentence: the run only ever *saw* it, never
extracted it, so it was never learned. `REPORT.md` §6 states that limit and a
test asserts both halves of it. It is not fixable by masking, which is why
discovery evidence is governed by retention.

---

## Replay bundles

`runs/<name>/` — each containing `run.jsonl` (one record per step, with the
locator tier that resolved), `steps/*.png`, `result.json`, `meta.json`, and
`failure/` where there was one.

| run | result | what it shows |
|---|---|---|
| `01-success` | `Success` | the ordinary path, a different member from the one discovered |
| `02-business-outcome-not-found` | `BusinessOutcome` | `MEMBER_NOT_FOUND` — an answer, not a failure |
| `03-business-outcome-permission` | `BusinessOutcome` | `PERMISSION_DENIED` — the application refused, correctly |
| `04-recovery-interstitial` | `Success` | a declared notice dismissed; the caller never hears about it |
| `05-recovery-session-expired` | `Success` | session dropped mid-flow, re-authenticated, resumed at `s1` |
| `06-escalation-handoff` | `Success` | compliance hold → paused → a person acted → re-verified → finished |
| `07-failure-no-savings-row` | `Failure` | `LOCATOR_UNRESOLVED` — a world the capability was not told about |
| `08-contract-refused` | `Failure` | `CONTRACT`, `steps_executed: 0` — refused before the browser was touched |
| `09-tenant-riverbend-unprepared` | `Success` | the base artifact on a deployment it never saw, **degraded** |
| `10-tenant-riverbend-overridden` | `Success` | the same, repaired by a one-field override |

The four kinds of result are the point. `02` and `07` look similar from
outside and are opposites: one is the system working, the other is the system
broken.

### Worth opening

- **`06-escalation-handoff/run.jsonl`** — the handoff end to end. The state
  changes, the intervention record, and a `human_actions` entry recording that
  a person clicked the button called *Acknowledge*, in the same vocabulary the
  automation's own steps use.
- **`08-contract-refused/result.json`** — `steps_executed: 0`. The cheapest
  check in the system runs first.
- **Any `run.jsonl`** for what is *not* in it: no credentials anywhere, and the
  values the contract declares `pii` appear as `[pii:…]` tokens.

---

## The cross-tenant measurement

`tenant-tier-log.md` — one artifact, three deployments, the same answer, with
the tier each locator resolved on.

This is the file that decides whether the tiered locator chain earns its cost.
Exactly one step degrades, in exactly one of the three runs, and the override
repairs it. See `REPORT.md` §4.

---

## What is not here

No recording of a person using the console. The handoff is covered by
`06-escalation-handoff` and by `tests/test_handoff_live.py`, which drives the
console's own buttons against a live browser — but a screen capture would show
the ergonomics, and there isn't one.
