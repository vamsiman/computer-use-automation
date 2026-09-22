# Recordings

The system doing its job, for people who will not run it. Regenerate with
`python scripts/record_media.py`.

Each scenario has two forms. The **`.gif`** is built from the per-step
screenshots the evidence recorder captures anyway, so it shows exactly the
frames the run recorded — which is a more honest artefact than a video of a
browser somebody could have driven by hand. The **`.webm`** is Playwright's
recording of the whole session, for when you want to watch it move.

The automation is paced at about a second per step. At full speed it is a
blur of three page loads, and a recording nobody can follow demonstrates
nothing.

| | what to watch for |
|---|---|
| `01-happy-path` | the ordinary run: search, open the member, read the balance |
| `02-business-outcome` | `99999` — the application answers "no records found" and the run **stops on purpose**. Nothing is broken |
| `03-escalation-handoff` | the compliance hold. The run pauses, control passes, the button is pressed, control returns, **the engine re-checks the screen**, the run finishes |
| `04-failure` | `10006` — a member with no savings row. This one *is* broken, and the result type says so |
| `05-tenant-degraded` | riverbend, no override. Same capability, renamed field, **still works** |
| `06-tenant-repaired` | the same deployment with a one-field override |

`console-03-escalation-handoff.png` is the operator console photographed while
that intervention was genuinely open — `Take control` greyed out because it had
already been taken, `Hand back` and `Cancel run` live, and the application's own
wording quoted verbatim.

## Two honest notes

**The operator in `03` is scripted.** Playwright stands in for a mouse, on the
thread that owns the browser. Everything around it is the production path — the
pause, the intervention record, the control token, the re-verification — but a
recording that implied a person was sitting there would be a lie about what was
demonstrated. The same handoff driven by an actual human is
`scripts/uat.py --case handoff`.

**These recordings show member names and balances in the clear.** That is the
limit `REPORT.md` §6 names: a balance is visible in a PNG, and no declarative
redaction reaches inside an image. It is why screenshot retention is a config
flag rather than a default, and why in a real deployment these would be
governed by retention rather than by masking. The data here is invented.
