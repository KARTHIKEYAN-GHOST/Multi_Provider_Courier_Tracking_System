# Whole-system test (live, with Claude)

On 2026-11-27 the whole system was run from top to bottom on this machine: the real Engine,
real workers, PostgreSQL, RabbitMQ, Redis, the demo courier website, the AI repair service
with Claude (`claude-code`, `claude-opus-5-5`) pushing to GitHub, and the automatic update
pulling, checking and deploying. Nothing was simulated.

The stack used its own database (`courier_systest`), its own queue prefix (`systest.`) and
its own ports (Engine 18080/18081, human interface 18088), so the development stack and its
database were not touched. Users: an organization user (`user@systest.example`, organization
"Systest Traders") and an admin (`ops@systest.example`).

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

## Not covered by this run

- The Docker parts (`make deploy`, `make worker-tokens`, the containers) are run by the
  owner, since the test machine's shell has no Docker access. The same steps were run as
  local processes.
- No real courier website was scraped (that needs its owner's permission); the AI built and
  repaired adapters for the demo site.
- The courier's login through placeholders was not exercised live (Demo Express has no
  login); it is covered by the automated tests.

## Repeating it

The scenarios and the commands are in [phase-12.md](phase-12.md) ("Step by step"). On the
demo site: `DEMO-OK-*`, `DEMO-TRANSIT-*` (work), `DEMO-OTP-*`, `DEMO-CAPTCHA-*` (a person
answers; the code is at `http://127.0.0.1:8090/inbox?number=...`), `DEMO-NEWLAYOUT-*` (the
redesigned page; read since demosite 0.2.1), `DEMO-SCRAMBLED-*` (unreadable by any version).
