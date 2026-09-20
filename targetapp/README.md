# Stand-in target: Meridian Trust FCU — MemberConsole

A fake credit-union back-office console. This is the **target** of the automation,
not part of it.

We are not given access to a real bank system and must not try to obtain one. A
public demo site was rejected for a specific reason: the interesting failures in
this problem are runtime conditions — a validation error, a permission denial, a
session timeout, an unexpected dialog — and you cannot make someone else's demo
site produce those on demand. Owning the target is what makes the error taxonomy
demonstrable rather than theoretical.

## Running it

```bash
python -m targetapp.seed                              # (re)build cu.db
python -m flask --app targetapp.app run --port 5000
```

Then open <http://localhost:5000/> and sign in.

| Setting | Env var | Default |
|---|---|---|
| User ID | `CUA_APP_USER` | `teller1` |
| Password | `CUA_APP_PASS` | `demo-pass-2024` |
| Tenant | `TENANT` | `base` |

The automation's auth bootstrap reads the same two variables, which is how
credentials stay out of capability artifacts entirely.

`cu.db` is gitignored and rebuilt from `seed.py`, so the demo always starts from a
known state.

## Screens

```
/                                        Sign In
/home                                    shell — nav + <iframe name="mainFrame">
/members/search                          Member Search
/members/<no>                            Member Details — info block + accounts grid
/members/<no>/subaccount/new             Open Sub-Account — 4-field form
/members/<no>/subaccount/review          Confirm Sub-Account
/members/<no>/subaccount/commit   POST   irreversible; creates the account
/logout
```

Two flows, chosen to exercise different halves of the problem:

- **read** — search a member, read a balance off a grid. Every step is safe.
- **write** — fill a form, reach a confirmation screen, commit. The final Confirm
  is genuinely irreversible, which is what gives the risk model something real to
  refuse.

## Seed data

| Member | Name | Accounts | Notes |
|---|---|---|---|
| 10001 | Dana Whitfield | Savings 4,210.33 · Checking 1,287.50 | primary happy path |
| 10002 | Marcus Ellery | Savings 982.14 · Checking 453.00 | second input, proves parameterisation |
| 10003 | Priya Raghavan | Savings 15,600.22 | flagged `restricted` |
| 10004 | Tomas Lindqvist | Savings 739.15 · Checking 211.88 | flagged `notice` |
| 10005 | Aisha Bello | Savings 22,047.19 | flagged `compliance_hold` |
| 10006 | Grant Okonkwo | Checking 662.40 | **no savings account** |
| 10007 | Nina Castellanos | Savings 3,105.00 · Money Market 75,000.00 | |
| 10008 | Rafael Osei | Savings 148.75 | |
| 10009 | Beatrix Lund | Savings 2.03 | status Dormant |
| 10010 | Hyun-woo Park | Savings 8,923.40 · Checking 1,730.96 | |

Every name is invented. There is no real PII in this project.

`10006` matters: the member exists but the thing we came to read does not. That is
a different failure from "no such member" and the system has to report it
differently.

## Trigger table

The brief is explicit that the hard part of replay is not layout drift — these UIs
are stable — but the runtime conditions that legitimately occur. So every one of
them is reachable here, deterministically, on demand. This is the payoff for
owning the target: you cannot ask someone else's server to expire your session on
cue.

| Trigger | What happens | Class |
|---|---|---|
| `10001`, `10002` | Member details with balances | happy path |
| `abc`, `123`, `""`, `1234567` | "Member ID must be 5 digits." | business outcome — `VALIDATION_ERROR` |
| `99999` | "No records found." | business outcome — `MEMBER_NOT_FOUND` |
| `10003` | "You are not authorized to view this member." | business outcome — `PERMISSION_DENIED` |
| `10004` | `System Notice` interstitial before the detail | **recoverable** — dismiss via `Continue` |
| `GET /debug/slow?ms=8000` | One-shot stall on the next render | **recoverable** — retry with backoff |
| `GET /debug/expire` | Session dropped; sign-in renders *in the frame* | **recoverable** — re-authenticate and resume |
| `10005` | `Compliance Hold`, single `Acknowledge` button | **undeclared** — escalate to a human |
| `10006` | Member visible, but no savings row to read | hard failure — extract target absent |
| 3 failed sign-ins | Account locks; correct password stops working | hard failure — survives a fresh session |
| `GET /debug/reset` | Clears lockouts, dismissals and arming flags | — |

Message strings live in `exceptional.py` and are part of the contract: artifacts
declare detectors against them, so changing the wording breaks every artifact
recorded against this app. That is itself a faithful reproduction of what a vendor
upgrade does to real automation.

### Why these particular distinctions

**Malformed vs unknown.** `abc` and `99999` are different conditions. One means the
caller sent garbage; the other is a legitimate answer about the world. Validation
runs before lookup so the two can never be conflated.

**Notice vs hold.** `10004` and `10005` render through the *same template*, with the
same roles, the same control ids and the same layout. They differ only by name.
That is deliberate: a recovery declared for `dialog "System Notice"` must not
swallow a compliance hold by accident. One is noise to click through unattended;
the other is a blocking condition a person has to decide about. Making them
structurally identical stops a detector from being right by luck.

Nothing in the app stops automation pressing `Acknowledge` — the server cannot tell
who is clicking. Keeping that button out of unattended reach is the automation's
job, which is precisely the seam `10005` exists to exercise.

**Lockout survives a fresh session.** Deliberately module-level rather than
session-scoped, so retrying with new cookies does not clear it. That makes it a
hard failure for the auth bootstrap rather than something a recovery can paper
over.

## The markup is hostile on purpose

A system that only works against clean markup proves nothing about the environment
in the brief. So this app is built the way the real ones are:

| What | Why it matters |
|---|---|
| Working area inside `<iframe name="mainFrame">` | A driver reading only the top document sees a nav menu and nothing else. The surface layer has to walk frames and merge them. |
| Nested table layout, spacer cells, no semantic containers | Nothing structural to anchor to. |
| ASP.NET ids (`ctl00_ContentPlaceHolder1_txtMbrId`) | Generated, meaningless, and not stable across vendor upgrades. |
| Member-ID field has a bare `<span>` label | No `<label for>`, no `aria-label`, no title, no placeholder — its accessible name is **empty**, so role+name cannot find it. |
| Accounts grid header row is `<td>`, not `<th>` | No column semantics. "The balance on the savings row" has to be resolved structurally. |
| `javascript:__doPostBack(...)` anchors | Cannot be followed as a URL. The automation has to genuinely click. |
| No test ids anywhere | Legacy enterprise apps essentially never have them. |

### …but not uniformly hostile

Deliberately, some things resolve cleanly:

- page titles are real `<h2>` headings, so checkpoints have something stable
- the Search button is a plain submit input, so its value is a real accessible name
- **Initial Deposit** on the sub-account form has a real `<label for>`, sitting
  right next to three fields that don't

A surface where *nothing* resolved semantically would be a strawman, and the
accessibility-tree approach would look wrong. A surface where *everything* did
would make the locator fallback chain decoration. Real enterprise apps are a mix
of vintages, so this one is too — and the mix is what lets a single screen
demonstrate two locator tiers side by side.

`tests/test_targetapp_flow.py` asserts the hostile properties directly, so a later
tidy-up that adds a label or a `<th>` fails the build rather than quietly making
the demonstration easier than the problem.

## Tenants

`TENANT` selects a config in `tenants.py`: branding, field wording, and the
accounts grid column list. Grid rows render from that column list rather than a
fixed template, so a tenant reordering its columns is a config change here and a
genuine targeting problem for the automation.

Only `base` exists today. The `riverbend` variant — same vendor product, different
branding, `Member ID:` renamed to `Member Number:`, columns reordered — is added
with the cross-tenant reuse work, where the point is to replay one artifact
against both.

## Browser setup

The surface tests need Chromium **and** the headless shell, which is a separate
download:

```bash
playwright install chromium
playwright install chromium-headless-shell   # only needed for CUA_HEADLESS=1
```

Without them, `pytest -m "not browser"` still runs everything else.
