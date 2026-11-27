# Phase 11: organizations and roles, test data, Claude in the agent

The system now runs on one server for many organizations. This phase adds:

1. **Organizations and two roles**: organization user and admin.
2. **Test data put straight into the database**, and how to test the system with it.
3. **Claude as the agent's model**, with a setup token (laptop) or an API key (server).
4. **SkyKing removed** from the system.

## 1. Organizations and roles

| | Organization user (`org_user`) | Admin (`admin`) |
|---|---|---|
| Belongs to | exactly one organization | the platform operator; no organization |
| Tracks shipments (`/v1/tracking-requests`) | yes, sees **only their own** requests | yes, their own (usually none) |
| Sees one-time codes and CAPTCHAs (`/v1/interventions`) | **no**: 403 `admins_only` | **all of them, every organization's**, oldest first |
| Answers them (human interface, API) | no | yes; the first answer wins, who answered is recorded |
| Uses the command line (`courier-engine admin ...`) | no (it needs the server) | the operator does |

- **Shared lookups.** When users of two organizations track the same number with the same
  courier, there is one shipment and one lookup; each user sees their own request only, and
  nobody sees who else follows it.
- **A disabled organization** (`organizations.status = 'disabled'`) shuts out all its users'
  API keys at once; other organizations and admins are unaffected.
- The database enforces the rule: an organization user without an organization, or an admin
  with one, cannot be stored.
- **Existing users** (from before this phase) were moved into an organization named
  "Default organization" by migration `0018`. Nobody is an admin until you name one.
- Phase 10's "every user is staff" and `admin set-staff` are gone. A challenge is no longer
  assigned to one user.

### Commands

```bash
cd courier-engine
cargo run --quiet -- admin create-organization --name "Acme Traders"
cargo run --quiet -- admin create-user --email asha@acme.example.com --organization "Acme Traders"   # prints a key once
cargo run --quiet -- admin create-user --email ops@yourcompany.example.com --admin                    # an admin
cargo run --quiet -- admin set-role --email you@example.com --role admin                              # make an existing user an admin
cargo run --quiet -- admin set-role --email x@acme.example.com --role org-user --organization "Acme Traders"
cargo run --quiet -- admin list-users
cargo run --quiet -- admin create-api-key --email asha@acme.example.com --name laptop                 # another key
```

With Docker, the same commands run inside the Engine container:
`sudo docker compose --env-file .env -f infrastructure/docker-compose.yml -p courier exec engine courier-engine admin list-users`

The human interface (<http://localhost:8088>) is for admins. An organization user who signs
in is told that only admins answer verification requests; an admin whose role is taken away
is signed out on their next click.

## 2. Testing with data put straight into the database

What you may insert by hand, and what must go through the Engine:

| Data | Straight into the database? |
|---|---|
| organizations, users (with `role`, `organization_id`) | yes |
| shipments, tracking requests | yes (a request belongs to one user, a shipment may have many) |
| providers, status mappings | yes, but the migrations already have them |
| **API keys** | **no**: the database holds only a hash. Use `admin create-api-key` |
| jobs, attempts, outbox messages, events | **no**: start a lookup with the API (`POST .../refresh`); the Engine writes these together in one transaction and publishes the job |
| credentials, challenge answers | **no**: they are encrypted with the master key; use `admin set-credential` / the API |

`infrastructure/test-data.sql` is a ready example: two organizations (Acme Traders, Globex
Logistics) with two users each, one admin, and three shipments, one of them followed by
both organizations. Fixed ids; loading it twice changes nothing. An Engine test
(`courier-engine/tests/test_data.rs`) loads the same file on every `make test`, so it
always matches the schema.

### Step by step

```bash
# 0. Services running, migrations applied (0018 and 0019 are new)
make up DOCKER="sudo docker"          # or: make up-deps DOCKER="sudo docker" && make migrate

# 1. Load the test data and get one API key per user (printed once: copy them)
make seed-test-data DOCKER="sudo docker"

# 2. Put the printed keys in shell variables
ASHA=ctk_...   RAVI=ctk_...   MEENA=ctk_...   ARUN=ctk_...   OPS=ctk_...

# 3. Each user sees only their own request
curl -s -H "Authorization: Bearer $ASHA"  http://127.0.0.1:8080/v1/tracking-requests
curl -s -H "Authorization: Bearer $MEENA" http://127.0.0.1:8080/v1/tracking-requests
curl -s -H "Authorization: Bearer $OPS"   http://127.0.0.1:8080/v1/tracking-requests        # admin: empty

# 4. Start the lookups (the seed has none yet). The shared shipment gets one job:
curl -s -X POST -H "Authorization: Bearer $RAVI"  http://127.0.0.1:8080/v1/tracking-requests/01930000-0000-7000-8000-00000000d002/refresh   # 202 job_created
curl -s -X POST -H "Authorization: Bearer $MEENA" http://127.0.0.1:8080/v1/tracking-requests/01930000-0000-7000-8000-00000000d003/refresh   # 200 job_already_active
curl -s -X POST -H "Authorization: Bearer $ASHA"  http://127.0.0.1:8080/v1/tracking-requests/01930000-0000-7000-8000-00000000d001/refresh

# 5. Another organization's request does not exist for you: 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $MEENA" \
     http://127.0.0.1:8080/v1/tracking-requests/01930000-0000-7000-8000-00000000d001/refresh

# 6. A one-time code: Globex's demo-site shipment asks for one (needs the demo site and its worker)
curl -s -X POST -H "Authorization: Bearer $ARUN" http://127.0.0.1:8080/v1/tracking-requests/01930000-0000-7000-8000-00000000d004/refresh
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ARUN" http://127.0.0.1:8080/v1/interventions   # 403
curl -s -H "Authorization: Bearer $OPS" http://127.0.0.1:8080/v1/interventions                                     # the admin sees it
#    Answer it as the admin at http://localhost:8088 (sign in with $OPS).

# 7. Switch an organization off: its keys stop working at once
psql "postgres://courier:<password>@localhost:55432/courier" \
     -c "UPDATE organizations SET status = 'disabled' WHERE name = 'Acme Traders'"
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ASHA" http://127.0.0.1:8080/v1/tracking-requests   # 401
psql "postgres://courier:<password>@localhost:55432/courier" \
     -c "UPDATE organizations SET status = 'active' WHERE name = 'Acme Traders'"
```

(The database password is `POSTGRES_PASSWORD` in your `.env`.) Your own data can be added
the same way: copy the file, change names, emails, numbers and ids.

## 3. Claude as the agent's model

One setting chooses who supplies the model: `AI_REPAIR_MODEL_SUPPLIER`.

| Value | Signs in with | Meant for |
|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | free models while developing |
| `claude-code` | `CLAUDE_CODE_OAUTH_TOKEN`, made by `claude setup-token` (Claude Pro, Max, Team or Enterprise) | **your laptop**, development |
| `anthropic` | `ANTHROPIC_API_KEY` from platform.claude.com | **the server** that many organizations use |

Both Claude suppliers use `AI_REPAIR_CLAUDE_MODEL` (default `claude-opus-5-5`) and
`AI_REPAIR_CLAUDE_EFFORT` (`low` … `max`, default `high`).

**Why two Claude ways.** A setup token signs in with your own Claude subscription. Anthropic
does not allow a subscription's sign-in to power a product offered to others unless
Anthropic has approved it. On your laptop, for your own development, the setup token is the
easy way. On the server, use an API key (`anthropic`).

**What Claude Code sees.** With `claude-code`, the service runs `claude --print` only as a
model: every tool is switched off (`--tools ""`), no MCP server is loaded, no setting,
memory or project file is read (a new empty configuration and working directory per
question), and it gets a minimal environment holding only the token. It never sees the
GitHub token, the repair token or anything else. Its answers go through the same guard and
sandbox as any model's.

### Step by step: setup token (laptop)

```bash
# 1. Claude Code on the laptop (skip if "claude --version" works)
curl -fsSL https://claude.ai/install.sh | bash

# 2. Make the token: a browser opens, you sign in to Claude; the token (sk-ant-oat01-...) is
#    printed once and is valid for one year. It only lets programs ask Claude.
claude setup-token

# 3. Put it in .env (an editor, not the command line, so it lands in no shell history):
#      AI_REPAIR_MODEL_SUPPLIER=claude-code
#      CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
#      AI_REPAIR_CLAUDE_MODEL=claude-opus-5-5      (optional)

# 4. Rebuild the repair service (the image now contains Claude Code) and start it
make up-ai DOCKER="sudo docker"

# 5. Check: all checks pass and "OK model: answers (claude-code: claude-opus-5-5)".
#    This asks Claude one short question (uses a little of your subscription).
make check-ai DOCKER="sudo docker"

# 6. A real task: repair the demo-site adapter, then watch it
cd courier-engine && cargo run --quiet -- admin request-repair --adapter demosite && cd ..
sudo docker compose --env-file .env -f infrastructure/docker-compose.yml -p courier logs -f ai-repair
make repairs
```

Without Docker: `make run-ai-repair` uses the `claude` program on your PATH.

### Step by step: API key (server)

```
AI_REPAIR_MODEL_SUPPLIER=anthropic
ANTHROPIC_API_KEY=sk-ant-api03-...
```

then `make up-ai` and `make check-ai` as above. The request is made with Anthropic's official
Python SDK; thinking is adaptive, the answer is streamed, a refusal or a cut-off answer is
never taken as an answer, and rate limits are waited out and retried.

To go back to OpenRouter: `AI_REPAIR_MODEL_SUPPLIER=openrouter` (or remove the line).

## 4. SkyKing removed

The SkyKing adapter is gone: its code and tests, its queues in `topology.yaml`, its Compose
worker, its rows in the database (migration `0019_remove_skyking.sql`) and the docs. A
database that already holds SkyKing shipments keeps the rows switched off, so their history
is not lost. The AI service now shows the Delhivery adapter as its example of an API
adapter. Queues already created in RabbitMQ stay there unused until deleted:

```bash
sudo docker compose --env-file .env -f infrastructure/docker-compose.yml -p courier exec rabbitmq \
     sh -c 'rabbitmqctl delete_queue --vhost "$RABBITMQ_VHOST" adapter.skyking.jobs; rabbitmqctl delete_queue --vhost "$RABBITMQ_VHOST" adapter.skyking.jobs.dlq'
```

## Not done yet

- An organization admin role (someone at a customer who manages that organization's users),
  and organization-wide views ("all of Acme's requests"). Requests stay per user.
- A web page for managing organizations and users; it is command-line only.
- OpenAI as a supplier.
- A real repair with Claude has not been run here: the tests use a stand-in for the
  `claude` program and for the SDK, so no subscription or key was spent.
