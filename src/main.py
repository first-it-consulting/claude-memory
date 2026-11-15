import asyncio
import json
import re
import logging
import time
import urllib.parse

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import memory as mem_store
from config import settings
from queue_worker import QueueFull, enqueue, queue_depth, run_write

# Blocking reads (pgvector + Ollama embeddings for search) run in the default
# thread pool; writes are serialised on the writer thread via run_write/enqueue.
# Nothing blocking may run on the event loop, or /health and every other
# client stall behind a single slow Ollama call.
_to_thread = asyncio.to_thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Long-term memory shared across sessions and agents. search_memory before "
    "re-deriving facts about the user's infrastructure or projects; add_memory for "
    "durable facts; update_memory to correct outdated ones. For long tasks keep your "
    "working state with checkpoint(session_id, state) after each milestone — it is "
    "re-injected after context compaction. If no briefing gave you a session_id, use "
    "get_checkpoint(project=...) to resume and '<project>-<YYYYMMDD>' as session_id."
)

mcp = FastMCP(
    "claude-memory",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------------------
# MCP tools — called by Claude Code / Cursor via MCP protocol
# ---------------------------------------------------------------------------

@mcp.tool()
async def add_memory(content: str, agent_id: str = "", infer: bool = True, category: str = "", tags: list[str] = []) -> str:
    """Store a fact or observation in memory.
    category: project|feedback|reference|user|session|incident
    tags: free-form list, e.g. ["prod-k8s", "remnant", "cnpg"] — use app names, cluster names, topics.
    Set infer=False to store as-is."""
    if category and category not in mem_store.VALID_CATEGORIES:
        return f"Invalid category '{category}'. Valid: {', '.join(sorted(mem_store.VALID_CATEGORIES - {''}))}"
    # Always queue: an inferred add takes tens of seconds on CPU Ollama and the
    # caller doesn't need the extraction result.
    try:
        job_id = await enqueue(content, agent_id, infer, category, tags)
    except QueueFull as e:
        return f"Memory server busy, not stored: {e}. Retry later."
    return json.dumps({"queued": True, "job_id": job_id})


@mcp.tool()
async def search_memory(query: str, limit: int = 5) -> str:
    """Search memory semantically. Returns top matching facts, one per line:
    [score] date · category · #tags · id=<id> — text
    Use the id with update_memory / delete_memory. Prefer newer facts when two conflict."""
    results = await _to_thread(mem_store.search, query, limit=limit)
    if not results:
        return "No memories found."
    return "\n".join(_format_memory_line(r, with_id=True) for r in results)


@mcp.tool()
async def get_all_memories(limit: int = 0, category: str = "") -> str:
    """List stored memories, newest first. Paged to protect your context window:
    default limit is 50 (max 500). Prefer search_memory for lookups."""
    results = await _to_thread(mem_store.get_all)
    if not results:
        return "No memories stored."
    if category:
        results = [r for r in results if _extract_meta(r).get("category", "") == category]
    results.sort(key=lambda r: r.get("created_at", "") or "", reverse=True)
    limit = limit if limit > 0 else settings.get_all_default_limit
    limit = min(limit, 500)
    shown = results[:limit]
    header = f"Showing {len(shown)} of {len(results)} memories (newest first)."
    return header + "\n" + "\n".join(
        f"{i+1}. {_format_memory_line(r, with_id=True, with_score=False)}" for i, r in enumerate(shown)
    )


@mcp.tool()
async def update_memory(memory_id: str, content: str) -> str:
    """Replace the text of an existing memory (ids come from search_memory).
    Use this to correct an outdated fact instead of adding a contradicting one."""
    content = (content or "").strip()
    if not content:
        return "content must not be empty."
    result = await run_write(mem_store.update, memory_id, content)
    return json.dumps(result, default=str)


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete a memory by ID."""
    result = await run_write(mem_store.delete, memory_id)
    return json.dumps(result)


@mcp.tool()
async def checkpoint(session_id: str, state: str, project: str = "") -> str:
    """Save your working state for this session. It is stored instantly and
    re-injected after context compaction, so you can continue where you were.

    Call it after each milestone, decision or failed attempt. Overwrites the
    previous checkpoint — always send the full, current state, condensed:

      ## Goal        what the user wants, in one or two lines
      ## Done        completed steps (short)
      ## Decisions   choice + why
      ## Dead ends   what was tried and why it failed — so you don't retry it
      ## Key files   paths / resources that matter
      ## Next        the immediate next step(s)

    session_id: the id from your memory briefing. Without a briefing, use
    "<project>-<YYYYMMDD>". project: repo or topic name (optional)."""
    session_id = (session_id or "").strip()
    state = (state or "").strip()
    if not session_id or not state:
        return "session_id and state are required."
    if len(state) > settings.checkpoint_max_chars:
        return (f"Checkpoint too long ({len(state)} chars, max {settings.checkpoint_max_chars}). "
                "Condense it — drop finished detail, keep goal, decisions, dead ends, next steps.")
    row = await _to_thread(mem_store.put_checkpoint, session_id, state, project.strip(), "")
    return json.dumps({"ok": True, "session_id": row["session_id"], "updated_at": str(row["updated_at"])})


@mcp.tool()
async def get_checkpoint(session_id: str = "", project: str = "") -> str:
    """Read a saved session checkpoint. With session_id: that session's state.
    With only project: the most recent session state for that project
    (useful to pick up work started in another session or harness)."""
    session_id = (session_id or "").strip()
    project = (project or "").strip()
    if session_id:
        row = await _to_thread(mem_store.get_checkpoint, session_id)
    elif project:
        rows = await _to_thread(mem_store.list_checkpoints, project, "", 1)
        row = rows[0] if rows else None
    else:
        return "Pass session_id or project."
    if not row:
        return "No checkpoint found."
    return _format_checkpoint(row, "Checkpoint")


# Build the FastMCP Starlette app and extract the ASGI handler directly
# to bypass Starlette's redirect_slashes (caused 307s on POST /mcp).
_starlette_app = mcp.streamable_http_app()
_mcp_handler = _starlette_app.router.routes[0].app


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------

def _json_response(data, status=200):
    body = json.dumps(data, default=str).encode()
    return status, [[b"content-type", b"application/json"],
                    [b"access-control-allow-origin", b"*"]], body


async def _send_response(send, status, headers, body):
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _check_auth(headers: dict) -> bool:
    auth = headers.get(b"authorization", b"").decode()
    # Accept both "Bearer <token>" and plain token formats
    token = auth.removeprefix("Bearer ").strip()
    return token == settings.bearer_token


def _get_agent_id(headers: dict) -> str:
    return headers.get(b"x-agent-id", b"").decode().strip()


def _parse_qs(qs: str) -> dict:
    params = {}
    for part in qs.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = urllib.parse.unquote_plus(v)
        elif part:
            params[part] = ""
    return params


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        msg = await receive()
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


def _make_receive(body: bytes, original_receive=None):
    """One-shot ASGI receive that replays a pre-read body.

    After the body is replayed, delegates to original_receive so that SSE
    EventSourceResponse can wait for actual client disconnect rather than
    getting an immediate http.disconnect signal.
    """
    done = False

    async def _receive():
        nonlocal done
        if not done:
            done = True
            return {"type": "http.request", "body": body, "more_body": False}
        if original_receive is not None:
            return await original_receive()
        return {"type": "http.disconnect"}

    return _receive


async def _mcp_handler_strip_output_schema(scope, receive, send):
    """Call the MCP handler and strip outputSchema from tools/list responses.

    outputSchema was added in MCP spec 2025-03-26. Claude Code 2.x uses
    2024-11-05 and silently drops all tools if outputSchema is present.
    """
    status_out = None
    headers_out = []
    body_parts = []

    async def _capture(event):
        nonlocal status_out, headers_out
        if event["type"] == "http.response.start":
            status_out = event["status"]
            headers_out = event.get("headers", [])
        elif event["type"] == "http.response.body":
            body_parts.append(event.get("body", b""))

    await _mcp_handler(scope, receive, _capture)

    def _strip(obj):
        if isinstance(obj, dict):
            for t in (obj.get("result") or {}).get("tools", []) or []:
                t.pop("outputSchema", None)
        return obj

    raw = b"".join(body_parts).decode("utf-8", errors="replace")
    new_body = None
    # json_response=True (our config) returns a plain JSON body; SSE framing is
    # handled too in case a client negotiates an event stream.
    try:
        new_body = json.dumps(_strip(json.loads(raw))).encode("utf-8")
    except ValueError:
        out = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    line = "data: " + json.dumps(_strip(json.loads(line[5:].strip())))
                except ValueError:
                    pass
            out.append(line)
        new_body = ("\n".join(out) + ("\n" if raw.endswith("\n") else "")).encode("utf-8")

    # The body changed size: replace the original content-length.
    headers_out = [h for h in headers_out if bytes(h[0]).lower() != b"content-length"]
    headers_out.append((b"content-length", str(len(new_body)).encode()))

    await send({"type": "http.response.start", "status": status_out or 200, "headers": headers_out})
    await send({"type": "http.response.body", "body": new_body})


def _extract_meta(r: dict) -> dict:
    """mem0 double-wraps metadata as {metadata: {category: ...}} — unwrap both forms."""
    raw = r.get("metadata") or {}
    # mem0 pgvector provider wraps: payload.metadata = {"metadata": {"category": "..."}}
    inner = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None
    return inner if inner is not None else raw


def _memory_dict(r: dict) -> dict:
    """Normalize a mem0 result to a consistent API shape."""
    meta = _extract_meta(r)
    return {
        "id": r.get("id", ""),
        "memory": r.get("memory", r.get("text", "")),
        "agent_id": r.get("agent_id", ""),
        "category": meta.get("category", ""),
        "tags": meta.get("tags") or [],
        "created_at": r.get("created_at", ""),
        "updated_at": r.get("updated_at", ""),
        "score": r.get("score"),
    }


_ORIGIN_RE = re.compile(r"^\[origin:[^\]]*\]\s*")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)


def _condense(text: str, limit: int) -> str:
    """Short form of a memory for hook injection. Imported memory files carry an
    "[origin: …]" prefix and YAML frontmatter; keep the description and the
    start of the body, on one line, capped at `limit` chars."""
    text = _ORIGIN_RE.sub("", (text or "").strip())
    m = _FRONTMATTER_RE.match(text)
    if m:
        desc = _DESC_RE.search(m.group(1))
        body = text[m.end():].strip()
        desc_text = desc.group(1).strip().strip("'\"") if desc else ""
        text = f"{desc_text} — {body}" if desc_text and body else (desc_text or body)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_memory_line(r: dict, with_id: bool = False, with_score: bool = True,
                        max_chars: int = 0) -> str:
    """One compact line per memory: [score] date · category · #tags · id=… — text
    max_chars > 0 condenses the text (hooks); the MCP tools show it in full."""
    meta = _extract_meta(r)
    parts = []
    date = str(r.get("updated_at") or r.get("created_at") or "")[:10]
    if date:
        parts.append(date)
    if meta.get("category"):
        parts.append(meta["category"])
    if meta.get("tags"):
        parts.append("#" + ",".join(meta["tags"]))
    if with_id and r.get("id"):
        parts.append(f"id={r['id']}")
    text = r.get("memory", r.get("text", ""))
    if max_chars > 0:
        text = _condense(text, max_chars)
    prefix = ""
    score = r.get("score")
    if with_score and isinstance(score, (int, float)):
        prefix = f"[{score:.2f}] "
    return f"{prefix}{' · '.join(parts)} — {text}" if parts else f"{prefix}{text}"


def _format_checkpoint(row: dict, title: str) -> str:
    when = str(row.get("updated_at", ""))[:16]
    who = " · ".join(x for x in (row.get("project"), row.get("harness")) if x)
    out = [f"### {title} (session {row['session_id']}{' · ' + who if who else ''}, updated {when})"]
    if row.get("state"):
        out.append(row["state"].strip())
    if row.get("auto_notes"):
        out.append("#### Auto-captured before the last compaction (raw, may be partial)")
        out.append(row["auto_notes"].strip())
    return "\n".join(out)


async def _search_for_hook(query: str, limit: int, min_score: float) -> list[dict]:
    """Semantic search with a hard deadline: a slow Ollama must never stall a
    harness hook. Returns [] on timeout or error."""
    query = (query or "").strip()[:1000]
    if not query:
        return []
    try:
        results = await asyncio.wait_for(
            _to_thread(mem_store.search, query, limit=max(limit * 3, 10)),
            timeout=settings.hook_search_timeout,
        )
    except Exception as e:
        logger.warning("hook search skipped: %s", type(e).__name__)
        return []
    hits = [r for r in results if isinstance(r.get("score"), (int, float)) and r["score"] >= min_score]
    hits.sort(key=lambda r: r["score"], reverse=True)
    return hits[:limit]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n… [truncated by claude-memory]"


async def _build_briefing(session_id: str, project: str, source: str, harness: str, tool: str) -> str:
    source = source or "startup"
    tool = tool or "checkpoint"
    lines = ["## Memory briefing (claude-memory)"]
    if session_id:
        lines.append(
            f"Memory session_id: `{session_id}`. Keep your working state in memory: after each "
            f"milestone, decision or failed attempt call `{tool}` (session_id=`{session_id}`) with the "
            "full current state — Goal / Done / Decisions (+why) / Dead ends / Key files / Next. "
            "It is re-injected after context compaction."
        )
    if source == "compact":
        lines.append("Context was just compacted. The checkpoint below is your authoritative "
                     "working state — trust it over the compaction summary where they differ.")

    if session_id and (project or harness):
        # Record project/harness now, so a later MCP `checkpoint` (which knows
        # neither) still belongs to this project for the next session's
        # "Last session on <project>" briefing. Never fatal.
        try:
            await _to_thread(mem_store.register_session, session_id, project, harness)
        except Exception as e:
            logger.warning("register_session failed: %s", type(e).__name__)

    current = await _to_thread(mem_store.get_checkpoint, session_id) if session_id else None
    query = ""
    if current and (current.get("state") or current.get("auto_notes")):
        lines.append("")
        lines.append(_format_checkpoint(current, "Current session checkpoint"))
        query = current.get("state") or current.get("auto_notes") or ""
    elif project:
        previous = await _to_thread(mem_store.list_checkpoints, project, session_id, 1)
        if previous:
            lines.append("")
            lines.append(_format_checkpoint(previous[0], f"Last session on {project}"))
            lines.append("(From an earlier session — only continue it if the user's request matches.)")
            query = previous[0].get("state") or ""

    hits = await _search_for_hook(query[:600] or project, limit=5, min_score=settings.recall_min_score)
    if hits:
        lines.append("")
        lines.append("### Relevant long-term memories (shortened; full text: search_memory)")
        lines.extend(f"- {_format_memory_line(r, max_chars=settings.hook_hit_max_chars)}" for r in hits)

    return _truncate("\n".join(lines), settings.briefing_max_chars)


async def _build_recall(prompt: str, limit: int, min_score: float) -> str:
    prompt = (prompt or "").strip()
    if len(prompt) < settings.recall_min_prompt_chars:
        return ""
    hits = await _search_for_hook(prompt, limit=limit, min_score=min_score)
    if not hits:
        return ""
    return "### Possibly relevant memories (claude-memory, shortened; full text: search_memory)\n" + "\n".join(
        f"- {_format_memory_line(r, max_chars=settings.hook_hit_max_chars)}" for r in hits
    )


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_metrics_cache: dict = {"time": 0.0, "data": None}


async def _get_all_cached() -> list[dict]:
    """Full scan for /metrics, cached so every Prometheus scrape isn't a table scan."""
    now = time.monotonic()
    if _metrics_cache["data"] is None or now - _metrics_cache["time"] > settings.metrics_cache_seconds:
        _metrics_cache["data"] = await _to_thread(mem_store.get_all)
        _metrics_cache["time"] = now
    return _metrics_cache["data"]


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await _starlette_app(scope, receive, send)
        return

    if scope["type"] != "http":
        return

    path = scope.get("path", "")
    method = scope.get("method", "GET")
    headers = dict(scope.get("headers", []))

    # Inject Accept header for MCP endpoint if missing (Claude Code compatibility)
    if path == "/mcp":
        accept = headers.get(b"accept", b"").decode()
        if "text/event-stream" not in accept and "application/json" not in accept:
            # Modify scope headers to include the required Accept header
            new_headers = []
            found_accept = False
            for h in scope.get("headers", []):
                if h[0] == b"accept":
                    new_headers.append((b"accept", b"application/json, text/event-stream"))
                    found_accept = True
                else:
                    new_headers.append(h)
            if not found_accept:
                new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope["headers"] = new_headers

    # Health — no auth
    if path == "/health":
        await _send_response(send, *_json_response({"status": "ok", "service": "claude-memory"}))
        return

    # Prometheus metrics — no auth
    if path == "/metrics" and method == "GET":
        try:
            all_memories = await _get_all_cached()
        except Exception:
            logger.exception("metrics: get_all failed")
            await _send_response(send, 503, [[b"content-type", b"text/plain"]], b"db unavailable")
            return
        categories: dict = {}
        agents: dict = {}
        tags: dict = {}
        for r in all_memories:
            meta = _extract_meta(r)
            cat = meta.get("category", "") or "uncategorized"
            categories[cat] = categories.get(cat, 0) + 1
            agent = r.get("agent_id") or "unknown"
            agents[agent] = agents.get(agent, 0) + 1
            for tag in (meta.get("tags") or []):
                tags[tag] = tags.get(tag, 0) + 1
        lines = [
            "# HELP remnant_memories_total Total number of memories stored",
            "# TYPE remnant_memories_total gauge",
        ]
        for cat, count in sorted(categories.items()):
            lines.append(f'remnant_memories_total{{category="{cat}"}} {count}')
        lines += [
            "# HELP remnant_memories_by_agent Total memories per agent",
            "# TYPE remnant_memories_by_agent gauge",
        ]
        for agent, count in sorted(agents.items()):
            lines.append(f'remnant_memories_by_agent{{agent_id="{agent}"}} {count}')
        if tags:
            lines += [
                "# HELP remnant_memories_by_tag Total memories per tag",
                "# TYPE remnant_memories_by_tag gauge",
            ]
            for tag, count in sorted(tags.items()):
                lines.append(f'remnant_memories_by_tag{{tag="{tag}"}} {count}')
        try:
            depth = await queue_depth()
            lines += [
                "# HELP remnant_queue_depth Current number of jobs pending in the add_memory queue",
                "# TYPE remnant_queue_depth gauge",
                f"remnant_queue_depth {depth}",
            ]
        except Exception:
            pass
        lines.append("")
        body = "\n".join(lines).encode()
        await _send_response(send, 200, [[b"content-type", b"text/plain; version=0.0.4"]], body)
        return

    # All /api/* routes require bearer auth
    if path.startswith("/api/"):
        if not _check_auth(headers):
            await _send_response(send, 401, [[b"content-type", b"text/plain"]], b"Unauthorized")
            return

        agent_id = _get_agent_id(headers)
        qs = _parse_qs(scope.get("query_string", b"").decode())

        # ---- Harness hook endpoints -------------------------------------
        # Return {"context": "<text ready to inject>"}; adapters wrap it in
        # their harness's output format. All logic lives here so every
        # harness behaves the same.

        # GET /api/hooks/briefing?session_id=&project=&source=startup|resume|compact&harness=&tool=
        if path == "/api/hooks/briefing" and method == "GET":
            try:
                context = await _build_briefing(
                    qs.get("session_id", "").strip(),
                    qs.get("project", "").strip(),
                    qs.get("source", "startup").strip(),
                    qs.get("harness", "").strip() or agent_id,
                    qs.get("tool", "").strip(),
                )
            except Exception:
                logger.exception("briefing failed")
                await _send_response(send, *_json_response({"context": ""}, status=503))
                return
            await _send_response(send, *_json_response({"context": context}))
            return

        # GET /api/hooks/recall?q=&limit=&min_score=
        if path == "/api/hooks/recall" and method == "GET":
            context = await _build_recall(
                qs.get("q", ""),
                max(1, min(_int(qs.get("limit"), settings.recall_limit), 10)),
                _float(qs.get("min_score"), settings.recall_min_score),
            )
            await _send_response(send, *_json_response({"context": context}))
            return

        # POST /api/hooks/precompact {session_id, project, harness, notes}
        if path == "/api/hooks/precompact" and method == "POST":
            try:
                body_data = json.loads(await _read_body(receive))
                session_id = str(body_data.get("session_id", "")).strip()
                notes = str(body_data.get("notes", "")).strip()
            except Exception:
                await _send_response(send, 400, [[b"content-type", b"text/plain"]], b"Bad Request")
                return
            if not session_id or not notes:
                await _send_response(send, *_json_response({"ok": False, "error": "session_id and notes required"}, status=400))
                return
            notes = _truncate(notes, settings.auto_notes_max_chars)
            await _to_thread(
                mem_store.put_auto_notes, session_id, notes,
                str(body_data.get("project", "")).strip(),
                str(body_data.get("harness", "")).strip() or agent_id,
            )
            await _send_response(send, *_json_response({"ok": True}))
            return

        # ---- Session checkpoints -----------------------------------------

        # GET /api/sessions?project=&harness=&limit=
        if path == "/api/sessions" and method == "GET":
            rows = await _to_thread(
                mem_store.list_checkpoints, qs.get("project", "").strip(), "",
                _int(qs.get("limit"), 20), qs.get("harness", "").strip(),
            )
            await _send_response(send, *_json_response(rows))
            return

        if path.startswith("/api/sessions/"):
            session_id = urllib.parse.unquote(path[len("/api/sessions/"):]).strip()
            if not session_id:
                await _send_response(send, 404, [[b"content-type", b"text/plain"]], b"Not Found")
                return

            # GET /api/sessions/{id}
            if method == "GET":
                row = await _to_thread(mem_store.get_checkpoint, session_id)
                if row is None:
                    await _send_response(send, 404, [[b"content-type", b"text/plain"]], b"Not Found")
                    return
                await _send_response(send, *_json_response(row))
                return

            # PUT /api/sessions/{id} {state, project?, harness?}
            if method == "PUT":
                try:
                    body_data = json.loads(await _read_body(receive))
                    state = str(body_data.get("state", "")).strip()
                except Exception:
                    await _send_response(send, 400, [[b"content-type", b"text/plain"]], b"Bad Request")
                    return
                if not state:
                    await _send_response(send, *_json_response({"ok": False, "error": "state required"}, status=400))
                    return
                if len(state) > settings.checkpoint_max_chars:
                    await _send_response(send, *_json_response({
                        "ok": False,
                        "error": f"state too long ({len(state)} chars, max {settings.checkpoint_max_chars}); condense it",
                    }, status=413))
                    return
                row = await _to_thread(
                    mem_store.put_checkpoint, session_id, state,
                    str(body_data.get("project", "")).strip(),
                    str(body_data.get("harness", "")).strip() or agent_id,
                )
                await _send_response(send, *_json_response({"ok": True, **row}))
                return

            # DELETE /api/sessions/{id}
            if method == "DELETE":
                deleted = await _to_thread(mem_store.delete_checkpoint, session_id)
                if not deleted:
                    await _send_response(send, 404, [[b"content-type", b"text/plain"]], b"Not Found")
                    return
                await _send_response(send, *_json_response({"ok": True, "session_id": session_id}))
                return

        # GET /api/stats
        if path == "/api/stats" and method == "GET":
            all_memories = await _to_thread(mem_store.get_all)
            total = len(all_memories)
            dates = [r.get("created_at", "") for r in all_memories if r.get("created_at")]
            dates_sorted = sorted(dates)
            agents: dict = {}
            categories: dict = {}
            tags_count: dict = {}
            for r in all_memories:
                a = r.get("agent_id") or "unknown"
                agents[a] = agents.get(a, 0) + 1
                meta = _extract_meta(r)
                c = meta.get("category", "") or "uncategorized"
                categories[c] = categories.get(c, 0) + 1
                for tag in (meta.get("tags") or []):
                    tags_count[tag] = tags_count.get(tag, 0) + 1
            await _send_response(send, *_json_response({
                "total": total,
                "first_added": dates_sorted[0] if dates_sorted else None,
                "last_added": dates_sorted[-1] if dates_sorted else None,
                "by_agent": agents,
                "by_category": categories,
                "by_tag": tags_count,
            }))
            return

        # GET /api/tag-rules
        if path == "/api/tag-rules" and method == "GET":
            rules = await _to_thread(mem_store.list_tag_rules)
            grouped: dict = {}
            for r in rules:
                tag = r["tag"]
                if tag not in grouped:
                    grouped[tag] = {"tag": tag, "keywords": [], "source": r["source"]}
                grouped[tag]["keywords"].append(r["keyword"])
            await _send_response(send, *_json_response(list(grouped.values())))
            return

        # POST /api/tag-rules
        if path == "/api/tag-rules" and method == "POST":
            raw = await _read_body(receive)
            try:
                body_data = json.loads(raw)
                tag = body_data.get("tag", "").strip()
                keyword = body_data.get("keyword", "").strip()
            except Exception:
                await _send_response(send, 400, [[b"content-type", b"text/plain"]], b"Bad Request")
                return
            if not tag or not keyword:
                await _send_response(send, 400, [[b"content-type", b"text/plain"]], b"tag and keyword required")
                return
            ok = await _to_thread(mem_store.add_tag_rule, tag, keyword)
            await _send_response(send, *_json_response({"ok": ok}))
            return

        # DELETE /api/tag-rules/{tag}/{keyword}
        if path.startswith("/api/tag-rules/") and method == "DELETE":
            rest = path[len("/api/tag-rules/"):]
            if "/" in rest:
                tag_part, kw_part = rest.split("/", 1)
                ok = await _to_thread(
                    mem_store.delete_tag_rule,
                    urllib.parse.unquote(tag_part),
                    urllib.parse.unquote(kw_part),
                )
                await _send_response(send, *_json_response({"ok": ok}))
                return

        # GET /api/jobs
        if path == "/api/jobs" and method == "GET":
            jobs = await _to_thread(mem_store.list_job_runs)
            await _send_response(send, *_json_response(jobs))
            return

        # GET /api/jobs/{id}
        if path.startswith("/api/jobs/") and method == "GET":
            run_id = path[len("/api/jobs/"):]
            job = await _to_thread(mem_store.get_job_run, run_id)
            if job is None:
                await _send_response(send, 404, [[b"content-type", b"text/plain"]], b"Not Found")
                return
            await _send_response(send, *_json_response(job))
            return

        # GET /api/memories/export — download all memories as JSON
        if path == "/api/memories/export" and method == "GET":
            all_memories = await _to_thread(mem_store.get_all)
            body = json.dumps([_memory_dict(r) for r in all_memories], default=str, indent=2).encode()
            await _send_response(send, 200, [
                [b"content-type", b"application/json"],
                [b"content-disposition", b"attachment; filename=\"memories.json\""],
            ], body)
            return

        # POST /api/memories/import — bulk import from JSON array
        if path == "/api/memories/import" and method == "POST":
            raw = await _read_body(receive)
            try:
                items = json.loads(raw)
                if not isinstance(items, list):
                    raise ValueError("expected JSON array")
            except Exception as e:
                await _send_response(send, 400, [[b"content-type", b"text/plain"]], str(e).encode())
                return
            imported = 0
            try:
                for item in items:
                    content = item.get("memory") or item.get("text") or item.get("content", "")
                    src_agent = item.get("agent_id", "") or agent_id
                    if content.strip():
                        await enqueue(content.strip(), src_agent, True, "", [])
                        imported += 1
            except QueueFull as e:
                await _send_response(send, *_json_response(
                    {"imported": imported, "queued": True, "error": str(e)}, status=503))
                return
            await _send_response(send, *_json_response({"imported": imported, "queued": True}))
            return

        # GET /api/memories — list / search
        if path == "/api/memories" and method == "GET":
            q = qs.get("q", "").strip()
            sort = qs.get("sort", "newest")  # newest | oldest | relevance
            filter_agent = qs.get("agent_id", "").strip()
            filter_category = qs.get("category", "").strip()

            if q:
                results = await _to_thread(mem_store.search, q, limit=200)
                if sort in ("newest", "oldest"):
                    reverse = sort == "newest"
                    results.sort(key=lambda r: r.get("created_at", ""), reverse=reverse)
            else:
                results = await _to_thread(mem_store.get_all, agent_id=filter_agent)
                reverse = sort != "oldest"
                results.sort(key=lambda r: r.get("created_at", ""), reverse=reverse)

            if filter_category:
                results = [r for r in results if _extract_meta(r).get("category", "") == filter_category]

            await _send_response(send, *_json_response([_memory_dict(r) for r in results]))
            return

        # POST /api/memories — add a memory
        if path == "/api/memories" and method == "POST":
            raw = await _read_body(receive)
            # Support both form-encoded and JSON body
            try:
                body_data = json.loads(raw)
                content = body_data.get("content", "").strip()
                src_agent = body_data.get("agent_id", "") or agent_id
            except Exception:
                params = dict(p.split("=", 1) for p in raw.decode().split("&") if "=" in p)
                content = urllib.parse.unquote_plus(params.get("content", "")).strip()
                src_agent = agent_id
            if content:
                try:
                    await enqueue(content, src_agent, True, "", [])
                except QueueFull as e:
                    await _send_response(send, *_json_response({"ok": False, "error": str(e)}, status=503))
                    return
            await _send_response(send, *_json_response({"ok": True, "queued": bool(content)}))
            return

        # Routes with memory ID suffix
        if path.startswith("/api/memories/"):
            memory_id = path[len("/api/memories/"):]

            # PUT /api/memories/{id} — edit content
            if method == "PUT":
                raw = await _read_body(receive)
                try:
                    body_data = json.loads(raw)
                    content = body_data.get("memory", body_data.get("content", "")).strip()
                except Exception:
                    params = dict(p.split("=", 1) for p in raw.decode().split("&") if "=" in p)
                    content = urllib.parse.unquote_plus(params.get("content", "")).strip()
                if content:
                    await run_write(mem_store.update, memory_id, content)
                await _send_response(send, *_json_response({"ok": True}))
                return

            # PATCH /api/memories/{id}/tags — update tags only
            if method == "PATCH" and memory_id.endswith("/tags"):
                real_id = memory_id[: -len("/tags")]
                raw = await _read_body(receive)
                try:
                    tags = json.loads(raw).get("tags", [])
                    if not isinstance(tags, list):
                        raise ValueError
                    tags = [str(t).strip() for t in tags if str(t).strip()]
                except Exception:
                    await _send_response(send, 400, [[b"content-type", b"text/plain"]], b"Bad Request")
                    return
                ok = await _to_thread(mem_store.update_tags, real_id, tags)
                await _send_response(send, *_json_response({"ok": ok}))
                return

            # DELETE /api/memories/{id}
            if method == "DELETE":
                await run_write(mem_store.delete, memory_id)
                await _send_response(send, *_json_response({"ok": True}))
                return

        await _send_response(send, 404, [[b"content-type", b"text/plain"]], b"Not Found")
        return

    # /mcp — MCP protocol (bearer auth required)
    if not _check_auth(headers):
        await _send_response(send, 401, [[b"content-type", b"text/plain"]], b"Unauthorized")
        return

    # Read body once so we can inspect method and replay it to the handler
    raw = await _read_body(receive)
    try:
        payload = json.loads(raw)
        method = payload.get("method")

        if method == "initialize":
            # In stateless_http=True mode, ServerSession starts as Initialized and
            # ClientRequest validation rejects "initialize" (INVALID_PARAMS).
            # Handle it here directly and return a valid capabilities response.
            init_opts = mcp._mcp_server.create_initialization_options()
            protocol_version = (payload.get("params") or {}).get(
                "protocolVersion", "2024-11-05"
            )
            result = {
                "protocolVersion": protocol_version,
                "capabilities": init_opts.capabilities.model_dump(exclude_none=True),
                "serverInfo": {
                    "name": init_opts.server_name,
                    "version": init_opts.server_version or "0.1.0",
                },
            }
            if init_opts.instructions:
                result["instructions"] = init_opts.instructions
            sse_event = f"data: {json.dumps({'jsonrpc':'2.0','id':payload.get('id'),'result':result})}\n\n"
            await send({"type": "http.response.start", "status": 200, "headers": [
                [b"content-type", b"text/event-stream"],
                [b"cache-control", b"no-cache, no-transform"],
            ]})
            await send({"type": "http.response.body", "body": sse_event.encode("utf-8")})
            return

        if method == "tools/list":
            await _mcp_handler_strip_output_schema(scope, _make_receive(raw, receive), send)
            return
    except Exception:
        pass
    await _mcp_handler(scope, _make_receive(raw, receive), send)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
