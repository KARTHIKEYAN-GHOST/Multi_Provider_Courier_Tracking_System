# Whole-system test (live, with Claude)

The whole system was run from top to bottom twice on 2026-11-27 on this machine: the real Engine,
real workers, PostgreSQL, RabbitMQ, Redis, the demo courier website, the AI repair service
with Claude (`claude-code`, `claude-opus-5-5`) pushing to GitHub, and the automatic update
pulling, checking and deploying. Nothing was simulated.

The stack used its own database (`courier_systest`), its own queue prefix (`systest.`) and
its own ports (Engine 18080/18081, human interface 18088), so the development stack and its
database were not touched. Users: an organization user (`user@systest.example`, organization
"Systest Traders") and an admin (`ops@systest.example`).

# First run

## Results

| # | Scenario | Result |
|---|---|---|
| 1 | The adapter exists and works | passed |
| 2 | The courier asks for a one-time code or a CAPTCHA; a person answers | passed |
| 3 | The adapter exists but breaks; the AI repairs it by itself | passed |
| 4 | The courier has no adapter; the AI builds it by itself | passed |

### 1. The adapter exists and works

The Engine reads the courier and its adapter from the database and sends the job to the
adapter's queue; the worker fetches the data; the Engine stores it.

| Number | Courier | Job | Shipment | Raw answers | Events |
|---|---|---|---|---|---|
| DUMMY-OK-SYS1 | dummy (API-like) | COMPLETED | DELIVERED | 1 (JSON) | 3 |
| DEMO-OK-SYS1 | demosite (website) | COMPLETED | DELIVERED | 1 (HTML) | 5 |
| DEMO-TRANSIT-SYS1 | demosite | COMPLETED | IN_TRANSIT | 1 (HTML) | 3 |

Both forms are stored: `raw_tracking_responses` keeps the courier's answer exactly as
received (the whole HTML page), and `tracking_events` the formatted events (time, the
courier's own status text, the normalized status, the parsed place).

### 2. A person answers the one-time code and the CAPTCHA

- `DEMO-OTP-SYS2` and `DEMO-CAPTCHA-SYS3` stopped in `WAITING_FOR_OTP` / `WAITING_FOR_CAPTCHA`.
- The organization user is refused the challenge list (403); only the admin sees it.
- The admin (the project owner, at the human interface) read the code from the demo site's
  pretend phone and the digits from the picture, and answered both.
- Both lookups then completed (`SUCCESS`, IN_TRANSIT, 1 raw page and 3 events each). Each
  answer was fetched by the worker once and audited (`intervention.resolve`).
- In between, the workers were restarted by a deploy: the lookups continued from their
  saved sessions instead of starting over.

### 3. The adapter breaks; the AI repairs it

| Time | What happened |
|---|---|
| 17:01 | Three different shipments (`DEMO-NEWLAYOUT-SYS1..3`) get the redesigned page: `SCHEMA_DRIFT` |
| 17:02 | The Engine opens a repair task for `demosite` by itself (third shipment in 24 hours) |
| 17:02–17:05 | Claude reads the adapter and the failing pages, rewrites the parser to read the new timeline layout as well as the old table, raises the version to 0.2.1, adds tests; the sandbox runs them |
| 17:05 | The AI service pushes `d71f99b` to `main`; no permission was asked |
| 17:05 | The automatic update pulls it, runs the contract and adapter checks, rebuilds, migrates and restarts the Engine and the workers |
| 17:06 | A refresh of `DEMO-NEWLAYOUT-SYS1` and a new `DEMO-NEWLAYOUT-SYS5` complete (IN_TRANSIT, raw page and 3 events each) |

### 4. The courier has no adapter; the AI builds it

| Time | What happened |
|---|---|
| 17:01 | "Demo Express" is inserted straight into `providers` (status `building`, a tracking address, notes on how to track) |
| 17:01 | The organization user tracks `DEMO-TRANSIT-SYS4` with it: accepted, `provider_status: building`, no job yet |
| 17:01 | The Engine opens a `new_provider` task by itself |
| 17:05–17:22 | Claude opens the site in the browser, saves pages as fixtures (consignment numbers removed), writes the adapter, its parser and tests; the scaffold adds the queues, the worker service and the migration `0021_provider_demoexpress.sql`; the sandbox runs the checks |
| 17:22 | The AI service pushes `fbf0678` to `main` |
| 17:22 | The automatic update pulls it, checks, migrates (the courier becomes `live`) and starts the new `demoexpress` worker with its own token |
| 17:23 | The waiting request starts by itself and completes: IN_TRANSIT, the raw HTML page and 3 events |

## Problems found, and what was done

1. **GitHub's checks failed on the AI's repair commit `d71f99b`** (AI repair service and
   end-to-end), although the repair itself was right. Two older tests assumed the demo-site
   adapter would never change:
   - an AI-service test needed the adapter at exactly version 0.2.0 and replaced the
     adapter's repair-test file; after the real repair the code rules rightly refused that
     (a repair may never remove tests). It now raises whatever version the adapter has and
     keeps the earlier repair's tests.
   - an end-to-end test used the redesigned page as its example of an unreadable page, which
     the repaired adapter now reads. The demo site has a new page, `DEMO-SCRAMBLED-*`, that
     no adapter version knows; the test uses it for `SCHEMA_DRIFT` and checks that the
     redesigned page now succeeds.

   Why it got deployed anyway: the automatic update runs the contract and adapter checks and
   the sandbox runs the adapter's tests; the AI-service and end-to-end suites run only on
   GitHub. The deployed adapter was correct; the failing checks were in the tests.
2. **A repair task can be left `running` with nobody on it** when the AI service stops in the
   middle (here: the test stack's deploy script stopped it; on the Docker stack it does not,
   since `make deploy` does not restart the AI service). The Engine hands such a task out
   again after 2 hours (`CLAIM_TIMEOUT_HOURS`). Recovery worked as designed; the task then
   finished on its second try.

## The automated suites afterwards

1005 tests pass on the code after the two AI commits and the fixes: 99 contract, 210 Engine,
456 adapter (30 more: Demo Express's and the demo site's repair tests), 31 human interface,
150 AI repair service, 59 end-to-end. The end-to-end test of a courier added by a user failed
once in a full run (it waits on the 60-second recheck of waiting requests) and passed in
every run since.

# Second run: adapters removed, so the agent does the work again

Before the second run, the AI-built code was taken out of `main` (`e4774bd`): Demo Express
was deleted entirely (adapter, tests, queues, worker, migration) and the demo-site adapter was
rolled back to 0.2.0, as before the AI's repair. A new demo courier was added to the demo
site, **Demo Secure**, whose tracking page needs a sign-in (form with a one-time token,
session cookie, its own page layout), so the agent had to build an adapter that logs in.

Fresh stack: database `courier_systest2`, queue prefix `systest2.`, its own demo site on port
18090 (current code; the Demo Secure password random, only in a private file).

## Results

| # | Scenario | Result |
|---|---|---|
| 1 | Existing adapters (dummy, demo site; also "not found") | passed |
| 2a | CAPTCHA answered by a person | passed |
| 2b | One-time code answered by a person | not passed: every answer was wrong (see below) |
| 3 | The adapter breaks; the AI repairs it | passed |
| 4 | No adapter (Demo Express, put into the database); the AI builds it | passed on its third try (two system problems found and fixed on the way) |
| 5 | No adapter and a login (Demo Secure); the AI builds it with the login | passed |
| 6 | Couriers with local tracking addresses through the API | refused (422) |

### Timeline

| Time | What happened |
|---|---|
| 17:55 | Three redesigned pages: `SCHEMA_DRIFT`; the Engine opens a repair of `demosite` |
| 17:55 | Demo Express inserted into the database; a user's request waits (a refresh gets `adapter_being_built`); its build task opens |
| 17:58 | Demo Secure inserted (stopped), its login stored by the organization user through `PUT /v1/providers/demosecure/login` (encrypted, the organization's own), `admin retry-provider` starts the build; a request waits |
| 17:58 | Claude's repair of the demo site published (`3c99248`), deployed 17:59; the failed shipment and a new one complete |
| 18:02 | Demo Express try 1 fails: GitHub cut the AI service's clone off (`early EOF`), not retried |
| 18:08 | Claude's Demo Secure adapter published (`79ba431`): it signs in with `{{login.email}}`/`{{login.password}}` while building, and with `ctx.credentials()` in the adapter. Deployed 18:09; the waiting request completes 11 seconds later |
| 18:16 | Demo Express try 2 fails at the push: `main` had moved meanwhile (a fix pushed by the operator) |
| 18:20 | AI service restarted with the fixes below |
| 18:36 | Demo Express try 3 published (`bf3183d`), deployed 18:41; the request that waited since 17:55 completes (IN_TRANSIT, raw page, 3 events) |

All AI commits of this run passed every GitHub check.

### The login was never exposed

- The password is in no commit, no file of the checkout, no AI log or working folder, and no
  database table (raw pages included); the AI even replaced the account's email by a
  placeholder in the saved pages.
- Audited: `credential.set` (by the user), `credential.agent_use` (the AI, once, while its
  task ran), `credential.resolve` (the worker).
- The lookup used the organization's own login; the shipment is shared only inside that
  organization (`access_scope = org:...`).

### The one-time code

Four OTP lookups failed: each new challenge was answered with a wrong code within 2 to 13
seconds of being created, three times in a row (then `CHALLENGE_LIMIT_EXCEEDED`), from the
admin's key. The system's side was checked: the deployed adapter gets the tracking page with
the right code and the "try again" page with a wrong one, the pretend phone shows the code
the site expects, and the CAPTCHA (same path) was answered and completed. The answers came
faster than a person reads and types a code; the cause was outside the system and is to be
retried. Wrong answers were handled as they should be: a new challenge saying the previous
answer was not accepted, and a stop after three.

## Problems found, and what was done

1. **Security: the agent could be pointed at this machine** (`e4774bd`). For a new courier
   the agent may reach the exact tracking address even on this machine, assumed to be set
   only by the operator. But any API user can register a public-looking name that resolves
   to 127.0.0.1, with any port (the Engine's internal API, for example). Now the exception
   holds only for an address written as a local one (IP, `localhost`), which the API refuses.
2. **A refused login was reported as `SCHEMA_DRIFT`** by the adapter the AI wrote (tested
   live with a wrong stored password). Three of those would send a working adapter to be
   repaired. Cause: the agent's rules never said what a refused login is. They now say
   `AuthInvalid`, and ask the agent to try a wrong password once while building (`16e4ece`).
   The adapter was fixed by hand (`1dbbeed`, 0.1.1): it raised before keeping the page, so no
   sample was stored and no repair task could be opened for it. Live again: wrong password
   `AUTH_INVALID`, right password data.
3. **A finished adapter was thrown away because `main` moved during the task** (Demo Express
   try 2). Now the work is replayed on top of what was pushed meanwhile, checked again and
   pushed, never forced; a conflict, a migration number taken meanwhile or failing checks
   still fail the task (`b65d427`).
4. **A clone cut off by GitHub was not retried** (Demo Express try 1: "unexpected disconnect",
   "early EOF" were not recognized as network trouble). Now they are (`b65d427`).
5. **With the demo-site adapter rolled back, an end-to-end test paused the courier for the
   tests after it**: its third unreadable shipment opened a repair. The test no longer uses
   the page whose result depends on the adapter's version (`e4774bd`).
6. The test stack's deploy no longer stops the AI service (the first run's stuck task).

## The automated suites after the second run

1033 tests pass: 99 contract, 210 Engine, 474 adapter, 31 human interface, 160 AI repair
service, 59 end-to-end.

## Not covered by these runs

- The Docker parts (`make deploy`, `make worker-tokens`, the containers) are run by the
  owner, since the test machine's shell has no Docker access. The same steps were run as
  local processes.
- No real courier website was scraped (that needs its owner's permission); the AI built and
  repaired adapters for the demo site.
- The one-time code by a person passed in the first run; in the second it was answered
  wrongly (see above) and is to be retried.
- The automatic update deploys what it pulls; a commit made in the same checkout is already
  there and is deployed with `make deploy` by hand.

## Repeating it

The scenarios and the commands are in [phase-12.md](phase-12.md) ("Step by step"). On the
demo site: `DEMO-OK-*`, `DEMO-TRANSIT-*` (work), `DEMO-OTP-*`, `DEMO-CAPTCHA-*` (a person
answers; the code is at `http://127.0.0.1:8090/inbox?number=...`), `DEMO-NEWLAYOUT-*` (the
redesigned page; read once an AI repair taught the adapter), `DEMO-SCRAMBLED-*` (unreadable by
any version). Demo Secure: `/secure/track?number=` behind `/secure/login`, with the account in
`DEMO_SITE_SECURE_EMAIL` and `DEMO_SITE_SECURE_PASSWORD` (without a password it answers 503).
