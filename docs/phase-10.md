# Phase 10: the agent

Phase 9 asked a model one question and got whole files back. That cannot find out anything:
the model never sees the courier's website, only what a person saved for it. From this phase
the model is used **only inside an agent**: it works in steps, with a browser, an HTTP client
and the adapter's files, running the checks itself until they pass. The single-question way
has been removed; there is one way to create or debug an adapter, and it is the agent.

What it produces is unchanged, and that is the point: a fixed adapter that speaks plain HTTP.
The browser and the model are used while the adapter is written or repaired, never while
consignments are tracked.

Everything around the agent is Phase 9 as it was: the Engine opens the task, the service
clones the repository, and after the agent hands its work in, the guard, the sandboxed checks,
the commit and the push decide what is published (see [phase-9.md](phase-9.md)).

## Independent of the model

The agent does not use any supplier's tool-calling feature. In every step the model is sent
the conversation and answers with **one JSON object**:

```json
{"action": "open_page", "url": "https://www.example-courier.in/track"}
```

The service does the action and sends the result back as the next message. Any chat model
that can write a small JSON object can drive it. Which model and which supplier is a setting
(`OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`: any service with the common chat-completions
interface, including a model run locally).

Help for weak models:

- The first JSON object in the reply counts; sentences and code fences around it are ignored.
- For `write_file` the file may follow in a fenced block instead of a JSON string, so a whole
  source file need not be escaped.
- An unusable reply costs a step and is answered with what was wrong.
- Old results are cut to their beginning; the task and the last six exchanges stay whole. A
  small context is enough.

## The actions

| Action | What it does |
|---|---|
| `open_page`, `click`, `type`, `select` | Drive the browser on an approved address |
| `look` | Describe the open page again: text, forms with every field (hidden ones too), links, and the requests the browser made, with what was sent |
| `page_html` | The page's HTML, or one element's |
| `http` | One GET or POST without the browser |
| `save_fixture` | Keep the open page or the last HTTP answer as a test fixture; the consignment number, e-mail addresses and phone numbers are removed first |
| `list_files`, `read_file`, `write_file` | The adapter's files. Reading: anything under `courier-adapters/`. Writing: only the adapter's own files |
| `run_checks` | Code rules, ruff, mypy and the adapter's tests in the sandbox |
| `finish` | Hand the work in. Accepted only if the checks pass |

The list of requests is what makes the browser useful: it shows which request, with which
fields, brought the tracking answer. The adapter repeats that request with `httpx`.

## What the agent cannot do

The agent reads pages written by strangers and is steered by a model. Its limits are code,
not instructions.

- **Where it can go** (`agent/net.py`). Browser and HTTP client reach only the adapter's
  approved hosts: for a repair the hosts the adapter already names, for a new provider the
  hosts the operator named. An address on this machine or in a private network is reachable
  only with the exact port the adapter itself names; so the demo site on `127.0.0.1:8090`
  being approved does not open the Engine's internal API on `127.0.0.1:8081`, the database
  or the broker. Addresses with a user name, other schemes and look-alike hosts are refused.
- **What a page can load.** Documents, form posts and scripted calls only to approved
  addresses. Scripts, styles, pictures and fonts also from public hosts (sites need their
  libraries), never from this machine or a private network.
- **How much.** A fixed number of requests per task (`AI_REPAIR_AGENT_MAX_REQUESTS`, 60) and
  at least a second between two requests to a courier.
- **What it can write.** One adapter's files, in a throwaway clone. Not the SDK, not
  another adapter, not the Engine. A fixture is written by the service, not by the model.
- **What it knows.** No secret. The browser starts with an empty environment and a fresh
  profile; the HTTP client sends no credentials and passes on only a few harmless headers.
  The GitHub token, the model key and the Engine token never enter a message.
- **What gets published.** Not what the agent says is done, but what passes the guard and
  the checks, exactly as in Phase 9.
- **CAPTCHAs and one-time codes.** It is told never to solve or bypass one, and to write the
  adapter so that it hands the challenge to a person (as the demo-site adapter does).

What these limits do not cover: a host name that resolves to a private address, and a model
that is talked into writing a subtly wrong adapter by a page. The second is why the tests
and the review of what is running matter.

## Before you let it visit a courier's site

In agent mode, asking for a new adapter or a repair makes the agent open the courier's
website and, at most twice, look up a consignment. Do this only for sites you may access
this way. Put a real consignment number into `--notes` when you request an adapter; without
one the agent only sees the site's "not found" answer and has to guess how a result looks.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `AI_REPAIR_AGENT_MAX_STEPS` | 40 | Actions per task |
| `AI_REPAIR_AGENT_MAX_REQUESTS` | 60 | Requests to the courier's site per task |
| `AI_REPAIR_AGENT_BROWSER` | `true` | `false`: plain HTTP only |

The browser is installed by `make setup` (Playwright's headless Chromium, in the user's cache
directory, no system change). If it is missing the service says so at start and works with
plain HTTP. On a machine where Chromium does not start for lack of system libraries:

```bash
cd courier-ai-repair
uv run playwright install chromium-headless-shell      # download
sudo uv run playwright install-deps chromium           # system libraries (needs sudo)
```

## Staff answer the challenges

The people who use this system are the company's own staff, not outside customers. So
**every user is a staff member**: they see every open one-time code and CAPTCHA, oldest
first, in the human interface (<http://localhost:8088>) and through `GET /v1/interventions`,
and may answer any of them. Nothing has to be set up for that.

- A challenge takes one answer. If two people answer the same one, the second is refused.
- Who answered is recorded (`human_interventions.answered_by`).
- One user can be restricted to the challenges of their own lookups, for example an account
  a program uses:

  ```bash
  cd courier-engine
  cargo run --quiet -- admin set-staff --email importer@example.com --staff false
  ```

The limits of a challenge are unchanged: its time to answer, three challenges per lookup,
and the answer stored encrypted and deleted once used ([phase-8.md](phase-8.md)).

A one-time code sent to the customer's own phone cannot be answered by staff. Only couriers
whose code reaches your staff are worth adding with such a step.

## A courier is paused while its adapter is repaired

When the Engine opens a repair task for an adapter, further lookups of that courier would
fail one by one. So while the task is waiting or being worked on:

- **A new tracking request is accepted**, but no lookup is started; `latest_job` is `null`.
  The Engine looks again every five minutes and starts the lookup by itself once the repair
  has ended. This works for every provider, also one that is never polled.
- **A manual refresh** is answered with 409 `provider_under_repair`.
- **Automatic polls** are postponed the same way.
- Lookups already running are not touched.

The pause ends when the task is published or has failed, and in any case six hours after
the task was opened: a task nobody works on (the repair service is not running) must not
keep a courier switched off for ever. Tasks for a new provider pause nothing.

After a repair is published, the workers still run the old version until the new commit is
deployed (`make auto-update` does that within minutes). Lookups started in between fail
once more; they do not open another task for the same version.

A repair that cannot go on gives the courier back at once instead of holding it:

- If the model gives no answer three steps in a row (free models are limited per minute and
  per day), the task ends as failed with that reason. It is tried again after six hours, or
  earlier with `admin request-repair`.
- If the service is stopped while it works on a task (Ctrl+C, or the container is stopped),
  it reports the task as failed first.

## The agent in a container

On a server the repair service runs as a container of its own:

```bash
make up-ai DOCKER="sudo docker"        # the stack plus the ai-repair service
```

`courier-ai-repair/Dockerfile` holds the service, the browser and a Python environment of
the adapters with the checking tools. The container receives exactly these settings from
`.env` and nothing else: the repair token, the model key, model and address, the GitHub
repository and token, and dry run. It gets no database, broker or Redis password and
no master key, and it reaches the Engine only through the internal API.

It runs as an unprivileged user with every capability dropped. It needs Docker's
system-call filter switched off (`seccomp=unconfined`), because the sandbox for generated
code and the browser's own sandbox create user namespaces, which the default filter refuses.

**Not verified:** the image has not been built or run; the Compose file validates. On the
first `make up-ai`, check the log (`sudo docker logs courier-ai-repair-1`): the service
refuses to start if the sandbox does not work, and says so.

## Not built yet

- Assigning a challenge to one person at a time (two may open the same one; the second
  answer is refused).
- A web page for managing staff and providers; both are command-line only.
- Load tests. That the design carries thousands of lookups a day is reasoning, not a
  measurement.

## Tests

`tests/test_agent.py`: the action format; the address rules; the real browser against the
demo courier website (page description, noted requests, refused addresses); the file and
HTTP tools; the loop with bad replies and its step limit; and three whole tasks with a
stand-in model that answers from a script: a repair in steps that is refused, fails its own
test, is corrected and published; an agent that never passes and publishes nothing; and an
agent that opens the site in the browser, saves the page as a fixture and tests against it.

Engine: `tests/interventions.rs` (every user sees and answers every open challenge, one
user can be restricted to their own, who answered is recorded) and `tests/repairs.rs` (a provider under
repair accepts requests, starts no lookup, refuses a refresh, and starts the held lookup
when the repair has ended; other tasks pause nothing).
