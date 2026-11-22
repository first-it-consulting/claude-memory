# claude-memory

[![test](https://github.com/first-it-consulting/claude-memory/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/first-it-consulting/claude-memory/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/first-it-consulting/claude-memory)](https://github.com/first-it-consulting/claude-memory/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![images](https://img.shields.io/badge/ghcr-linux%2Famd64%20%C2%B7%20arm64-brightgreen)](https://ghcr.io/first-it-consulting/claude-memory)

**One memory server for all your agents — and everyone's.**

**The problem, in 30 seconds.** Every AI coding session starts from zero. Your agents (Claude Code, Codex, Qwen Code, Cursor, pi) re-learn the same cluster names, the same CI quirks, the same "never do this again" decisions on every machine, every day — and nothing one agent learns reaches the others.

**The solution.** A self-hosted memory server your whole team's agents write to and read from: agents store facts via [MCP](https://modelcontextprotocol.io) or REST, memories are embedded (Ollama) into **Postgres + pgvector**, harness hooks give every session a briefing at start and recall relevant memories per prompt, and a nightly job deduplicates and promotes what finished sessions learned. Multi-user by design: per-user, revocable API tokens; browser login via OIDC (Keycloak); an admin-published shared space so one person's hard-won fact becomes every tenant's baseline.

Built on [mem0](https://github.com/mem0ai/mem0), Postgres/pgvector, FastAPI. One container + Postgres; `docker compose up -d`.

### Shared team server vs local-first tools

| | **claude-memory** (this project) | local-first single-user tools |
|---|---|---|
| Where memories live | one Postgres you operate | on the machine (e.g. claude-mem: local SQLite) |
| Teammates / multiple machines | one shared store, per-user workspaces | the store is per-machine; nothing is shared |
| Agent-to-agent sharing | shared space + automatic promotion of session decisions | out of scope (single local user) |
| Auth | OIDC (Keycloak) for browsers, per-user **revocable** API tokens for agents | not a multi-user problem — no server to authenticate to |
| How agents connect | one server: MCP + REST + hooks adapters (Claude Code, Codex, Qwen Code, pi) | integrations installed per agent, per machine (claude-mem supports many agents this way) |
| Ops | multi-arch images, GitOps-ready (Flux), Prometheus metrics | n/a (runs on the laptop) |

Local-first tools (e.g. [claude-mem](https://github.com/thedotmack/claude-mem)) are a good fit for one person on one machine — different trade-off, deliberately. claude-memory is the **server** shape: your agents and your team grow into one knowledge base instead of N disconnected ones. If you are one person on one laptop, either works; the moment you have a second machine, a teammate, or a CI runner that should know what the cluster's quirks are, the server shape is the one that scales.

---

## Features

- **MCP tools**: `add_memory`, `search_memory` (also `mode="index"` for token-cheap rows + `fetch_memories` for full text), `memory_timeline`, `get_all_memories`, `update_memory`, `delete_memory`, `checkpoint`, `get_checkpoint`. Works with any MCP client (Claude Code, Qwen, Codex, Cursor, …).
- **Session working memory**: `checkpoint` stores an agent's current working state (goal, decisions, dead ends, next steps) instantly, without the queue or LLM, so it survives context compaction. It's re-injected by the harness hooks.
- **Harness hooks**: `/api/hooks/*` endpoints plus thin adapters for Claude Code, Qwen Code, Codex and Pi (see [`adapters/`](adapters/README.md)). They give each session a briefing at start, re-inject the checkpoint after compaction, and recall relevant memories per prompt.
  Set `BRIEFING_TOKEN_BUDGET` (e.g. `600`) to cap that start briefing to an estimated token budget (chars/4): it becomes a compact index - session instructions, checkpoint (truncated if oversized) and one short `id=`-bearing line per relevant memory, full text via `search_memory`. Default `0` keeps the classic full briefing.
- **Semantic memory** — stores, embeds, searches and deduplicates memories using mem0 + pgvector.
- **Pluggable model providers** — embeddings and the extraction LLM run on Ollama (default) or **any OpenAI-compatible endpoint** (LiteLLM, vLLM, TEI, Ollama's own `/v1`) via `EMBED_PROVIDER`/`LLM_PROVIDER`; a startup dimension guard refuses writes on a 768-dim mismatch. See [Quickstart](#use-litellm-or-another-openai-compatible-endpoint-instead-of-ollama).
- **Setup & doctor script** — `scripts/setup.sh` configures every installed harness in one go (hook env 0600, hook/MCP config entries, token never printed, idempotent, `--dry-run`); `scripts/setup.sh doctor` verifies server reachability, token and hook/MCP config.
- **Categorization & tagging** — classify memories (`project`, `feedback`, `reference`, `user`, `session`, `incident`) and tag them with free-form labels for filtering and metrics.
- **REST API** — full CRUD over `/api/*` with bearer-token auth, plus JSON export/import.
- **Multi-tenancy** — every user gets their own workspace (memories + session
  checkpoints). Agents authenticate with per-user API tokens, the browser with
  a Keycloak login; admins see across all workspaces and can publish facts into
  a shared space that every tenant reads. See [Multi-tenancy](#multi-tenancy).
- **Async queue (optional)** — set `REDIS_URL` to route writes through a Redis job queue (`remnant:queue:add_memory`) with a background worker, so heavy inference never blocks the request.
- **Prometheus metrics** — `/metrics` exposes memory counts by category, agent and tag, plus queue depth.
- **Nightly maintenance** — `dedup_job.py` (CronJob) removes exact and near-duplicate memories (verbatim where possible; LLM merges only if every number, version and path survives; originals archived to `mem0_archive`).
- **Checkpoint promotion** — the same job copies the `## Decisions` and `## Dead ends` bullets of finished sessions into long-term memory, verbatim, tagged `auto-promoted`, so what one session learned reaches every agent. See [Checkpoint promotion](#checkpoint-promotion).
- **Multi-platform images** — `linux/amd64` and `linux/arm64`, published via Docker Buildx Bake.

---

## Screenshots

From a demo workspace (invented data). The browser UI is per-tenant: agents
and users each see their own workspace, admins can switch workspaces.

![Memories tab: invented project decisions and dead ends with category and tag chips, per-tenant workspace selector](docs/images/memories.png)

*Memories - every entry carries category, tags and date; search, add, export.*

![Stats tab: totals, memories by category and agent, top tags](docs/images/stats.png)

*Stats - totals, by category, by agent, top tags.*

![Sessions tab: two agent sessions with checkpoints, project and harness columns, goal line each](docs/images/sessions.png)

*Sessions - working-memory checkpoints per agent session (survive context compaction).*

---

## Architecture

```
Claude Code / MCP client            Browser UI (claude-memory-ui)
         │                                   │
         │  MCP protocol (/mcp, SSE/JSON)    │  Bearer-authenticated HTTP
         ▼                                   ▼
   ┌──────────────┐                   ┌──────────────────────────┐
   │  claude-     │  <─ proxy ─────── │  claude-memory-ui        │
   │  memory      │                   │  (FastAPI static + proxy)│
   │  :8080       │                   └────────────┬─────────────┘
   └──────┬───────┘                                  │  /api/* (proxied)
          │                                          ▼
          ▼                                   ┌──────────────┐
   ┌──────────────┐                           │  claude-     │
   │ mem0 /       │◀───────────┬──────────────│  memory      │
   │ pgvector     │            │              │  :8080 (API) │
   └──────┬───────┴────────────┴──────────────┴──────────────┘
          │ PostgreSQL (:5432)
          ▼
   ┌──────────────┐        (optional, when REDIS_URL set)
   │ Redis        │◀──── enqueue / worker ──────┐
   └──────────────┘                            │
          ▼                                    ▼
   ┌─────────────────────────────────  queue_worker.py (background)
   │  embeddings + LLM: Ollama (:11434) or any OpenAI-compatible endpoint
   └───────────────────────────────────────────────┘
```

Session checkpoints live in a plain `session_state` table (one row per session, upserted). They're not stored in mem0, because they have to be readable the moment they're written.

**Data flow:** an `add_memory` call either writes synchronously through mem0 or, when Redis is configured, enqueues a job that the background worker (`queue_worker.py`) processes. mem0 persists embeddings in pgvector and embeds via Ollama or the configured OpenAI-compatible endpoint; search ranks by embedding similarity.

---

## Configuration

All settings come from environment variables (`.env` file is auto-loaded). See `src/config.py`.

| Variable | Default | Description |
|---|---|---|
| `BEARER_TOKEN` | *(empty)* | **Legacy** shared secret for `/api/*` and `/mcp` (tenant `default`). Empty (the default) disables this auth path. Superseded by per-user API tokens; it is registered as the revocable token `legacy-hooks` so it can be switched off. Send as `Authorization: Bearer <token>`. |
| `DATABASE_URL` | `postgresql://mem0:mem0@localhost:5432/mem0` | PostgreSQL connection string for the pgvector store. |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional: let mem0 use Anthropic for inference instead of Ollama. |
| `MEM0_USER_ID` | `default` | mem0 user id used as a filter namespace. |
| `MEM0_AGENT_ID` | `default` | Fallback agent id when no `X-Agent-ID` header is sent. |
| `MEM0_INFER` | `true` | LLM extraction on add for callers that don't choose (REST). The MCP `add_memory` tool stores notes **as written** unless called with `infer=true`: with small local models extraction dropped, distorted and mixed up notes. |
| `MEM0_CUSTOM_INSTRUCTIONS` | *(technical-notes prompt)* | Appended to mem0's extraction prompt when inferring; `""` disables. |
| `ADD_RAW_FALLBACK` | `true` | If extraction yields no facts, store the note as written instead of dropping it. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for embeddings. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model used for text inference / dedup. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Model used for embeddings. |
| `EMBED_PROVIDER` | `ollama` | `openai` switches embeddings to any OpenAI-compatible `/v1/embeddings` endpoint (LiteLLM, vLLM, TEI, Ollama's own `/v1`, ...). |
| `EMBED_BASE_URL` / `EMBED_MODEL` / `EMBED_API_KEY`(`_FILE`) | *(empty)* | OpenAI-compatible embedder target; `*_API_KEY_FILE` (mounted secret) wins over the inline key. Keys are never logged. |
| `LLM_PROVIDER` | `ollama` | `openai` switches the extraction/merge LLM to any OpenAI-compatible `/v1/chat/completions`. |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`(`_FILE`) | *(empty)* | OpenAI-compatible LLM target (same key rules). |
| dimension guard | — | The pgvector column is fixed 768 dims. With `EMBED_PROVIDER=openai` the server probe-embeds at startup; a mismatched model **refuses writes** (503 + `/health` "degraded") instead of corrupting the store. Re-embed path: export, point at the new model, re-add (no auto re-embed yet). |
| `APP_PORT` | `8080` | Port the server listens on. |
| `APP_BIND` | `127.0.0.1` | Host interface `APP_PORT` is published on. Loopback by default (localhost only); set `0.0.0.0` or an interface IP to expose it. |
| `REDIS_URL` | *(empty)* | Set to enable the async write queue (requires a reachable Redis). |
| `ADMIN_TOKEN` | *(empty)* | Bootstrap admin API token (role admin, tenant `default`). Keep it set — it is the recovery path if the users table or Keycloak is unreachable. |
| `SHARED_TENANT_ID` | `global` | Pseudo-tenant whose memories every workspace can read (admin-published). |
| `OIDC_ISSUER` | *(empty)* | Keycloak issuer URL; enables browser (JWT) auth when set. |
| `OIDC_CLIENT_ID` | `claude-memory-ui` | Expected token audience. |
| `OIDC_AUDIENCE` | *(empty)* | Override the expected audience if it differs from the client id. |
| `OIDC_ADMIN_ROLES` | `claude-memory-admin` | Comma-separated realm/client roles that grant the admin role. |
| `OIDC_ADMIN_USERNAMES` | *(empty)* | Comma-separated `preferred_username` fallback for admins. |
| `OIDC_AUTO_CREATE` | `true` | First Keycloak login auto-creates a `role=user` account. |

The `X-Agent-ID` request header overrides the agent id for a given call.

---

## Multi-tenancy

A **tenant** is a user's workspace. The tenant id doubles as mem0's `user_id`
namespace, so isolation is mem0's own filter — no data was re-embedded when
this was introduced: everything that existed before lives in tenant `default`.

**Authentication** (`src/auth.py`, order of resolution):

| Token | Who | Tenant / role |
|---|---|---|
| `ADMIN_TOKEN` | operators, recovery | `default` / **admin** |
| `cm_…` API token | agents (MCP, REST, hooks) | its user / its role |
| Keycloak JWT | browser (UI login) | mapped by `sub`, role from realm roles |
| `BEARER_TOKEN` (legacy) | shared token of pre-token setups; shown as `legacy-hooks` (user `default`), revocable | `default` / user |

**Per-user API tokens** (`cm_<id>_<secret>`, `src/tokens.py`) are self-service:
every signed-in user mints, lists and revokes their own tokens under
**My tokens** in the UI (or `/api/me/tokens`). A token has a name, an expiry
(default 90 days, maximum 365), an optional default harness and, for admins
only, an opt-in `can_admin` flag; the secret is shown **once** and only its
SHA-256 hash is stored. Admins see every token masked (prefix + last four) and
can revoke any of them (`/api/admin/tokens`), but never mint one for someone
else. Use one token per device or harness so a lost device is a single
revoke. Creating/revoking is audited (`token_audit`); failed attempts are rate
limited. `POST /api/users` / `rotate-token` still exist for the older
per-user token on the `users` row (imported as `imported-legacy`).
Migrating a device from the shared token: see
[docs/token-migration.md](docs/token-migration.md); moving a tenant's data
(a personal token reads **its** tenant, not `default`): see
[docs/retenant-design.md](docs/retenant-design.md).

Keycloak access tokens are verified
against the realm's JWKS (signature, `iss`, `aud`, `exp`); the first login
provisions `users` by `preferred_username`, and realm role
`claude-memory-admin` (or `OIDC_ADMIN_USERNAMES`) grants the admin role on
every login.

**Isolation rules:**

- Reads (list/search/hooks/MCP) see the caller's workspace **plus the shared
  space** (`SHARED_TENANT_ID`, default `global`).
- Writes land in the caller's workspace. Admins publish into the shared space
  with `POST /api/memories {"content": "...", "shared": true}` — or share an
  **existing** memory: `PATCH /api/memories/<id>/share` moves it into the
  shared space keeping its id, recording `shared_by` / `shared_at` /
  `original_tenant` on the row (idempotent; the original owner keeps seeing
  it because the shared space is in every view). The MCP `add_memory` tool
  accepts `shared=true` for admin tokens only — anything else is rejected
  without storing.
- Foreign-tenant memory ids and session ids behave exactly like missing ones
  (404) — no existence leak. Admin-only endpoints answer 403.
- Proposal workflow (bean oi38): an owner marks their own memory with
  `POST /api/memories/<id>/propose-share` (max 20 open per user) — it stays
  in their workspace, invisible to others, until an admin reviews it on
  `GET /api/share-proposals` (admin) and either approves
  (`POST /api/share-proposals/<id>/approve` — performs the shared-space move
  with approve audit; refuses 409 if the owner edited the content after
  proposing) or rejects with an optional reason (the owner sees a `rejected`
  marker and can re-propose). Owners can withdraw an open proposal via
  `POST /api/memories/<id>/withdraw-share`.
- Admins may add `?tenant=<id>` or `?scope=all` to memories/stats/export/
  sessions; `/api/jobs` and tag-rule writes are admin-only (job reports quote
  other tenants' session lines).
- The nightly job dedups **within** a tenant only, and checkpoint promotion
  lands in the session's owning workspace.

### Checkpoint promotion

The nightly `dedup_job.py` first promotes finished sessions: every session whose checkpoint has
not changed for `PROMOTE_IDLE_HOURS` and was not promoted yet (or changed since). From its
checkpoint it takes the bullets under `Decisions` / `Dead ends` headings (`## Decisions`,
`**Dead ends:**`, …) and stores each one **verbatim** — no LLM — as
`Decision (<project>): <bullet>` / `Dead end (<project>): <bullet>`, category `project`,
tags `auto-promoted`, `decision` | `dead-end`, project and harness, with
`source: checkpoint` and the `session_id` in its metadata.

It skips bullets that are too short (< 40 characters or 6 words), only matter inside their
session ("for now", "temporarily", "none"), repeat a line from another session, or are already
known (similarity ≥ `PROMOTE_DUP_THRESHOLD` to an existing memory). The hooks rank
auto-promoted memories `PROMOTED_RECALL_PENALTY` below deliberately stored ones. Each run
writes a line-by-line report to the job run (`details`, shown in the UI's Jobs tab).

| Variable (CronJob) | Default | Description |
|---|---|---|
| `PROMOTE_MODE` | `dry-run` | `off`, `dry-run` (report only: what *would* be promoted) or `on`. |
| `PROMOTE_IDLE_HOURS` | `12` | A session counts as finished after this many hours without a checkpoint update. |
| `PROMOTE_DUP_THRESHOLD` | `0.92` | Skip a bullet if an existing memory is at least this similar. |
| `PROMOTED_RECALL_PENALTY` (server) | `0.05` | Subtracted from an auto-promoted memory's similarity in hook recall. |

---

## API reference

### MCP (`POST /mcp`)
Standard Model Context Protocol (stateless HTTP, SSE or JSON transport). Tools: `add_memory`, `search_memory` (returns `[score] date · category · #tags · id=… — text`; `mode="index"` returns 140-char snippets + an estimated full-text token count per hit — then `fetch_memories(ids=…)` pulls only what you actually read), `memory_timeline` (what was stored just before/after a memory), `search_memory hybrid=true` (exact-identifier keyword path - Postgres FTS + pg_trgm - fused with the semantic ranking via reciprocal-rank fusion; use it when an error code or identifier must match exactly), `get_all_memories` (paged, default 50), `update_memory`, `delete_memory`, `checkpoint`, `get_checkpoint`. Server `instructions` explain the checkpoint workflow to clients without hooks.

### REST (`/api/*` — requires bearer auth; every route is tenant-scoped)
Admins may add `?tenant=<id>` or `?scope=all` where marked *(admin scope)*.
All users may add `?space=own|shared` to memories/stats/export *(bean o9y4)*
to narrow any workspace view: `own` excludes shared-space rows, `shared`
keeps only them; omitted (or `all`) is the default union "own + shared".
The two selectors compose — `?tenant=alice&space=own` is "alice's own,
without shared", and an invalid `space` value answers 400.

| Method | Path | Description |
|---|---|---|
| GET | `/api/auth/whoami` | Identity of the presented token (`user_id`, `role`, `via`) |
| GET / POST | `/api/me/tokens` | List own API tokens (masked) / mint one (`{"name", "expires_days", "default_harness", "can_admin"}`) — plaintext returned **once** |
| DELETE | `/api/me/tokens/{id}` | Revoke own token |
| GET | `/api/admin/tokens` *(admin)* | All tokens, masked (`?user=`) |
| DELETE | `/api/admin/tokens/{id}` *(admin)* | Revoke any token (revoking `legacy-hooks` kills the shared token for every device) |
| GET | `/api/users` *(admin)* | List tenants (no token hashes) |
| POST | `/api/users` *(admin)* | Create a tenant (`{"id", "name", "role"}`) — returns its API token **once** |
| GET/PATCH/DELETE | `/api/users/{id}` *(admin)* | Read / change (name, role, disabled) / remove a tenant |
| POST | `/api/users/{id}/rotate-token` *(admin)* | New API token (shown once, old one dies) |
| GET | `/api/memories` | List or search memories (`?q=&sort=newest|oldest|relevance&category=&agent_id=&tag=`); `?format=index` → compact rows (`id`, `snippet`, `score`, `est_tokens_full`); `?ids=a,b` → visible records by id *(admin scope)*; optional `?limit=&offset=` → `{"memories": [...], "total": N}` paging (`total` = full filtered set; without these params the response stays the plain array) |
| GET | `/api/memories/timeline/{id}` | Chronological neighbours (`?window=3`, own workspace + shared space) |
| (any search) | `?hybrid=1` | additionally run the exact keyword path (FTS + `pg_trgm` ILIKE) and fuse rankings (RRF) |
| POST | `/api/memories` | Add a memory (`{"content": "...", "agent_id": "..."}`, admin: `"shared": true` → shared space) — queued if Redis is enabled |
| PUT | `/api/memories/{id}` | Edit memory content (own tenant only) |
| PATCH | `/api/memories/{id}/tags` | Replace tags (`{"tags": [...]}`, own tenant only) |
| DELETE | `/api/memories/{id}` | Delete a memory (own tenant only) |
| GET | `/api/memories/export` | Download memories as JSON *(admin scope)* |
| POST | `/api/memories/import` | Bulk import a JSON array into the caller's workspace |
| GET | `/api/stats` | Aggregate counts (total, first/last added, by agent/category/tag) *(admin scope)* |
| GET | `/api/tag-rules` | List auto-tagging rules (keyword → tag) |
| POST | `/api/tag-rules` *(admin)* | Add a rule (`{"tag": "...", "keyword": "..."}`) |
| DELETE | `/api/tag-rules/{tag}/{keyword}` *(admin)* | Remove a rule |
| GET | `/api/jobs` / `/api/jobs/{id}` *(admin)* | List nightly job runs (one row per phase, with a `details` report) / fetch one run |
| GET | `/api/sessions` | List session checkpoints (`?project=&limit=`), newest first *(admin scope)* |
| GET / PUT | `/api/sessions/{id}` | Read / upsert a session checkpoint (`{"state": "...", "project": "..."}`) *(admin scope)* |
| GET | `/api/hooks/briefing` | Text to inject at session start / after compaction (`?session_id=&project=&source=startup\|resume\|compact&tool=`) |
| GET | `/api/hooks/recall` | Relevant memories for a prompt above the score threshold (`?q=&limit=&min_score=`) |
| POST | `/api/hooks/precompact` | Store auto-captured recent turns before compaction (`{session_id, project, notes}`) |

### Unauthenticated
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe (`{"status":"ok"}`) |
| GET | `/metrics` | Prometheus text exposition |

---

## Quickstart (Docker Compose, ~5 minutes)

Brings up postgres + server on `http://localhost:8080` using `compose.yaml`.
`docker compose up -d` pulls the published multi-arch image
(`ghcr.io/first-it-consulting/claude-memory:rolling`); use `--build` to run the
working tree instead. Verified 2026-11-21 with Docker Compose v2 on macOS
against the published image; the same steps run on Linux.

**You need a model endpoint for real use.** Every write and search needs an
embedding model, and storing memories with `infer=true` (the REST default)
needs a chat model. Default is Ollama:

```bash
ollama pull nomic-embed-text        # embeddings (always required)
ollama pull qwen2.5:7b              # or set OLLAMA_MODEL to any chat model
```

Two things to know: LLM extraction needs a chat-model context window of about
8k tokens — on a stock Ollama raise it (e.g. run the server with
`OLLAMA_CONTEXT_LENGTH=16384`) or adds fail with "exceeds the available
context size". And REST adds are processed **asynchronously** (queued, then
embedded + extracted): on a CPU-only Ollama expect up to a minute before a new
memory is searchable; the MCP `add_memory` tool stores notes as written
(`infer=false` default) and is ready immediately.

### Use LiteLLM (or another OpenAI-compatible endpoint) instead of Ollama

Each role (embeddings, extraction LLM) can switch to any OpenAI-compatible
`/v1` endpoint - a LiteLLM gateway, vLLM, TEI, or Ollama's own `/v1` - with
these variables (full table: [Configuration](#configuration)):

```bash
EMBED_PROVIDER=openai
EMBED_BASE_URL=https://litellm.example.com/v1   # any /v1-compatible gateway
EMBED_MODEL=nomic-embed-text                    # must produce 768-dim vectors
EMBED_API_KEY=CHANGE_ME                         # or EMBED_API_KEY_FILE (mounted secret, wins)
LLM_PROVIDER=openai
LLM_BASE_URL=https://litellm.example.com/v1
LLM_MODEL=qwen2.5:7b                            # any chat model on the endpoint
LLM_API_KEY=CHANGE_ME                           # or LLM_API_KEY_FILE
```

The pgvector column is fixed at 768 dimensions: with `EMBED_PROVIDER=openai`
the server probe-embeds at startup and **refuses writes** (503, `/health`
"degraded") if the model returns a different width. After switching, restart
the stack and run `scripts/setup.sh doctor` to confirm the server still
answers for agents (doctor checks server/token/MCP, not the provider).

1. Configure:

   ```bash
   cp .env.example .env
   # Set ADMIN_TOKEN in .env to a long random value - it is the bootstrap
   # admin token. Generate one with:
   openssl rand -hex 32
   ```

2. Start:

   ```bash
   docker compose up -d              # pulls the published image
   curl -s localhost:8080/health      # {"status": "ok", "service": "claude-memory"}
   ```

3. Mint a personal token (the admin token is bootstrap only — use personal
   tokens for clients). This creates tenant `alice` and prints her token once:

   ```bash
   curl -s -X POST localhost:8080/api/users \
     -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"id":"alice","name":"Alice","role":"user"}' | jq .
   ```

   Or a token for the `default` tenant with your admin token:
   `POST /api/me/tokens` (same headers, body `{"name":"laptop","expires_days":90}`).

4. Connect an MCP client (e.g. Claude Code):

   ```bash
   claude mcp add --transport http claude-memory http://localhost:8080/mcp \
     --header "Authorization: Bearer <your-personal-token>"
   ```

   Then `add_memory` / `search_memories` show up as tools. The MCP default is
   `infer=false` — memories are stored as written.

5. Or do steps 4+5 for every installed harness in one go:

   ```bash
   ./scripts/setup.sh --url http://localhost:8080 --token <your-personal-token>
   ./scripts/setup.sh doctor    # connectivity, token, hook/MCP config checks
   ```

   `setup` detects Claude Code, Qwen Code, Codex, pi and opencode, writes the
   hook env (0600), the hook/MCP config entries (backing up files it touches,
   once) and never prints the token; it is idempotent and supports `--dry-run`.
   Behind a login proxy (oauth2-proxy in front of the web UI), point `--url`
   at the public address: agents and these checks work as long as the proxy
   lets `/mcp`, `/api/hooks` and `/api/sessions` through with bearer tokens
   (the UI-only paths may stay login-gated).
   With an admin token it can create the user and store the issued token
   directly: `--admin-token <admin> --create-user alice`. Manual per-harness
   steps (and what the script writes) stay documented in
   [adapters/README.md](adapters/README.md).

6. Optional — web UI: run the `claude-memory-ui` image (see that repo's
   README). Without Keycloak it authenticates with the legacy shared token:
   set `BEARER_TOKEN` in `.env` to a strong value, restart the stack, and give
   the UI container the same value plus `MCP_INTERNAL_URL=http://server:8080`.

To tear down: `docker compose down` (add `-v` to also drop the database).

## Deploy on Kubernetes

Sanitized example manifests — server, UI, Ingress (incl. the oauth2-proxy
bearer pass-through paths), nightly dedup CronJob, pgvector/`pg_trgm`
requirements and a Flux `Kustomization` — live in
[docs/kubernetes.md](docs/kubernetes.md). They are the production shape this
project runs in itself, with hostnames and secrets genericized.

## Build & run

### Requirements
- Docker (with `buildx`) for image builds.
- A PostgreSQL instance running mem0's schema (created automatically at startup, non-fatal if the DB isn't ready yet).
- Optional: Redis (for async mode) and Ollama (for embeddings/inference without an API key).

### Build locally with Docker Bake
```bash
# Single-platform image to the local daemon
docker buildx bake -f docker-bake.hcl image-local

# Multi-platform (amd64 + arm64) build to a registry
# REGISTRY defaults to ghcr.io/first-it-consulting; override to publish elsewhere
REGISTRY=ghcr.io/<you> docker buildx bake -f docker-bake.hcl image-all --push
```

### Run locally (Docker)
```bash
docker buildx bake -f docker-bake.hcl image-local
docker run --rm -p 8080:8080 \
  -e BEARER_TOKEN=secret \
  -e DATABASE_URL="postgresql://mem0:mem0@localhost:5432/mem0" \
  claude-memory:rolling
```

### Run locally (source, for development)
```bash
pip install -r requirements-mcp.txt
export BEARER_TOKEN=secret DATABASE_URL="postgresql://mem0:mem0@localhost:5432/mem0"
python src/main.py        # serves on 0.0.0.0:8080
```

---

## Testing

The suite starts the real ASGI app against a real PostgreSQL, with mem0 replaced
by an in-process fake, so it needs neither pgvector nor Ollama. It covers the MCP
tools over `/mcp`, the REST and hook endpoints, and the adapter script, which is
run as a subprocess the same way harnesses call it.

```bash
docker run --rm -d --name mem0-test -p 5432:5432 \
  -e POSTGRES_USER=mem0 -e POSTGRES_PASSWORD=mem0 -e POSTGRES_DB=mem0_test postgres:16
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://mem0:mem0@localhost:5432/mem0_test pytest
```

Without `TEST_DATABASE_URL`, only the DB-independent tests run. With `REQUIRE_DB=1`
(set in CI), a missing database is an error instead of a skip. CI
(`.github/workflows/test.yml`) runs on Python 3.13 and 3.15 and also typechecks
the Pi extension.

---

## Project layout

```
├── Dockerfile                 # multi-stage: pip install → appuser, exposes :8080
├── docker-bake.hcl            # buildx bake targets (image / image-local / image-all)
├── requirements-mcp.txt       # Python dependencies (mcp, mem0, psycopg2, ollama, redis…)
├── tests.yaml                 # Container Structure Tests (critical imports resolve)
├── tests/                     # pytest suite (see Testing)
├── requirements-dev.txt       # test dependencies
├── renovate.json5             # dependency updates; blocks mcp major (v2 dropped fastmcp)
└── src/
    ├── main.py                # ASGI app: MCP tools + REST API + /health + /metrics
    ├── config.py              # pydantic-settings configuration
    ├── memory.py              # mem0 client, pgvector storage, categories, tag rules
    ├── queue_worker.py        # Redis async enqueue + background worker (optional)
    └── scripts/
        ├── dedup_job.py       # nightly job: checkpoint promotion + de-duplication
        └── promotion.py       # parses Decisions / Dead ends out of checkpoints
adapters/                      # harness hook adapters (see adapters/README.md)
├── hooks/memory_hook.py       # shared hook script: Claude Code, Qwen Code, Codex
├── claude-code/ qwen/ codex/  # config snippets per harness
└── pi/agent-memory.ts         # Pi extension
```

---

## Dependencies & compatibility

- **`mcp` is pinned to `1.x`.** The v2.0.0 release dropped the bundled `mcp.server.fastmcp` submodule that `src/main.py` imports directly (`from mcp.server.fastmcp import FastMCP`). Renovate blocks `mcp` major updates until the import is migrated to the standalone [`fastmcp`](https://pypi.org/project/fastmcp/) package.
- Compatible with **Claude Code 2.x** (MCP spec `2024-11-05`); the server strips `outputSchema` from tool definitions so tools aren't silently dropped.

---

## Related projects

- **[claude-memory-ui](https://github.com/first-it-consulting/claude-memory-ui)** — React + Vite single-page app with a FastAPI proxy that provides a web UI for managing memories, stats, jobs and tag rules.
- **[containers-images](https://github.com/first-it-consulting/containers-images)** — the monorepo claude-memory was originally part of (now extracted).

---

## Contributing and security

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Please report security issues privately as described in [SECURITY.md](SECURITY.md).

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
