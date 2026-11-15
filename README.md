# claude-memory

A self-hosted **memory server for AI agents** (Claude Code, Cursor, and other MCP clients). It stores facts and observations about your infrastructure, projects, and workflows so agents don't have to re-learn them on every session.

Built on [mem0](https://github.com/mem0ai/mem0) with **pgvector** storage, optional async processing via **Redis**, and an LLM-powered deduplication job. Exposes both an [MCP](https://modelcontextprotocol.io) interface and a REST API.

---

## Features

- **MCP tools**: `add_memory`, `search_memory`, `get_all_memories`, `update_memory`, `delete_memory`, `checkpoint`, `get_checkpoint`. Works with any MCP client (Claude Code, Qwen, Codex, Cursor, …).
- **Session working memory**: `checkpoint` stores an agent's current working state (goal, decisions, dead ends, next steps) instantly, without the queue or LLM, so it survives context compaction. It's re-injected by the harness hooks.
- **Harness hooks**: `/api/hooks/*` endpoints plus thin adapters for Claude Code, Qwen Code, Codex and Pi (see [`adapters/`](adapters/README.md)). They give each session a briefing at start, re-inject the checkpoint after compaction, and recall relevant memories per prompt.
- **Semantic memory** — stores, embeds (via Ollama), searches and deduplicates memories using mem0 + pgvector.
- **Categorization & tagging** — classify memories (`project`, `feedback`, `reference`, `user`, `session`, `incident`) and tag them with free-form labels for filtering and metrics.
- **REST API** — full CRUD over `/api/*` with bearer-token auth, plus JSON export/import.
- **Async queue (optional)** — set `REDIS_URL` to route writes through a Redis job queue (`remnant:queue:add_memory`) with a background worker, so heavy inference never blocks the request.
- **Prometheus metrics** — `/metrics` exposes memory counts by category, agent and tag, plus queue depth.
- **LLM deduplication** — a standalone `dedup_job.py` uses Ollama to merge and de-duplicate stored memories.
- **Multi-platform images** — `linux/amd64` and `linux/arm64`, published via Docker Buildx Bake.

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
   │  Ollama embeddings + LLM dedup (:11434)
   └───────────────────────────────────────────────┘
```

Session checkpoints live in a plain `session_state` table (one row per session, upserted). They're not stored in mem0, because they have to be readable the moment they're written.

**Data flow:** an `add_memory` call either writes synchronously through mem0 or, when Redis is configured, enqueues a job that the background worker (`queue_worker.py`) processes. mem0 persists embeddings in pgvector and embeds via Ollama; search ranks by embedding similarity.

---

## Configuration

All settings come from environment variables (`.env` file is auto-loaded). See `src/config.py`.

| Variable | Default | Description |
|---|---|---|
| `BEARER_TOKEN` | `change-me` | Shared secret for `/api/*` and `/mcp`. Send as `Authorization: Bearer <token>`. |
| `DATABASE_URL` | `postgresql://mem0:mem0@localhost:5432/mem0` | PostgreSQL connection string for the pgvector store. |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional: let mem0 use Anthropic for inference instead of Ollama. |
| `MEM0_USER_ID` | `default` | mem0 user id used as a filter namespace. |
| `MEM0_AGENT_ID` | `default` | Fallback agent id when no `X-Agent-ID` header is sent. |
| `MEM0_INFER` | `true` | Enable LLM extraction / auto-categorization on add. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for embeddings. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model used for text inference / dedup. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Model used for embeddings. |
| `APP_PORT` | `8080` | Port the server listens on. |
| `REDIS_URL` | *(empty)* | Set to enable the async write queue (requires a reachable Redis). |

The `X-Agent-ID` request header overrides the agent id for a given call.

---

## API reference

### MCP (`POST /mcp`)
Standard Model Context Protocol (stateless HTTP, SSE or JSON transport). Tools: `add_memory`, `search_memory` (returns `[score] date · category · #tags · id=… — text`), `get_all_memories` (paged, default 50), `update_memory`, `delete_memory`, `checkpoint`, `get_checkpoint`. Server `instructions` explain the checkpoint workflow to clients without hooks.

### REST (`/api/*` — requires bearer auth)
| Method | Path | Description |
|---|---|---|
| GET | `/api/memories` | List or search memories (`?q=&sort=newest|oldest|relevance&category=&agent_id=`) |
| POST | `/api/memories` | Add a memory (`{"content": "...", "agent_id": "..."}`) — queued if Redis is enabled |
| PUT | `/api/memories/{id}` | Edit memory content |
| PATCH | `/api/memories/{id}/tags` | Replace tags (`{"tags": [...]}`) |
| DELETE | `/api/memories/{id}` | Delete a memory |
| GET | `/api/memories/export` | Download all memories as JSON |
| POST | `/api/memories/import` | Bulk import a JSON array of memories |
| GET | `/api/stats` | Aggregate counts (total, first/last added, by agent/category/tag) |
| GET | `/api/tag-rules` | List auto-tagging rules (keyword → tag) |
| POST | `/api/tag-rules` | Add a rule (`{"tag": "...", "keyword": "..."}`) |
| DELETE | `/api/tag-rules/{tag}/{keyword}` | Remove a rule |
| GET | `/api/jobs` / `/api/jobs/{id}` | List dedup job runs / fetch one run (Redis queue mode) |
| GET | `/api/sessions` | List session checkpoints (`?project=&limit=`), newest first |
| GET / PUT | `/api/sessions/{id}` | Read / upsert a session checkpoint (`{"state": "...", "project": "..."}`) |
| GET | `/api/hooks/briefing` | Text to inject at session start / after compaction (`?session_id=&project=&source=startup\|resume\|compact&tool=`) |
| GET | `/api/hooks/recall` | Relevant memories for a prompt above the score threshold (`?q=&limit=&min_score=`) |
| POST | `/api/hooks/precompact` | Store auto-captured recent turns before compaction (`{session_id, project, notes}`) |

### Unauthenticated
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe (`{"status":"ok"}`) |
| GET | `/metrics` | Prometheus text exposition |

---

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
docker buildx bake -f docker-bake.hcl image-all --push \
  --set "*.tags=registry.example.internal/first-it-consulting/claude-memory:rolling"
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

## Project layout

```
├── Dockerfile                 # multi-stage: pip install → appuser, exposes :8080
├── docker-bake.hcl            # buildx bake targets (image / image-local / image-all)
├── requirements-mcp.txt       # Python dependencies (mcp, mem0, psycopg2, ollama, redis…)
├── tests.yaml                 # Container Structure Tests (critical imports resolve)
├── renovate.json5             # dependency updates; blocks mcp major (v2 dropped fastmcp)
└── src/
    ├── main.py                # ASGI app: MCP tools + REST API + /health + /metrics
    ├── config.py              # pydantic-settings configuration
    ├── memory.py              # mem0 client, pgvector storage, categories, tag rules
    ├── queue_worker.py        # Redis async enqueue + background worker (optional)
    └── scripts/
        └── dedup_job.py       # Ollama-powered memory de-duplication job
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

## License

Internal-use project (no license file included). Image registry:
`registry.example.internal/first-it-consulting/claude-memory`.
