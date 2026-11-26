# Phase 9 — AI repair service

When an adapter stops being able to read its courier's answers, or when a courier has no
adapter yet, an AI model writes the code. The repair service checks that code, tests it in
a sandbox and pushes it to the `main` branch on GitHub, so every device gets it with
`git pull`.

**Nobody reviews the code before it reaches `main`.** That was chosen deliberately for this
phase. The automatic checks described below are the only gate. Read
[What can still go wrong](#what-can-still-go-wrong) before switching the service on.

## What happens, step by step

```
adapter cannot read an answer          operator: admin request-adapter
        │ job.result FAILURE with the answer         │
        ▼                                            ▼
Engine: adapter_failure_samples ──3 lookups──► repair_tasks (pending)
                                                     │  POST /internal/v1/repairs/claim
                                                     ▼
                                        AI repair service (separate program)
        1 fresh clone of the repository from GitHub
        2 scrub the answers (tracking number, e-mail addresses, phone numbers)
        3 ask the model through OpenRouter for whole files
        4 guard: fixed rules on the proposed files, before anything is written
        5 sandbox: ruff, mypy and the adapter's tests, no network, no secrets
          (a rejected or failing proposal goes back to the model, 3 rounds at most)
        6 commit only the files that belong to the task
        7 git push origin HEAD:main   (never forced)
                                                     │  POST /internal/v1/repairs/{id}/result
                                                     ▼
                                   repair_tasks: published (commit) or failed (reason)
```

### 1. The adapter hands over what it could not read

An adapter calls `ctx.keep_evidence(response)` on the courier's tracking answer before
reading it. If the lookup then fails with `PARSE_ERROR` or `SCHEMA_DRIFT`, the worker puts
that answer into the `job.result` message (`raw_response`, which the contract already
allowed on failures). For every other outcome nothing extra is sent. Login and token
answers are never kept.

### 2. The Engine records it and decides when a repair is due

The Engine stores the answer in `adapter_failure_samples` (the ten most recent per adapter
are kept). It opens a task in `repair_tasks` when

- three different lookups of the same adapter version failed this way within 24 hours, and
- no task for that adapter is waiting or running, and
- no repair was already published for that adapter version (workers that have not been
  updated yet keep failing; that must not start a second repair), and
- no repair attempt for it failed in the last 6 hours.

The Engine runs no AI and executes no generated code. It only keeps the list.

### 3. The repair service does the work

It is a separate program (`courier-ai-repair/`) that runs on a machine of yours, outside
Docker Compose. It has **no** access to PostgreSQL, RabbitMQ or Redis and holds no courier
credentials. It reaches the Engine through two routes of the internal API, with its own
token, which opens nothing else:

| Route | Purpose |
|---|---|
| `POST /internal/v1/repairs/claim` | the next task with up to three unreadable answers, or `204` |
| `POST /internal/v1/repairs/{id}/result` | `published` with the commit, or `failed` with the reason |

A task that was claimed and never reported (the service crashed) is handed out again after
two hours.

## The checks generated code must pass

### The guard (`ai_repair/guard.py`)

Fixed rules, applied to the proposed files before one of them is written to disk:

- **Files.** Only `courier-adapters/adapters/<key>/*.py`,
  `courier-adapters/tests/test_<key>_adapter.py` and
  `courier-adapters/tests/test_<key>_repairs.py` (where a repair adds its tests, so that it
  need not send a long existing test file back). Not the SDK, not another adapter, not the
  Engine, not CI, not configuration.
- **Imports.** An exact list: `os`, `re`, `typing`, `datetime`, `json`, `dataclasses`,
  `httpx`, `bs4`, the adapter SDK's public modules, the shared helpers `india` and
  `tables`, and the adapter's own modules. Tests may also use `pytest`, `pathlib`,
  `base64`, `urllib.parse` and the shared test helper. A file that already exists keeps
  what its current version imports and the hosts it names (a person wrote or accepted
  those), except modules that start programs or open connections. A test in
  `tests/test_guard.py` proves that every existing adapter could be repaired within the rules.
- **No escape hatches.** `eval`, `exec`, `open`, `getattr`, `__import__`, attributes that
  start with two underscores, file writing, starting programs.
- **Hosts.** Every web address written in the code must belong to the adapter's existing
  hosts (repair) or to the hosts the operator named (new provider). The endpoint addresses
  stay in the adapters, as before; the model cannot add one.
- **Environment.** Only `os.environ.get("<KEY>_…")`. A worker's token or the session key
  cannot be read.
- **Shape.** The adapter keeps its key, raises its version, still calls `keep_evidence`;
  no existing test disappears (tests are compared by name), and the tests must use the
  recorded answers as fixtures.

### The sandbox (`ai_repair/sandbox.py`)

The first time generated code runs is when its tests run. That happens under
[bubblewrap](https://github.com/containers/bubblewrap):

- its own network namespace: no Engine, database, RabbitMQ, Redis or internet;
- an empty environment: none of the service's tokens;
- only system libraries, the Python environment and the clone are visible, read-only while
  tests run; no home directory, no `.env`, no `.git` of your checkout;
- a time limit.

If bubblewrap is missing or not permitted, the service refuses to start. There is no mode
without a sandbox.

What runs inside: `ruff check`, `mypy --strict`, the adapter's tests, and the SDK's
evidence and contract tests.

### Publishing (`ai_repair/workspace.py`)

The work happens in a fresh clone that is deleted afterwards. Exactly the files of the task
are committed; if anything else changed, the task fails. The commit is made as
`courier-ai-repair` and says in its message that an AI model wrote it and that nobody
reviewed it. The push is never forced: if `main` moved in the meantime, the push fails, the
task fails, and the Engine opens a new one later.

The GitHub token is given to Git through its environment for the clone and the push only.
It is not in an address, a file, a command line or a log, and never inside the sandbox.

## New providers

```bash
cd courier-engine
cargo run -- admin request-adapter \
  --code bluedart --name "Blue Dart" --method api \
  --hosts apigateway.bluedart.com --needs-credential \
  --notes "GET /in/transportation/tracking/v1/shipment?awb=<number>; header JWTToken; answer has ShipmentData.Shipment[].Scans[]" \
  --sample-file ~/bluedart-answer.json
```

`--method` is `api` or `scrape`. `--hosts` are the only hosts the new adapter may contact.
`--sample-file` is an example answer; without one the model works from the notes alone and
marks its reading as provisional.

The model writes only the adapter and its tests. Everything else a provider needs is
produced from templates (`ai_repair/scaffold.py`), exactly as for the existing providers:

- the job queue and dead-letter queue in `courier-contracts/topology.yaml`;
- a numbered migration with the provider, the adapter and the seven status mappings of
  `adapters/india.py`;
- a worker service in `infrastructure/docker-compose.yml` (profile `couriers`).

The new provider cannot be used right away, like every other: an API provider needs
`admin set-credential` (and its worker a token), a website provider needs
`admin set-scraping-authorized` after the site's owner agreed.

A repair can also be requested by hand, as soon as one unreadable answer is stored:
`cargo run -- admin request-repair --adapter balaji`. `make repairs` lists the tasks.

## Setting it up

You need three secrets. All go into `.env` in the repository root, which Git ignores.

### 1. The repair token (shared by Engine and repair service)

```bash
make repair-token          # prints 64 random characters
```

Put it into `.env` as `ENGINE_REPAIR_TOKEN=…` and restart the Engine. Without it the two
repair routes refuse everybody.

### 2. OpenRouter

1. Create an account at <https://openrouter.ai> and add credit (Settings → Credits).
2. Create a key at <https://openrouter.ai/keys>. Give it a spending limit.
3. Optionally choose a model at <https://openrouter.ai/models> and copy its name exactly as
   shown there.

```
OPENROUTER_API_KEY=sk-or-v1-…
OPENROUTER_MODEL=<the model's name>      # optional
```

**Free models.** Without `OPENROUTER_MODEL` the service uses free models, best first:

```
baidu/ernie-5.5-flash-preview:free,particle-ai/particle-1:free,openrouter/free
```

`OPENROUTER_MODEL` takes one name or up to three separated by commas. The first is used; the
others only when it is unavailable or rate-limited. The last entry, `openrouter/free`, is
OpenRouter's own pick among whatever is free at that moment.

The order comes from a trial on 25 November 2026: every free text model OpenRouter offered
was run through this service on its own two kinds of task, in dry-run mode, three rounds at
most. "Passed" means the code got through the guard, ruff, mypy and its tests in the sandbox.

| Model | New adapter | Repair | Notes |
|---|---|---|---|
| `baidu/ernie-5.5-flash-preview:free` | passed, round 1 | passed, round 3 | the only one that passed both |
| `particle-ai/particle-1:free` | passed, round 1 | no result | repair run stopped by the rate limit |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | passed, round 2 | failed | its own test failed |
| `nvidia/nemotron-3.5-super-vl:free` | passed, round 3 | failed | its own test failed |
| `nvidia/nemotron-3.5-lightning:free` | failed | not run | wrote invalid Python |
| `inclusionai/ling-3.2-flash:free`, `google/gemma-4-31b-it:free`, `google/gemma-4-26b-a4b-it:free` | no result | not run | rate-limited before answering |
| `recursive/flint-v1.0:free`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | no result | not run | too slow; stopped |

This is one run per model on two tasks, so it shows which models can do the work, not how
often. Free models are also added and withdrawn without notice: look at
<https://openrouter.ai/models> now and then. A name that no longer exists does not stop the
service; it falls back to the last entry of the list.

Three more things to know about free models:

- Free models are rate-limited per minute and per day, for the whole account. The service
  waits and repeats a refused call (`AI_REPAIR_LLM_RETRIES`, default 3, 30 seconds apart),
  and an answer that never comes costs one of the task's rounds.
- They are weaker at writing code than paid models. Expect some tasks to end as "no
  acceptable proposal after 3 rounds". Nothing is published in that case.
- Providers of free models may keep and train on what is sent to them, and OpenRouter
  offers them only if the account's privacy settings allow that. What is sent here is
  adapter source code and scrubbed courier answers.

### 3. GitHub token

A *fine-grained* personal access token that can do one thing: write the contents of this
one repository.

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token.
2. Repository access: **Only select repositories** → this repository.
3. Permissions → Repository permissions → **Contents: Read and write**. Nothing else.
4. Set an expiry and copy the token.

```
AI_REPAIR_GITHUB_REPO=<owner>/<repository>
AI_REPAIR_GITHUB_TOKEN=github_pat_…
```

If `main` has a branch-protection rule that requires pull requests, the push is refused.
Either leave `main` unprotected (what "push directly" means), or set
`AI_REPAIR_PUSH_BRANCH=ai-repair` and merge that branch yourself.

### 4. Run it

```bash
sudo apt install bubblewrap        # once; you do this, the service only uses it
make setup                         # creates courier-ai-repair/.venv and courier-adapters/.venv
make run-ai-repair                 # asks the Engine for work every 60 seconds
```

The service starts in dry-run mode: everything happens except the push, and the task is
reported as failed with the note "dry run". Set `AI_REPAIR_DRY_RUN=false` in `.env` when
finished work should really be pushed.

### On the other devices

Nobody has to watch GitHub. On every device that runs the system, leave this running:

```bash
make auto-update                       # or: make auto-update DOCKER="sudo docker"
```

`infrastructure/auto-update.sh` looks at GitHub every two minutes. When `main` has new
commits it lists them, fast-forwards this checkout, runs the contract and adapter checks
(`make test-contracts test-adapters`, about half a minute) and, if they pass, runs
`make up`, which rebuilds the images and restarts what changed.

- It only fast-forwards. Local commits or changes that stand in the way are never
  discarded: it says so and waits for you.
- If the checks fail, the new code is in the folder but is **not** deployed; what is
  running stays as it was, and it waits for a newer commit. This is the only check a
  second device makes of code that no person reviewed.
- If deploying fails (Docker or the network), it tries again at the next look.
- `AUTO_UPDATE_INTERVAL_S`, `AUTO_UPDATE_CHECK`, `AUTO_UPDATE_DEPLOY` and
  `AUTO_UPDATE_BRANCH` change the interval, the checks, the deploy command and the branch.
  `infrastructure/auto-update.sh --once` looks once and exits (for cron or a systemd timer).
- With `sudo docker`, `sudo` asks for the password again when its grace period is over,
  so an unattended device needs Docker usable without `sudo`.
- The worker of a **new** provider is in the `couriers` profile and is not started by
  `make up`; start it with `make up-couriers` once the provider may be used.

By hand it is the same two steps:

```bash
git pull
make up            # rebuilds the Engine and worker images
```

A repaired adapter needs its worker restarted. A new provider also brings a migration and
new queues, so the Engine must be rebuilt and restarted as well (both are compiled into it).

## What can still go wrong

- **No human review.** The guard reads code; it cannot judge whether a parser is *right*.
  A model can write an adapter that passes its own tests and still reads a courier's page
  wrongly. The tests are written by the same model. Watch `make repairs` and the commits on
  GitHub.
- **The guard is not a proof.** It stops the direct routes: forbidden imports, other files,
  foreign hosts written as text. Python is flexible enough that a determined author could
  assemble an address from pieces at run time. Provider answers are untrusted and could
  contain text aimed at the model. The sandbox contains such code while it is tested; once
  it is on `main` and a worker runs it, it has what a worker has: network access and, for
  an API provider, that provider's credential. If that risk is not acceptable, set
  `AI_REPAIR_PUSH_BRANCH` to a branch and review before merging.
- **Data leaves the system.** The unreadable answers are sent to OpenRouter and to the
  model's provider, and are committed to the repository as test fixtures. The scrubber
  removes the tracking number, e-mail addresses and Indian phone numbers. It does **not**
  recognize names or street addresses. If the repository is public, so are the fixtures.
- **A new provider's adapter is untested against the real courier**, exactly like the
  hand-written ones were. The gates (credential, scraping permission) still apply.
- **Cost.** Every round sends the adapter's source and the answers to the model. Three
  rounds at most per task, one task per adapter at a time, six hours between failed
  attempts. Set a spending limit on the OpenRouter key.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ENGINE_REPAIR_TOKEN` | — | shared by Engine and service; at least 32 characters |
| `OPENROUTER_API_KEY` | — | required |
| `OPENROUTER_MODEL` | two free models, then `openrouter/free` | the model that writes the code; several names = first choice and fallbacks |
| `AI_REPAIR_GITHUB_REPO`, `AI_REPAIR_GITHUB_TOKEN` | — | required |
| `AI_REPAIR_PUSH_BRANCH` | `main` | where finished work is pushed |
| `AI_REPAIR_DRY_RUN` | `true` | `true`: everything except the push. Set `false` to publish |
| `AI_REPAIR_MAX_ROUNDS` | `3` | corrections the model may attempt |
| `AI_REPAIR_POLL_S` | `60` | seconds between questions to the Engine |
| `AI_REPAIR_ENGINE_URL` | `http://127.0.0.1:8081` | the Engine's internal API |
| `AI_REPAIR_SANDBOX_TIMEOUT_S` | `300` | limit for one check |

## Tests

`make test-ai-repair` (109 tests) and `courier-engine/tests/repairs.rs` (8 tests). The
end-to-end tests of the service use a copy of this repository as a local Git remote, the
real sandbox and the real adapter tests; only the model is replaced by scripted answers.
No test calls OpenRouter or GitHub.
