# Phase 12: couriers without an adapter; the AI builds the adapter by itself

Until now a courier was usable only if somebody had written its adapter, and a new adapter
was asked for with an admin command. Now:

1. **Anyone signed in adds a courier**: an organization user or an admin, through the API
   (or an admin on the command line, or a row put straight into the database). It is stored
   with how it is tracked: method (API or website), tracking address, notes, the website
   owner's permission, and optionally **a login** (email and password, or an API key).
2. **The Engine asks the AI for its adapter by itself.** No admin step: a courier in
   `building` gets a new-adapter task within a second or two.
3. **Tracking requests for it are accepted and wait.** They start by themselves when the
   adapter is live.
4. **The AI may use the login while it builds or repairs**, through placeholders. It logs
   in, sees the logged-in pages and the login errors, and tests against the real site, but
   never sees the values.
5. **The adapter goes into service by itself.** The AI pushes to GitHub; the automatic
   update pulls, checks, migrates, gives the new worker its token and starts it.
6. **More reasons to repair automatically**: a login that worked is now refused, or
   shipments that were found are now "not found".
7. **Each organization can keep its own login for a courier.** Its users' lookups use it,
   and those lookups are shared only inside the organization.

One-time codes and CAPTCHAs are still answered only by a person (an admin); everything else
is automatic.

## The flow

```
user/admin ──POST /v1/providers──▶ Engine: provider "building" (+ login, encrypted)
                                     │  (scheduler, every pass)
                                     ▼
                               repair task "new_provider" ──▶ AI repair service
user ──POST /v1/tracking-requests──▶ accepted, waits             │ claims it
                                                                  │ fetches the login (only while running, audited)
                                                                  │ agent: browser/HTTP with {{login.email}} ...
                                                                  │ checks in the sandbox
                                                                  ▼
                                                          git push to main
                                                                  │
                                   automatic update on the server ◀┘
                                   (pull, check, make deploy: migrate,
                                    worker token, start worker)
                                     │
                                     ▼
                     provider "live" ──▶ the waiting lookups start; the worker logs in
                                          with the stored login and fetches the data
```

| Status | Meaning | A new request | A refresh |
|---|---|---|---|
| `building` | its adapter is being written | 201, waits (`provider_status: "building"`) | 409 `adapter_being_built` |
| `live` | it has an adapter | as always | as always |
| `failed` | three tries failed (`build_failure` says why) | 409 `adapter_build_failed` | 409 `adapter_build_failed` |

After a failed try the Engine waits 10 minutes and tries again; after the third it marks
the courier `failed`. `admin retry-provider --code X` starts a new round of three tries;
the requests that waited wait again.

## Adding a courier

### Through the API (organization users and admins)

```bash
curl -s -X POST http://127.0.0.1:8080/v1/providers \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
        "name": "Shree Courier",
        "method": "scrape",
        "tracking_url": "https://www.shreecourier.example/track",
        "notes": "Log in, type the docket number in the box, press Track. A table of scans appears. Test number: SC123456.",
        "permitted": true,
        "login": { "email": "ops@yourcompany.example", "password": "..." }
      }'
# 202 {"code": "shreecourier", "status": "building"}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | the courier's name; the code is made from it unless `code` is given |
| `method` | yes | `api` or `scrape` (a website) |
| `tracking_url` | yes | the tracking page or the API address; its host is the one the AI and the adapter may contact |
| `hosts` | no | up to four more hosts it may contact (for example a separate login host) |

Hosts must be public names: an IP address or an internal name (`.local`, `.internal`,
`.lan`, ...) is refused, and the agent also refuses a name that resolves to an address on
this machine or in a private network, so nobody can point the AI at the deployment itself.
| `notes` | no | how it is tracked, in your words; the AI reads it |
| `permitted` | for a website | `true`: its owner has permitted automatic lookups. Who said so, and when, is recorded |
| `login` | no | the login, stored encrypted; for an organization user it is **that organization's own** login, for an admin the platform's |
| `needs_login` | no | it needs a login that you will store later (`PUT /v1/providers/{code}/login`) |
| `sample` | no | an example answer (JSON or HTML), if you have one |

An organization may add five couriers per day (admins: no limit). The answer never contains
the login; `GET /v1/providers` lists every courier with its `status`.

### A login later, or a new password

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/providers/shreecourier/login \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"login": {"email": "ops@yourcompany.example", "password": "..."}}'     # 204
```

### On the command line (admins)

```bash
cd courier-engine
echo '{"email": "ops@yourcompany.example", "password": "..."}' > /tmp/login.json   # or an editor
cargo run --quiet -- admin add-provider --name "Shree Courier" --method scrape \
     --tracking-url https://www.shreecourier.example/track --permitted \
     --notes "Log in, type the docket number, press Track" --login-file /tmp/login.json
shred -u /tmp/login.json
cargo run --quiet -- admin list-providers       # code, status, method, name
cargo run --quiet -- admin retry-provider --code shreecourier
cargo run --quiet -- admin set-credential --provider shreecourier --organization "Acme Traders" < login.json
```

`request-adapter` (Phase 9) still works: it is the old name of `add-provider`.

### Straight into the database

```sql
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms,
                       result_grace_ms, max_attempts, retry_base_ms, retry_max_ms,
                       status, tracking_url, notes, scraping_authorized)
VALUES (gen_random_uuid(), 'shreecourier', 'Shree Courier', 'scrape', 20000, 900000, 30000,
        3, 5000, 300000, 'building', 'https://www.shreecourier.example/track',
        'Log in, type the docket number, press Track', true);
```

The Engine picks it up on its next pass. (A login cannot be inserted this way: it is
encrypted; use the API or `set-credential`.)

## The login and the AI

The AI repair service asks the Engine for the login of the task it is working on
(`POST /internal/v1/repairs/{task}/login`, repair token). The Engine hands it out **only
while that task is running**, and writes an audit row each time (`credential.agent_use`,
without the value). The service keeps it in memory for that task only.

The agent is told the field names, never the values:

```
{"action": "http", "method": "POST", "url": "https://www.shreecourier.example/login",
 "form": {"email": "{{login.email}}", "password": "{{login.password}}"}}
{"action": "type", "selector": "#password", "text": "{{login.password}}"}
```

- Values are filled in only when the request is sent.
- Every value, also URL-encoded, HTML-escaped or JSON-escaped, is replaced by its
  placeholder in **everything the model reads back**: pages, answers, errors, refusals.
- Fixtures go into Git: they are cleaned the same way.
- An `Authorization` or API-key header is sent only when its value is made from the login's
  placeholders, never with a key the agent found somewhere (for example in a page's script).
- The login never enters the sandbox, Git, the task, the logs or the model.

The adapter the AI writes reads the login at run time with `await ctx.credentials()`, as
the DHL adapter does with its key. A one-time code or CAPTCHA at login is never answered by
code: the adapter asks a person (an admin), as the demo-site adapter does.

## Which login a lookup uses

| The user's organization | Login used | Shipment shared with |
|---|---|---|
| has its own login for the courier | the organization's | users of that organization only (`access_scope = org:<id>`) |
| has none | the platform's (stored by an admin), if any | everyone without their own |

The worker always gets the login that belongs to the job's shipment, whatever it asks for.
A courier that needs a login and has none for that user stays unusable, as before.

## More automatic repairs

Before: three unreadable answers (PARSE_ERROR, SCHEMA_DRIFT) for different shipments within
24 hours open a repair. Now also:

- **AUTH_INVALID although the same login worked** since it was stored. (A login that never
  worked is a wrong password, not a broken adapter: no repair.)
- **NOT_FOUND for shipments found before.** (A number that was never found is the user's
  mistake: no repair.)

Counted per shipment: the same shipment failing three times counts once.

## Fully automatic deploy

`make auto-update` now runs `make deploy` after the checks:

1. `make up`: builds and starts the stack; the migrations, including the AI's new one, are
   applied by `engine-migrate`.
2. `make worker-tokens`: every worker of an AI-written adapter that needs one gets its
   token, written to `infrastructure/worker-tokens/<code>.env` (only you can read it;
   never printed, never in Git). It is done once per adapter.
3. Every courier's worker is started (`--profile couriers`), so the new one runs too.

The adapter's migration works where somebody added the courier at run time (its row, login
and recorded permission are kept; only the adapter is added and the status set to `live`)
and on a fresh database (the row is created).

## Step by step: your own courier, start to finish

```bash
# 0. Once: the stack, the AI service and the automatic update
make up DOCKER="sudo docker"
make up-ai DOCKER="sudo docker"
make check-ai DOCKER="sudo docker"                 # all OK
make auto-update DOCKER="sudo docker"              # in its own terminal; leave it running

# 1. A key for yourself (an organization user)
cd courier-engine
cargo run --quiet -- admin create-user --email you@yourcompany.example --organization "Real test"
KEY=ctk_...

# 2. Add the courier (see "Adding a courier"); for a website you need the owner's permission
curl -s -X POST http://127.0.0.1:8080/v1/providers -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d @shree.json
cargo run --quiet -- admin list-providers         # shreecourier building

# 3. Track a real number at once: it waits
curl -s -X POST http://127.0.0.1:8080/v1/tracking-requests -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"provider_code": "shreecourier", "tracking_number": "SC123456"}'
REQ=<the id>

# 4. Watch the AI work (it started by itself)
sudo docker compose --env-file .env -f infrastructure/docker-compose.yml -p courier logs -f ai-repair
make repairs                                       # new_provider shreecourier running / published

# 5. Published: the automatic update pulls, checks and deploys it within two minutes.
#    Its terminal shows "deploying: make deploy" and "shreecourier: worker token written".
cargo run --quiet -- admin list-providers         # shreecourier live

# 6. The waiting request starts by itself (within a minute) and returns the data
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/v1/tracking-requests/$REQ
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/v1/tracking-requests/$REQ/events

# 7. If the site asks for a one-time code: an admin answers it at http://localhost:8088
# 8. If it failed three times: read why, then try again
cargo run --quiet -- admin list-providers
cargo run --quiet -- admin retry-provider --code shreecourier
```

## Database changes (migration 0020)

- `providers`: `status`, `tracking_url`, `hosts`, `notes`, `added_by`, `added_by_organization`,
  `permission_recorded_by`, `permission_recorded_at`, `build_started_at`, `build_failure`,
  `build_sample`. Existing couriers are `live`.
- `provider_credentials.organization_id`: one active login per courier and organization,
  plus one for the platform.
- `shipments.access_scope` may be `org:<organization id>`.
- `audit_log.actor_type` may be `user` and `ai_repair`.

## Not done yet

- The whole chain has been run live once with Claude, against the demo courier website: a
  repair and a new adapter, both pushed, pulled and deployed by themselves
  ([system-test.md](system-test.md)). No real courier website has been built yet (that needs
  its owner's permission), and the login placeholders were not used live.
- No web page for adding couriers; it is the API and the command line.
- A courier added by an organization is visible (by name and code) to every organization;
  its login is not.
- `infrastructure/worker-tokens/` needs Docker Compose 2.24 or newer (`env_file` with
  `required: false`).
