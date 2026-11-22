import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import auth
import memory as mem_store
import users as users_store
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
async def add_memory(
    content: str,
    agent_id: str = "",
    infer: bool = False,
    category: str = "",
    tags: list[str] = [],
    shared: bool = False,
) -> str:
    """Store a fact or observation in long-term memory (shared by all agents).
    Write it as one or more condensed, self-contained statements — it is stored
    exactly as written. Keep names, paths, versions and numbers precise.
    category: project|feedback|reference|user|session|incident
    tags: free-form list, e.g. ["prod-k8s", "remnant", "cnpg"] — use app names, cluster names, topics.
    infer=True lets the server's small LLM rephrase/split the note first (slow; it
    can distort technical details — only for long, chatty input).
    shared=True stores straight into the shared space visible to every tenant —
    admin tokens only; anything else is rejected and nothing is stored."""
    if category and category not in mem_store.VALID_CATEGORIES:
        return f"Invalid category '{category}'. Valid: {', '.join(sorted(mem_store.VALID_CATEGORIES - {''}))}"
    principal = auth.get_principal()
    if shared and not principal.is_admin:
        return "shared=true requires an admin token - nothing was stored. Omit it to store in your own workspace."
    g = mem_store.embedding_guard()
    if not g["ok"]:
        return f"writes disabled: {g['msg']}"
    # Always queue: an inferred add takes tens of seconds on CPU Ollama and the
    # caller doesn't need the extraction result. The tenant travels with the
    # job so the async write lands in the caller's workspace (or the shared
    # space for admin shared adds, bean vd3y).
    target = settings.shared_tenant_id if shared else principal.user_id
    try:
        job_id = await enqueue(
            content,
            agent_id,
            infer,
            category,
            tags,
            user_id=target,
        )
    except QueueFull as e:
        return f"Memory server busy, not stored: {e}. Retry later."
    return json.dumps({"queued": True, "job_id": job_id, "shared": shared})


@mcp.tool()
async def search_memory(
    query: str, limit: int = 5, mode: str = "full", hybrid: bool = False
) -> str:
    """Search memory. mode="full" (default) returns one line per fact:
    [score] date · category · #tags · id=<id> — text
    mode="index" returns token-cheap rows (snippet + estimated full-text size)
    - pull only what you need with fetch_memories(ids=...). Saves context when
    you are browsing. hybrid=true also runs exact keyword search (error codes,
    identifiers, function names) and fuses both rankings - use it when a plain
    semantic search missed an exact string. Use the id with update_memory /
    delete_memory. Prefer newer facts when two conflict."""
    results = await _to_thread(
        mem_store.search_hybrid if hybrid else mem_store.search,
        query,
        limit=limit,
        user_id=auth.get_principal().user_id,
    )
    if not results:
        return "No memories found."
    if mode == "index":
        lines = []
        for r in results:
            text = r.get("memory", r.get("text", ""))
            est = max(1, len(text) // 4)
            line = _format_memory_line(r, with_id=True, max_chars=140)
            lines.append(f"{line} (~{est}tok full)")
        return "\n".join(lines) + '\nFull text: fetch_memories(ids="<id>,...")'
    return "\n".join(_format_memory_line(r, with_id=True) for r in results)


@mcp.tool()
async def fetch_memories(ids: str) -> str:
    """Fetch full records by id (comma-separated; ids come from search_memory
    mode=index). Second step of the token-efficient two-step search; unknown
    ids are skipped."""
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    recs = await _to_thread(mem_store.get_by_ids, wanted, auth.get_principal().user_id)
    if not recs:
        return "No memories for those ids."
    # no max_chars: full text
    return "\n\n".join(_format_memory_line(r, with_id=True) for r in recs)


@mcp.tool()
async def memory_timeline(id: str, window: int = 3) -> str:
    """What was stored just before/after this memory (same workspace + shared
    space), chronological. Helps walk a thread of related notes."""
    tl = await _to_thread(mem_store.timeline, id, window, auth.get_principal().user_id)
    if tl is None:
        return "Memory not found (or not visible to you)."

    def _fmt(r):
        return _format_memory_line(r, with_id=True, max_chars=140)

    parts = [_fmt(r) for r in tl["before"]]
    parts.append(">>> " + _fmt(tl["self"]))
    parts.extend(_fmt(r) for r in tl["after"])
    return "\n".join(parts)


@mcp.tool()
async def get_all_memories(limit: int = 0, category: str = "") -> str:
    """List stored memories, newest first. Paged to protect your context window:
    default limit is 50 (max 500). Prefer search_memory for lookups."""
    results = await _to_thread(mem_store.get_all, user_id=auth.get_principal().user_id)
    if not results:
        return "No memories stored."
    if category:
        results = [
            r for r in results if _extract_meta(r).get("category", "") == category
        ]
    results.sort(key=lambda r: r.get("created_at", "") or "", reverse=True)
    limit = limit if limit > 0 else settings.get_all_default_limit
    limit = min(limit, 500)
    shown = results[:limit]
    header = f"Showing {len(shown)} of {len(results)} memories (newest first)."
    return (
        header
        + "\n"
        + "\n".join(
            f"{i + 1}. {_format_memory_line(r, with_id=True, with_score=False)}"
            for i, r in enumerate(shown)
        )
    )


async def _may_modify(memory_id: str) -> bool:
    """True if the current principal may modify this memory. Foreign-tenant
    ids are indistinguishable from missing ones (no existence leak)."""
    return await _may_modify_api(memory_id, auth.get_principal())


async def _may_modify_api(memory_id: str, principal) -> bool:
    owner = await _to_thread(mem_store.get_owner, memory_id)
    if owner is None:
        return False
    return owner == principal.user_id or principal.is_admin


@mcp.tool()
async def update_memory(memory_id: str, content: str) -> str:
    """Replace the text of an existing memory (ids come from search_memory).
    Use this to correct an outdated fact instead of adding a contradicting one."""
    content = (content or "").strip()
    if not content:
        return "content must not be empty."
    if not await _may_modify(memory_id):
        return f"No memory with id {memory_id}."
    result = await run_write(mem_store.update, memory_id, content)
    return json.dumps(result, default=str)


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete a memory by ID."""
    if not await _may_modify(memory_id):
        return f"No memory with id {memory_id}."
    result = await run_write(mem_store.delete, memory_id)
    return json.dumps(result)


@mcp.tool()
async def checkpoint(
    session_id: str, state: str, project: str = "", harness: str = ""
) -> str:
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

    Write each Decision and Dead end as one self-contained bullet with what and
    why ("Removed kubeval from CI: its schema host has SSL errors"). After the
    session ends they are copied into long-term memory for all agents.

    session_id: the id from your memory briefing. Without a briefing, use
    "<project>-<YYYYMMDD>". project: repo or topic name (optional).
    harness: which agent tool you are: claude, codex, qwen or pi (optional).
    It labels the session; omit it and an already-known harness is kept."""
    session_id = (session_id or "").strip()
    state = (state or "").strip()
    if not session_id or not state:
        return "session_id and state are required."
    if len(state) > settings.checkpoint_max_chars:
        return (
            f"Checkpoint too long ({len(state)} chars, max {settings.checkpoint_max_chars}). "
            "Condense it — drop finished detail, keep goal, decisions, dead ends, next steps."
        )
    row = await _to_thread(
        mem_store.put_checkpoint,
        session_id,
        state,
        project.strip(),
        _norm_harness(harness),
        auth.get_principal().user_id,
    )
    if row is None:
        return f"session_id {session_id} belongs to another workspace."
    return json.dumps(
        {
            "ok": True,
            "session_id": row["session_id"],
            "updated_at": str(row["updated_at"]),
        }
    )


@mcp.tool()
async def get_checkpoint(session_id: str = "", project: str = "") -> str:
    """Read a saved session checkpoint. With session_id: that session's state.
    With only project: the most recent session state for that project
    (useful to pick up work started in another session or harness)."""
    session_id = (session_id or "").strip()
    project = (project or "").strip()
    p = auth.get_principal()
    if session_id:
        row = await _to_thread(mem_store.get_checkpoint, session_id, p.user_id)
    elif project:
        rows = await _to_thread(
            mem_store.list_checkpoints, project, "", 1, "", p.user_id
        )
        row = rows[0] if rows else None
    else:
        return "Pass session_id or project."
    if not row:
        return "No checkpoint found."
    return _format_checkpoint(row, "Checkpoint")


# Build the FastMCP Starlette app and extract the ASGI handler directly
# to bypass Starlette's redirect_slashes (caused 307s on POST /mcp).
_starlette_app = mcp.streamable_http_app()
_mcp_handler = _starlette_app.router.routes[0].app  # type: ignore[attr-defined]  # Starlette wraps the ASGI app onto routes at runtime


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------


def _write_guard_block():
    """503 + clear message while the embedding dimension guard is tripped."""
    g = mem_store.embedding_guard()
    if not g["ok"]:
        return _json_response({"error": f"writes disabled: {g['msg']}"}, status=503)
    return None


def _json_response(data, status=200):
    body = json.dumps(data, default=str).encode()
    return (
        status,
        [
            [b"content-type", b"application/json"],
            [b"access-control-allow-origin", b"*"],
        ],
        body,
    )


async def _send_response(send, status, headers, body):
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _get_agent_id(headers: dict) -> str:
    return headers.get(b"x-agent-id", b"").decode().strip()


def _admin_target(qs: dict, principal) -> tuple:
    """Which workspace(s) an admin request targets.
    ("own",) | ("tenant", <id>) | ("all",) — scope params are admin-only and
    silently ignored for regular users, who always see their own workspace
    (plus the shared space)."""
    if principal.is_admin:
        t = (qs.get("tenant", "") or "").strip()
        if t:
            return ("tenant", t)
        if (qs.get("scope", "") or "").strip() == "all":
            return ("all",)
    return ("own",)


def _space_param(qs: dict) -> str | None:
    """?space=all|own|shared (bean o9y4): narrows any workspace selection to
    exclude ('own') or contain only ('shared') shared-space rows. '' / 'all'
    = no filter (byte-identical to pre-o9y4 behavior). None = invalid value
    -> the caller answers 400; never silently fall back to 'all'."""
    v = (qs.get("space", "") or "").strip().lower()
    if v in ("", "all"):
        return "all"
    if v in ("own", "shared"):
        return v
    return None


def _bad_space(send):
    return _send_response(
        send,
        *_json_response(
            {"ok": False, "error": "space must be one of: all, own, shared"},
            status=400,
        ),
    )


def _filter_space(rows: list[dict], space: str) -> list[dict]:
    """Post-filter cross-workspace result sets (admin scope=all) by space.
    Applied BEFORE the limit cut so counts and limits stay correct."""
    if space == "all":
        return rows
    shared = settings.shared_tenant_id
    if space == "own":
        return [r for r in rows if r.get("user_id") != shared]
    return [r for r in rows if r.get("user_id") == shared]


def _forbidden(send):
    return _send_response(
        send,
        403,
        [[b"content-type", b"application/json"]],
        json.dumps({"ok": False, "error": "admin only"}).encode(),
    )


# ---------------------------------------------------------------------------
# Failed-auth rate limiting (per IP, generous: hook fleets retry, a tight
# limit is a self-inflicted outage). In-process and approximate — a single
# homelab worker makes that good enough.
# ---------------------------------------------------------------------------
_AUTH_FAIL_LIMIT = 120  # failures
_AUTH_FAIL_WINDOW = 600.0  # per IP per 10 minutes


def _client_ip(headers: dict) -> str:
    """Best-effort client IP for rate limiting — NOT an identity.
    X-Real-IP first (ingress-nginx sets it), X-Forwarded-For first hop as a
    fallback, '' when neither exists. Caveat verified on this cluster: the
    internal ingress sets enable-real-ip=true (realip module), so a
    determined client can spoof these headers; the limiter is therefore
    deliberately generous (120/10min) and the window self-clears, bounding
    any framing attempt. '' means untrusted: never count it, never share a
    bucket for it."""
    real = headers.get(b"x-real-ip", b"").decode(errors="replace").strip()
    if real:
        return real
    xff = headers.get(b"x-forwarded-for", b"").decode(errors="replace")
    return xff.split(",")[0].strip() if xff else ""


def _auth_fail_limited(ip: str) -> bool:
    now = time.monotonic()
    window = _AUTH_FAIL_WINDOWS
    q = [t for t in window.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
    q.append(now)
    window[ip] = q
    if len(window) > 2000:  # keep memory bounded
        for k in [k for k, v in window.items() if now - v[-1] >= _AUTH_FAIL_WINDOW]:
            window.pop(k, None)
    return len(q) > _AUTH_FAIL_LIMIT


_AUTH_FAIL_WINDOWS: dict = {}


async def _tokens_api(
    path: str, method: str, send, receive, qs: dict, principal, admin_view: bool
) -> bool:
    """Self-service (/api/me/tokens) and admin (/api/admin/tokens) token
    management. Plaintext tokens appear exactly once, in the create response.
    Admins can list and revoke anything but never mint for other users."""
    from urllib.parse import unquote

    import tokens as tok_mod

    if admin_view:
        if path == "/api/admin/tokens" and method == "GET":
            rows = await _to_thread(
                tok_mod.list_all, (qs.get("user", "") or "").strip()
            )
            await _send_response(send, *_json_response({"tokens": rows}))
            return True
        if path.startswith("/api/admin/tokens/") and method == "DELETE":
            tid = unquote(path[len("/api/admin/tokens/") :]).strip()
            ok = await _to_thread(tok_mod.revoke, tid, principal.user_id)
            await _send_response(send, *_json_response({"ok": ok}))
            return True
        return False

    if path == "/api/me/tokens" and method == "GET":
        rows = await _to_thread(tok_mod.list_for_user, principal.user_id)
        await _send_response(send, *_json_response({"tokens": rows}))
        return True
    if path == "/api/me/tokens" and method == "POST":
        try:
            body = json.loads(await _read_body(receive) or b"{}")
        except Exception:
            body = {}
        can_admin = bool(body.get("can_admin")) and principal.is_admin
        raw_days = body.get("expires_days")
        clamped_note = ""
        if raw_days in (None, ""):
            days = tok_mod.DEFAULT_EXPIRY_DAYS
        else:
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                await _send_response(
                    send,
                    *_json_response(
                        {"ok": False, "error": "expires_days must be an integer"},
                        status=400,
                    ),
                )
                return True
            clamped = max(1, min(days, tok_mod.MAX_EXPIRY_DAYS))
            if clamped != days:
                clamped_note = f"expires_days {days} clamped to {clamped}"
            days = clamped
        try:
            row, plain = await _to_thread(
                tok_mod.create_token,
                principal.user_id,
                str(body.get("name", "")),
                principal.user_id,
                can_admin,
                days,
                str(body.get("default_harness", "")),
            )
        except Exception as e:
            await _send_response(
                send,
                *_json_response(
                    {
                        "ok": False,
                        "error": f"could not create token: {type(e).__name__}",
                    },
                    status=500,
                ),
            )
            return True
        out = {
            "ok": True,
            "id": row["id"],
            "token": plain,
            "expires_days": days,
            "expires_at": str(row["expires_at"]),
            "can_admin": row["can_admin"],
            "warning": "shown once — store it now; only its hash is kept",
        }
        if clamped_note:
            out["note"] = clamped_note
        await _send_response(send, *_json_response(out))
        return True
    if path.startswith("/api/me/tokens/") and method == "DELETE":
        tid = unquote(path[len("/api/me/tokens/") :]).strip()
        ok = await _to_thread(tok_mod.revoke, tid, principal.user_id, principal.user_id)
        await _send_response(send, *_json_response({"ok": ok}))
        return True
    return False


async def _scoped_memories(qs: dict, principal, agent_id: str = "") -> list[dict]:
    """List memories according to the caller's scope: own workspace + shared,
    or (admins only) one named tenant or everything."""
    mode = _admin_target(qs, principal)
    space = _space_param(qs) or "all"
    shared = settings.shared_tenant_id
    if mode[0] == "all":
        rows = await _to_thread(mem_store.get_all_across, agent_id)
        return _filter_space(rows, space)
    if mode[0] == "tenant":
        t = mode[1]
        if space == "shared":
            # honest intersection: a named tenant is not the shared space
            if t != shared:
                return []
            return await _to_thread(mem_store.get_all, agent_id, t)
        return await _to_thread(mem_store.get_all, agent_id, t, space != "own")
    if space == "shared":
        return await _to_thread(mem_store.get_all, agent_id, shared)
    return await _to_thread(
        mem_store.get_all, agent_id, principal.user_id, space != "own"
    )


async def _scoped_search(q: str, limit: int, qs: dict, principal) -> list[dict]:
    mode = _admin_target(qs, principal)
    space = _space_param(qs) or "all"
    shared = settings.shared_tenant_id
    if mode[0] == "all":
        if space == "shared":
            return await _to_thread(mem_store.search, q, limit=limit, user_id=shared)
        tenants = await _to_thread(users_store.list_tenant_ids)
        rows = await _to_thread(mem_store.search_across, q, limit, tenants)
        return _filter_space(rows, space)
    tenant = mode[1] if mode[0] == "tenant" else principal.user_id
    if space == "shared" and mode[0] == "tenant" and tenant != shared:
        # honest intersection: an explicitly named tenant is not the shared
        # space. (own-mode callers just query the shared space directly.)
        return []
    if qs.get("hybrid", "").strip() in ("1", "true", "yes"):
        return await _to_thread(
            mem_store.search_hybrid,
            q,
            limit=limit,
            user_id=tenant if space != "shared" else shared,
            include_shared=space != "own",
        )
    return await _to_thread(
        mem_store.search,
        q,
        limit=limit,
        user_id=tenant if space != "shared" else shared,
        include_shared=space != "own",
    )


async def _users_api(path: str, method: str, send, receive, qs: dict) -> bool:
    """Admin user management. Returns False when the path is not a user route
    (caller continues routing). The API token appears in the response exactly
    once — at creation or rotation; it is stored only as a hash."""
    if path == "/api/users" and method == "GET":
        rows = await _to_thread(users_store.list_users)
        await _send_response(send, *_json_response(rows))
        return True

    if path == "/api/users" and method == "POST":
        try:
            body = json.loads(await _read_body(receive))
        except Exception:
            await _send_response(
                send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
            )
            return True
        user_id = str(body.get("id", "")).strip() or str(body.get("name", "")).strip()
        try:
            user, token = await _to_thread(
                users_store.create_user,
                user_id,
                str(body.get("name", "")).strip(),
                str(body.get("role", "user")).strip() or "user",
            )
        except ValueError as e:
            await _send_response(
                send, *_json_response({"ok": False, "error": str(e)}, status=400)
            )
            return True
        logger.info("audit: created user %s (role=%s)", user["id"], user["role"])
        await _send_response(
            send,
            *_json_response({"ok": True, "user": user, "token": token}, status=201),
        )
        return True

    if path.startswith("/api/users/"):
        rest = path[len("/api/users/") :]
        if "/" in rest:  # sub-resource: POST /api/users/{id}/rotate-token
            uid, action = rest.split("/", 1)
            uid = urllib.parse.unquote(uid)
            if action == "rotate-token" and method == "POST":
                new_token = await asyncio.to_thread(users_store.rotate_token, uid)
                if new_token is None:
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return True
                logger.info("audit: rotated API token for %s", uid)
                await _send_response(
                    send, *_json_response({"ok": True, "token": new_token})
                )
                return True
            await _send_response(
                send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
            )
            return True

        uid = urllib.parse.unquote(rest)
        if method == "GET":
            target = await asyncio.to_thread(users_store.get_user, uid)
            if target is None:
                await _send_response(
                    send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                )
                return True
            await _send_response(send, *_json_response(target))
            return True
        if method == "PATCH":
            try:
                body = json.loads(await _read_body(receive))
            except Exception:
                await _send_response(
                    send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                )
                return True
            kw: dict[str, Any] = {}
            for key in ("name", "role"):
                if key in body:
                    kw[key] = str(body[key])
            if "disabled" in body:
                kw["disabled"] = bool(body["disabled"])
            try:
                updated = await asyncio.to_thread(users_store.update_user, uid, **kw)
            except ValueError as e:
                await _send_response(
                    send, *_json_response({"ok": False, "error": str(e)}, status=400)
                )
                return True
            if updated is None:
                await _send_response(
                    send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                )
                return True
            logger.info("audit: updated user %s %s", uid, sorted(kw))
            await _send_response(send, *_json_response({"ok": True, "user": updated}))
            return True
        if method == "DELETE":
            try:
                deleted = await _to_thread(users_store.delete_user, uid)
            except ValueError as e:
                await _send_response(
                    send, *_json_response({"ok": False, "error": str(e)}, status=400)
                )
                return True
            if not deleted:
                await _send_response(
                    send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                )
                return True
            logger.info("audit: deleted user %s (memories kept in its namespace)", uid)
            await _send_response(send, *_json_response({"ok": True}))
            return True
    return False


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
        new_body = ("\n".join(out) + ("\n" if raw.endswith("\n") else "")).encode(
            "utf-8"
        )

    # The body changed size: replace the original content-length.
    headers_out = [h for h in headers_out if bytes(h[0]).lower() != b"content-length"]
    headers_out.append((b"content-length", str(len(new_body)).encode()))

    await send(
        {
            "type": "http.response.start",
            "status": status_out or 200,
            "headers": headers_out,
        }
    )
    await send({"type": "http.response.body", "body": new_body})


def _extract_meta(r: dict) -> dict:
    """mem0 double-wraps metadata as {metadata: {category: ...}} — unwrap both forms."""
    raw = r.get("metadata") or {}
    # mem0 pgvector provider wraps: payload.metadata = {"metadata": {"category": "..."}}
    inner = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None
    return inner if inner is not None else raw


def _index_row(r: dict) -> dict:
    """Token-cheap search row: identity + snippet + estimated full size."""
    meta = _extract_meta(r)
    text = r.get("memory", r.get("text", "")) or ""
    return {
        "id": r.get("id"),
        "snippet": _condense(text, 140),
        "score": r.get("score"),
        "date": str(r.get("updated_at") or r.get("created_at") or "")[:10],
        "category": meta.get("category", ""),
        "tags": meta.get("tags") or [],
        "est_tokens_full": max(1, len(text) // 4),
    }


def _memory_dict(r: dict) -> dict:
    """Normalize a mem0 result to a consistent API shape."""
    meta = _extract_meta(r)
    return {
        "id": r.get("id", ""),
        "memory": r.get("memory", r.get("text", "")),
        "agent_id": r.get("agent_id", ""),
        "tenant": r.get(
            "user_id", ""
        ),  # owning workspace (shared space = settings.shared_tenant_id)
        "category": meta.get("category", ""),
        "tags": meta.get("tags") or [],
        "created_at": r.get("created_at", ""),
        "updated_at": r.get("updated_at", ""),
        "score": r.get("score"),
    }


_ORIGIN_RE = re.compile(r"^\[origin:[^\]]*\]\s*")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def _condense(text: str, limit: int) -> str:
    """Short form of a memory for hook injection. Imported memory files carry an
    "[origin: …]" prefix and YAML frontmatter; keep the description and the
    start of the body, on one line, capped at `limit` chars."""
    text = _ORIGIN_RE.sub("", (text or "").strip())
    m = _FRONTMATTER_RE.match(text)
    if m:
        desc = _DESC_RE.search(m.group(1))
        body = text[m.end() :].strip()
        desc_text = desc.group(1).strip().strip("'\"") if desc else ""
        text = f"{desc_text} — {body}" if desc_text and body else (desc_text or body)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_memory_line(
    r: dict, with_id: bool = False, with_score: bool = True, max_chars: int = 0
) -> str:
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
    out = [
        f"### {title} (session {row['session_id']}{' · ' + who if who else ''}, updated {when})"
    ]
    if row.get("state"):
        out.append(row["state"].strip())
    if row.get("auto_notes"):
        out.append(
            "#### Auto-captured before the last compaction (raw, may be partial)"
        )
        out.append(row["auto_notes"].strip())
    return "\n".join(out)


async def _search_for_hook(
    query: str, limit: int, min_score: float, user_id: str = ""
) -> list[dict]:
    """Semantic search with a hard deadline: a slow Ollama must never stall a
    harness hook. Returns [] on timeout or error. Scoped to the caller's
    workspace (plus the shared space)."""
    query = (query or "").strip()[:1000]
    if not query:
        return []
    try:
        results = await asyncio.wait_for(
            _to_thread(
                mem_store.search, query, limit=max(limit * 3, 10), user_id=user_id
            ),
            timeout=settings.hook_search_timeout,
        )
    except Exception as e:
        logger.warning("hook search skipped: %s", type(e).__name__)
        return []
    hits = []
    for r in results:
        score = _rank_score(r)
        if score is not None and score >= min_score:
            hits.append({**r, "score": score})
    hits.sort(key=lambda r: r["score"], reverse=True)
    return hits[:limit]


def _rank_score(r: dict):
    """Similarity used by the hooks. Lines promoted automatically from session
    checkpoints rank slightly below memories someone chose to store."""
    score = r.get("score")
    if not isinstance(score, (int, float)):
        return None
    if "auto-promoted" in (_extract_meta(r).get("tags") or []):
        score -= settings.promoted_recall_penalty
    return score


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n… [truncated by claude-memory]"


def _norm_harness(value: str) -> str:
    """Normalize a self-declared harness name: strip + lowercase, max 32 chars,
    no control characters. Anything invalid becomes empty — a bad harness must
    never make a checkpoint fail. Empty never overwrites a known harness
    (see memory._upsert_session)."""
    h = (value or "").strip().lower()
    if not h or len(h) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in h):
        return ""
    return h


async def _build_briefing(
    session_id: str, project: str, source: str, harness: str, tool: str, principal
) -> str:
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
        lines.append(
            "Context was just compacted. The checkpoint below is your authoritative "
            "working state — trust it over the compaction summary where they differ."
        )

    tenant = principal.user_id
    if session_id and (project or harness):
        # Record project/harness now, so a later MCP `checkpoint` (which knows
        # neither) still belongs to this project for the next session's
        # "Last session on <project>" briefing. Never fatal.
        try:
            await _to_thread(
                mem_store.register_session, session_id, project, harness, tenant
            )
        except Exception as e:
            logger.warning("register_session failed: %s", type(e).__name__)

    current = (
        await _to_thread(mem_store.get_checkpoint, session_id, tenant)
        if session_id
        else None
    )
    query = ""
    if current and (current.get("state") or current.get("auto_notes")):
        lines.append("")
        lines.append(_format_checkpoint(current, "Current session checkpoint"))
        query = current.get("state") or current.get("auto_notes") or ""
    elif project:
        previous = await _to_thread(
            mem_store.list_checkpoints, project, session_id, 1, "", tenant
        )
        if previous:
            lines.append("")
            lines.append(_format_checkpoint(previous[0], f"Last session on {project}"))
            lines.append(
                "(From an earlier session — only continue it if the user's request matches.)"
            )
            query = previous[0].get("state") or ""

    hits = await _search_for_hook(
        query[:600] or project,
        limit=5,
        min_score=settings.recall_min_score,
        user_id=tenant,
    )
    if settings.briefing_token_budget > 0:
        # Compact-index mode: same information, token-budgeted.
        return _build_briefing_compact(
            session_id, source, tool, lines[1:], hits, settings
        )
    if hits:
        lines.append("")
        lines.append(
            "### Relevant long-term memories (shortened; full text: search_memory)"
        )
        lines.extend(
            f"- {_format_memory_line(r, max_chars=settings.hook_hit_max_chars)}"
            for r in hits
        )

    return _truncate("\n".join(lines), settings.briefing_max_chars)


def _est_tokens(text: str) -> int:
    """chars/4 heuristic (same order of magnitude the header comments use:
    9000 chars ~ 2.2k tokens). No tokenizer dependency in the server."""
    return max(1, len(text) // 4)


def _build_briefing_compact(
    session_id: str,
    source: str,
    tool: str,
    head_lines: list[str],
    hits: list[dict],
    settings,
) -> str:
    """SessionStart briefing as a compact index within briefing_token_budget
    estimated tokens: header + session_id + checkpoint (truncated if needed)
    + one short id-bearing line per memory until the budget runs out. The
    agent can always pull full text via search_memory / get_all_memories."""
    budget = settings.briefing_token_budget
    parts: list[str] = ["## Memory briefing (claude-memory, compact index)"]
    used = _est_tokens(parts[0])
    cp_lines = [ln for ln in head_lines if ln]
    if session_id:
        line = (
            f"session_id: `{session_id}` - keep working state current with "
            f"`{tool}`; re-injected after compaction."
        )
        parts.append(line)
        used += _est_tokens(line)
    if source == "compact":
        line = "Context was just compacted; the checkpoint below is authoritative."
        parts.append(line)
        used += _est_tokens(line)

    # checkpoint section (everything before the memory hits)
    cp_text = "\n".join(cp_lines).strip()
    cp_budget = max(0, budget - used - 60)  # leave room for >=1 index line + footer
    cp_tok = 0
    dropped_mem = len(hits)
    mem_lines: list[str] = []
    if cp_text:
        if _est_tokens(cp_text) > cp_budget:
            cp_text = _truncate(cp_text, cp_budget * 4)
        cp_tok = _est_tokens(cp_text)
        used += cp_tok
        parts.append("")
        parts.append(cp_text)

    for r in hits:
        line = "- " + _format_memory_line(r, with_id=True, max_chars=90)
        tok = _est_tokens(line)
        if used + tok > budget - 20:
            dropped_mem = len(hits) - len(mem_lines)
            break
        mem_lines.append(line)
        used += tok
    if mem_lines:
        parts.append("")
        parts.append("### Memory index (full text: search_memory)")
        parts.extend(mem_lines)
    footer = (
        f"(budget ~{budget} tok, used ~{used}: checkpoint ~{cp_tok}, "
        f"memories {len(mem_lines)}/{len(hits)}, dropped {dropped_mem}; "
        "estimate = chars/4)"
    )
    parts.append(footer)
    return "\n".join(parts)


async def _build_recall(
    prompt: str, limit: int, min_score: float, user_id: str = ""
) -> str:
    prompt = (prompt or "").strip()
    if len(prompt) < settings.recall_min_prompt_chars:
        return ""
    hits = await _search_for_hook(
        prompt, limit=limit, min_score=min_score, user_id=user_id
    )
    if not hits:
        return ""
    return (
        "### Possibly relevant memories (claude-memory, shortened; full text: search_memory)\n"
        + "\n".join(
            f"- {_format_memory_line(r, max_chars=settings.hook_hit_max_chars)}"
            for r in hits
        )
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
    """Full scan for /metrics, cached so every Prometheus scrape isn't a table
    scan. /metrics stays cross-tenant (aggregate counts only, no content)."""
    now = time.monotonic()
    if (
        _metrics_cache["data"] is None
        or now - _metrics_cache["time"] > settings.metrics_cache_seconds
    ):
        _metrics_cache["data"] = await _to_thread(mem_store.get_all_across)
        _metrics_cache["time"] = now
    return _metrics_cache["data"]


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

_startup_init_started = False


def _startup_schema_init() -> None:
    """Best-effort schema + service-token init at server start (users first:
    tokens.ensure_table imports from it). Runs in a background thread with a
    few retries so the legacy-hooks row exists right after rollout even if no
    request has arrived yet; the lazy ensure path remains the fallback."""
    import time

    import tokens

    for attempt in range(5):
        try:
            users_store.ensure_table()
            tokens.ensure_table()
            logger.info(
                "startup schema init complete (api_tokens, token_audit, service token)"
            )
            return
        except Exception as e:
            logger.warning("startup schema init attempt %d failed: %s", attempt + 1, e)
            time.sleep(5)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        global _startup_init_started
        if not _startup_init_started:
            _startup_init_started = True
            asyncio.get_running_loop().run_in_executor(None, _startup_schema_init)
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
                    new_headers.append(
                        (b"accept", b"application/json, text/event-stream")
                    )
                    found_accept = True
                else:
                    new_headers.append(h)
            if not found_accept:
                new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope["headers"] = new_headers

    # Health — no auth
    if path == "/health":
        g = mem_store.embedding_guard()
        await _send_response(
            send,
            *_json_response(
                {
                    "status": "ok" if g["ok"] else "degraded",
                    "service": "claude-memory",
                    "embed_provider": settings.embed_provider,
                    "llm_provider": settings.llm_provider,
                    "embedding_guard": g,
                }
            ),
        )
        return

    # Prometheus metrics — no auth
    if path == "/metrics" and method == "GET":
        try:
            all_memories = await _get_all_cached()
        except Exception:
            logger.exception("metrics: get_all failed")
            await _send_response(
                send, 503, [[b"content-type", b"text/plain"]], b"db unavailable"
            )
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
            for tag in meta.get("tags") or []:
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
        await _send_response(
            send, 200, [[b"content-type", b"text/plain; version=0.0.4"]], body
        )
        return

    # All /api/* routes require bearer auth (API token, Keycloak JWT, admin or
    # legacy token — see auth.resolve). The principal scopes every handler.
    if path.startswith("/api/"):
        principal = auth.principal_from_asgi_headers(headers)
        if principal is None:
            # Count/audit ONLY requests that presented a credential that
            # failed validation. Unauthenticated page loads (no Authorization
            # header) are the pre-login UI, not an attack, and must never
            # consume the budget or write audit rows.
            presented = bool(headers.get(b"authorization", b"").strip())
            ip = _client_ip(headers)
            if presented and ip:  # ip=='' -> untrusted, skip limiting entirely
                if _auth_fail_limited(ip):
                    await _send_response(
                        send,
                        429,
                        [[b"content-type", b"text/plain"]],
                        b"Too many failed auth attempts",
                    )
                    return
                import tokens as _tok

                _tok.audit("auth_fail", ip=ip, detail=path[:100])
            await _send_response(
                send, 401, [[b"content-type", b"text/plain"]], b"Unauthorized"
            )
            return
        auth.set_principal(principal)

        agent_id = _get_agent_id(headers)
        qs = _parse_qs(scope.get("query_string", b"").decode())

        # ---- Auth / users ---------------------------------------------------

        # GET /api/auth/whoami — who does this token belong to?
        if path == "/api/auth/whoami" and method == "GET":
            hdrs: list = [[b"content-type", b"application/json"]]
            payload = {
                "user_id": principal.user_id,
                "role": principal.role,
                "via": principal.via,
                "label": principal.label,
                "shared_tenant": settings.shared_tenant_id,
            }
            if principal.via == "token" and principal.token_name:
                payload["token_name"] = principal.token_name
                payload["token_expires_in_days"] = principal.token_expires_in_days
                if 0 <= principal.token_expires_in_days < 14:
                    # Expiry warning for clients that poll whoami (hooks, UI).
                    payload["token_expiring_soon"] = True
                    hdrs.append(
                        [
                            b"x-token-expires-in-days",
                            str(principal.token_expires_in_days).encode(),
                        ]
                    )
            await _send_response(send, 200, hdrs, json.dumps(payload).encode())
            return

        # GET /api/version — build metadata for the UI footer. Auth like every
        # other /api route: no anonymous fingerprinting.
        if path == "/api/version" and method == "GET":
            import version as ver_mod

            await _send_response(send, *_json_response(ver_mod.info()))
            return

        # Personal API tokens — self-service for any caller...
        if path == "/api/me/tokens" or path.startswith("/api/me/tokens/"):
            if await _tokens_api(
                path, method, send, receive, qs, principal, admin_view=False
            ):
                return

        # ...and the admin view (masked listing, revoke any; never mint for others).
        if path.startswith("/api/admin/tokens"):
            if not principal.is_admin:
                await _forbidden(send)
                return
            if await _tokens_api(
                path, method, send, receive, qs, principal, admin_view=True
            ):
                return

        if path.startswith("/api/users"):
            if not principal.is_admin:
                await _forbidden(send)
                return
            handled = await _users_api(path, method, send, receive, qs)
            if handled:
                return

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
                    qs.get("harness", "").strip()
                    or principal.default_harness
                    or agent_id,
                    qs.get("tool", "").strip(),
                    principal,
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
                principal.user_id,
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
                await _send_response(
                    send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                )
                return
            if not session_id or not notes:
                await _send_response(
                    send,
                    *_json_response(
                        {"ok": False, "error": "session_id and notes required"},
                        status=400,
                    ),
                )
                return
            notes = _truncate(notes, settings.auto_notes_max_chars)
            row = await _to_thread(
                mem_store.put_auto_notes,
                session_id,
                notes,
                str(body_data.get("project", "")).strip(),
                str(body_data.get("harness", "")).strip()
                or principal.default_harness
                or agent_id,
                principal.user_id,
            )
            if row is None:
                await _send_response(
                    send,
                    *_json_response(
                        {"ok": False, "error": "session belongs to another workspace"},
                        status=404,
                    ),
                )
                return
            await _send_response(send, *_json_response({"ok": True}))
            return

        # ---- Session checkpoints -----------------------------------------

        # GET /api/sessions?project=&harness=&limit=  (admin: ?scope=all|?tenant=)
        if path == "/api/sessions" and method == "GET":
            mode = _admin_target(qs, principal)
            owner = (
                None
                if mode[0] == "all"
                else (mode[1] if mode[0] == "tenant" else principal.user_id)
            )
            rows = await _to_thread(
                mem_store.list_checkpoints,
                qs.get("project", "").strip(),
                "",
                _int(qs.get("limit"), 20),
                qs.get("harness", "").strip(),
                owner,
            )
            await _send_response(send, *_json_response(rows))
            return

        if path.startswith("/api/sessions/"):
            session_id = urllib.parse.unquote(path[len("/api/sessions/") :]).strip()
            if not session_id:
                await _send_response(
                    send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                )
                return

            # GET /api/sessions/{id}
            if method == "GET":
                mode = _admin_target(qs, principal)
                # Reads may cross tenants only for admins ("all"); ?tenant=
                # restricts to that one workspace.
                owner = (
                    None
                    if mode[0] == "all"
                    else (mode[1] if mode[0] == "tenant" else principal.user_id)
                )
                row = await _to_thread(mem_store.get_checkpoint, session_id, owner)
                if row is None:
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                await _send_response(send, *_json_response(row))
                return

            # PUT /api/sessions/{id} {state, project?, harness?}
            if method == "PUT":
                try:
                    body_data = json.loads(await _read_body(receive))
                    state = str(body_data.get("state", "")).strip()
                except Exception:
                    await _send_response(
                        send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                    )
                    return
                if not state:
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "state required"}, status=400
                        ),
                    )
                    return
                if len(state) > settings.checkpoint_max_chars:
                    await _send_response(
                        send,
                        *_json_response(
                            {
                                "ok": False,
                                "error": f"state too long ({len(state)} chars, max {settings.checkpoint_max_chars}); condense it",
                            },
                            status=413,
                        ),
                    )
                    return
                mode = _admin_target(qs, principal)
                # Writes always land in the caller's workspace unless an admin
                # names one explicitly with ?tenant=.
                owner = mode[1] if mode[0] == "tenant" else principal.user_id
                row = await _to_thread(
                    mem_store.put_checkpoint,
                    session_id,
                    state,
                    str(body_data.get("project", "")).strip(),
                    str(body_data.get("harness", "")).strip()
                    or principal.default_harness
                    or agent_id,
                    owner,
                )
                if row is None:
                    await _send_response(
                        send,
                        *_json_response(
                            {
                                "ok": False,
                                "error": "session belongs to another workspace",
                            },
                            status=404,
                        ),
                    )
                    return
                await _send_response(send, *_json_response({"ok": True, **row}))
                return

            # DELETE /api/sessions/{id}
            if method == "DELETE":
                mode = _admin_target(qs, principal)
                owner = (
                    None
                    if mode[0] == "all"
                    else (mode[1] if mode[0] == "tenant" else principal.user_id)
                )
                deleted = await _to_thread(
                    mem_store.delete_checkpoint, session_id, owner
                )
                if not deleted:
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                await _send_response(
                    send, *_json_response({"ok": True, "session_id": session_id})
                )
                return

        # GET /api/stats  (admin: ?scope=all|?tenant=)
        if path == "/api/stats" and method == "GET":
            if _space_param(qs) is None:
                await _bad_space(send)
                return
            all_memories = await _scoped_memories(qs, principal)
            total = len(all_memories)
            dates = [
                r.get("created_at", "") for r in all_memories if r.get("created_at")
            ]
            dates_sorted = sorted(dates)
            agents: dict = {}
            categories: dict = {}
            tags_count: dict = {}
            # Distinct memories carrying at least one tag - NOT the sum of
            # tag occurrences (that made the UI's 'untagged' go negative when
            # memories carried several tags each).
            tagged = 0
            for r in all_memories:
                a = r.get("agent_id") or "unknown"
                agents[a] = agents.get(a, 0) + 1
                meta = _extract_meta(r)
                c = meta.get("category", "") or "uncategorized"
                categories[c] = categories.get(c, 0) + 1
                tags = meta.get("tags") or []
                if tags:
                    tagged += 1
                for tag in tags:
                    tags_count[tag] = tags_count.get(tag, 0) + 1
            await _send_response(
                send,
                *_json_response(
                    {
                        "total": total,
                        "first_added": dates_sorted[0] if dates_sorted else None,
                        "last_added": dates_sorted[-1] if dates_sorted else None,
                        "by_agent": agents,
                        "by_category": categories,
                        "by_tag": tags_count,
                        "tagged": tagged,
                    }
                ),
            )
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

        # POST /api/tag-rules (admin — rules apply to every tenant)
        if path == "/api/tag-rules" and method == "POST":
            if not principal.is_admin:
                await _forbidden(send)
                return
            raw = await _read_body(receive)
            try:
                body_data = json.loads(raw)
                tag = body_data.get("tag", "").strip()
                keyword = body_data.get("keyword", "").strip()
            except Exception:
                await _send_response(
                    send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                )
                return
            if not tag or not keyword:
                await _send_response(
                    send,
                    400,
                    [[b"content-type", b"text/plain"]],
                    b"tag and keyword required",
                )
                return
            ok = await _to_thread(mem_store.add_tag_rule, tag, keyword)
            await _send_response(send, *_json_response({"ok": ok}))
            return

        # DELETE /api/tag-rules/{tag}/{keyword} (admin)
        if path.startswith("/api/tag-rules/") and method == "DELETE":
            if not principal.is_admin:
                await _forbidden(send)
                return
            rest = path[len("/api/tag-rules/") :]
            if "/" in rest:
                tag_part, kw_part = rest.split("/", 1)
                ok = await _to_thread(
                    mem_store.delete_tag_rule,
                    urllib.parse.unquote(tag_part),
                    urllib.parse.unquote(kw_part),
                )
                await _send_response(send, *_json_response({"ok": ok}))
                return

        # GET /api/jobs (admin — the per-phase reports quote memory text from
        # every tenant, so they are not shown to regular users)
        if path == "/api/jobs" and method == "GET":
            if not principal.is_admin:
                await _forbidden(send)
                return
            jobs = await _to_thread(mem_store.list_job_runs)
            await _send_response(send, *_json_response(jobs))
            return

        # GET /api/jobs/{id} (admin)
        if path.startswith("/api/jobs/") and method == "GET":
            if not principal.is_admin:
                await _forbidden(send)
                return
            run_id = path[len("/api/jobs/") :]
            job = await _to_thread(mem_store.get_job_run, run_id)
            if job is None:
                await _send_response(
                    send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                )
                return
            await _send_response(send, *_json_response(job))
            return

        # GET /api/memories/export — download memories as JSON
        # (own workspace + shared; admins may pass ?scope=all or ?tenant=)
        if path == "/api/memories/export" and method == "GET":
            if _space_param(qs) is None:
                await _bad_space(send)
                return
            all_memories = await _scoped_memories(qs, principal)
            body = json.dumps(
                [_memory_dict(r) for r in all_memories], default=str, indent=2
            ).encode()
            await _send_response(
                send,
                200,
                [
                    [b"content-type", b"application/json"],
                    [b"content-disposition", b'attachment; filename="memories.json"'],
                ],
                body,
            )
            return

        # POST /api/memories/import — bulk import from JSON array
        if path == "/api/memories/import" and method == "POST":
            raw = await _read_body(receive)
            try:
                items = json.loads(raw)
                if not isinstance(items, list):
                    raise ValueError("expected JSON array")
            except Exception as e:
                await _send_response(
                    send, 400, [[b"content-type", b"text/plain"]], str(e).encode()
                )
                return
            imported = 0
            try:
                for item in items:
                    content = (
                        item.get("memory")
                        or item.get("text")
                        or item.get("content", "")
                    )
                    src_agent = item.get("agent_id", "") or agent_id
                    if content.strip():
                        await enqueue(
                            content.strip(),
                            src_agent,
                            True,
                            "",
                            [],
                            user_id=principal.user_id,
                        )
                        imported += 1
            except QueueFull as e:
                await _send_response(
                    send,
                    *_json_response(
                        {"imported": imported, "queued": True, "error": str(e)},
                        status=503,
                    ),
                )
                return
            await _send_response(
                send, *_json_response({"imported": imported, "queued": True})
            )
            return

        # GET /api/memories — list / search (?ids=a,b fetches visible records
        # by id; ?format=index returns token-cheap rows for the two-step search)
        if path == "/api/memories" and method == "GET":
            if _space_param(qs) is None:
                await _bad_space(send)
                return
            ids_param = qs.get("ids", "").strip()
            if ids_param:
                recs = await _to_thread(
                    mem_store.get_by_ids,
                    [i.strip() for i in ids_param.split(",") if i.strip()],
                    principal.user_id,
                )
                await _send_response(
                    send, *_json_response([_memory_dict(r) for r in recs])
                )
                return
            q = qs.get("q", "").strip()
            sort = qs.get("sort", "newest")  # newest | oldest | relevance
            filter_agent = qs.get("agent_id", "").strip()
            filter_category = qs.get("category", "").strip()
            filter_tag = qs.get("tag", "").strip().lower()

            if q:
                results = await _scoped_search(q, 200, qs, principal)
                if sort in ("newest", "oldest"):
                    reverse = sort == "newest"
                    results.sort(key=lambda r: r.get("created_at", ""), reverse=reverse)
            else:
                results = await _scoped_memories(qs, principal, agent_id=filter_agent)
                reverse = sort != "oldest"
                results.sort(key=lambda r: r.get("created_at", ""), reverse=reverse)

            if filter_category:
                results = [
                    r
                    for r in results
                    if _extract_meta(r).get("category", "") == filter_category
                ]
            if filter_tag:
                results = [
                    r
                    for r in results
                    if filter_tag in (_extract_meta(r).get("tags") or [])
                ]

            if qs.get("format", "").strip() == "index":
                await _send_response(
                    send, *_json_response([_index_row(r) for r in results])
                )
                return
            await _send_response(
                send, *_json_response([_memory_dict(r) for r in results])
            )
            return

        # GET /api/memories/timeline/{id}?window=3 — chronological neighbours
        if path.startswith("/api/memories/timeline/") and method == "GET":
            tl_id = path[len("/api/memories/timeline/") :].strip("/")
            window = _int(qs.get("window"), 3)
            tl = await _to_thread(mem_store.timeline, tl_id, window, principal.user_id)
            if tl is None:
                await _send_response(
                    send, *_json_response({"error": "not found"}, status=404)
                )
                return
            await _send_response(
                send,
                *_json_response(
                    {
                        "before": [_memory_dict(r) for r in tl["before"]],
                        "self": _memory_dict(tl["self"]),
                        "after": [_memory_dict(r) for r in tl["after"]],
                    }
                ),
            )
            return

        # POST /api/memories — add a memory. {"shared": true} (admin only)
        # publishes into the shared space every tenant can read.
        if path == "/api/memories" and method == "POST":
            blocked = _write_guard_block()
            if blocked:
                await _send_response(send, *blocked)
                return
            raw = await _read_body(receive)
            shared = False
            # Support both form-encoded and JSON body
            try:
                body_data = json.loads(raw)
                content = body_data.get("content", "").strip()
                src_agent = body_data.get("agent_id", "") or agent_id
                shared = bool(body_data.get("shared", False))
            except Exception:
                params = dict(
                    p.split("=", 1) for p in raw.decode().split("&") if "=" in p
                )
                content = urllib.parse.unquote_plus(params.get("content", "")).strip()
                src_agent = agent_id
            target = principal.user_id
            if shared:
                if not principal.is_admin:
                    await _forbidden(send)
                    return
                target = settings.shared_tenant_id
            if content:
                try:
                    await enqueue(content, src_agent, True, "", [], user_id=target)
                except QueueFull as e:
                    await _send_response(
                        send,
                        *_json_response({"ok": False, "error": str(e)}, status=503),
                    )
                    return
            await _send_response(
                send, *_json_response({"ok": True, "queued": bool(content)})
            )
            return

        # Routes with memory ID suffix — all ownership-checked: another
        # tenant's ids behave exactly like missing ones (404).
        # /api/share-proposals — admin worklist for user proposals (bean
        # oi38). Non-admins get 403 before anything is read.
        if path == "/api/share-proposals" or path.startswith("/api/share-proposals/"):
            if not principal.is_admin:
                await _forbidden(send)
                return
            if path == "/api/share-proposals" and method == "GET":
                status = (qs.get("status", "proposed") or "proposed").strip().lower()
                if status not in mem_store.PROPOSAL_STATUSES:
                    await _send_response(
                        send,
                        *_json_response(
                            {
                                "ok": False,
                                "error": "status must be one of: proposed, rejected, all",
                            },
                            status=400,
                        ),
                    )
                    return
                rows = await _to_thread(mem_store.list_proposals, status)
                await _send_response(send, *_json_response(rows))
                return
            sub = path[len("/api/share-proposals/") :]
            pid, _, action = sub.partition("/")
            if method == "POST" and action == "approve":
                res = await run_write(
                    mem_store.approve_proposal, pid, principal.user_id
                )
                if res == "not_found":
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if res == "changed":
                    await _send_response(
                        send,
                        *_json_response(
                            {
                                "ok": False,
                                "error": "content changed since proposal - ask the owner to re-propose",
                            },
                            status=409,
                        ),
                    )
                    return
                if res == "not_proposed":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "no open proposal on this memory"},
                            status=409,
                        ),
                    )
                    return
                if res == "error":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "store unreachable"}, status=503
                        ),
                    )
                    return
                await _send_response(
                    send,
                    *_json_response(
                        {
                            "ok": True,
                            "shared": res == "moved",
                            "already_shared": res == "already",
                        }
                    ),
                )
                return
            if method == "POST" and action == "reject":
                try:
                    body = json.loads(await _read_body(receive) or b"{}")
                    reason = str(body.get("reason", ""))[:300]
                except Exception:
                    await _send_response(
                        send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                    )
                    return
                res = await run_write(
                    mem_store.reject_proposal, pid, principal.user_id, reason
                )
                if res == "not_found":
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if res == "not_proposed":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "no open proposal on this memory"},
                            status=409,
                        ),
                    )
                    return
                if res == "error":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "store unreachable"}, status=503
                        ),
                    )
                    return
                await _send_response(
                    send, *_json_response({"ok": True, "rejected": True})
                )
                return
            await _send_response(
                send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
            )
            return

        if path.startswith("/api/memories/"):
            memory_id = path[len("/api/memories/") :]

            # PUT /api/memories/{id} — edit content
            if method == "PUT":
                blocked = _write_guard_block()
                if blocked:
                    await _send_response(send, *blocked)
                    return
                raw = await _read_body(receive)
                try:
                    body_data = json.loads(raw)
                    content = body_data.get(
                        "memory", body_data.get("content", "")
                    ).strip()
                except Exception:
                    params = dict(
                        p.split("=", 1) for p in raw.decode().split("&") if "=" in p
                    )
                    content = urllib.parse.unquote_plus(
                        params.get("content", "")
                    ).strip()
                if not await _may_modify_api(memory_id, principal):
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if content:
                    await run_write(mem_store.update, memory_id, content)
                await _send_response(send, *_json_response({"ok": True}))
                return

            # POST /api/memories/{id}/propose-share — owner marks their own
            # memory as a proposal for the shared space (bean oi38). It
            # stays in their tenant and invisible to others until an admin
            # approves; foreign ids behave exactly like missing ones.
            if method == "POST" and memory_id.endswith("/propose-share"):
                res = await run_write(
                    mem_store.propose_share,
                    memory_id[: -len("/propose-share")],
                    principal.user_id,
                )
                if res == "not_found":
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if res == "limit":
                    await _send_response(
                        send,
                        *_json_response(
                            {
                                "ok": False,
                                "error": f"too many open proposals (max {mem_store.MAX_OPEN_PROPOSALS})",
                            },
                            status=429,
                        ),
                    )
                    return
                if res == "error":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "store unreachable"}, status=503
                        ),
                    )
                    return
                await _send_response(
                    send,
                    *_json_response(
                        {
                            "ok": True,
                            "proposed": res == "proposed",
                            "already_proposed": res == "already",
                        }
                    ),
                )
                return

            # POST /api/memories/{id}/withdraw-share — owner pulls an open
            # proposal back (bean oi38).
            if method == "POST" and memory_id.endswith("/withdraw-share"):
                res = await run_write(
                    mem_store.withdraw_share,
                    memory_id[: -len("/withdraw-share")],
                    principal.user_id,
                )
                if res == "not_found":
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if res == "not_proposed":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "no open proposal on this memory"},
                            status=409,
                        ),
                    )
                    return
                if res == "error":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "store unreachable"}, status=503
                        ),
                    )
                    return
                await _send_response(
                    send, *_json_response({"ok": True, "withdrawn": True})
                )
                return

            # PATCH /api/memories/{id}/share — admin-only MOVE into the
            # shared space (bean vd3y): id/created_at/embeddings are kept,
            # provenance (shared_by/shared_at/original_tenant) lands in the
            # payload. Auth is checked before any existence lookup, and
            # non-admins get the same 404 shape as for any id they cannot
            # see — no existence leak either way.
            if method == "PATCH" and memory_id.endswith("/share"):
                if not principal.is_admin:
                    await _forbidden(send)
                    return
                real_id = memory_id[: -len("/share")]
                res = await run_write(
                    mem_store.share_to_space, real_id, principal.user_id
                )
                if res == "not_found":
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                if res == "error":
                    await _send_response(
                        send,
                        *_json_response(
                            {"ok": False, "error": "store unreachable"}, status=503
                        ),
                    )
                    return
                await _send_response(
                    send,
                    *_json_response(
                        {
                            "ok": True,
                            "shared": res == "moved",
                            "already_shared": res == "already",
                        }
                    ),
                )
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
                    await _send_response(
                        send, 400, [[b"content-type", b"text/plain"]], b"Bad Request"
                    )
                    return
                ok = await _to_thread(
                    mem_store.update_tags,
                    real_id,
                    tags,
                    principal.user_id,
                    principal.is_admin,
                )
                await _send_response(send, *_json_response({"ok": ok}))
                return

            # DELETE /api/memories/{id}
            if method == "DELETE":
                if not await _may_modify_api(memory_id, principal):
                    await _send_response(
                        send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
                    )
                    return
                await run_write(mem_store.delete, memory_id)
                await _send_response(send, *_json_response({"ok": True}))
                return

        await _send_response(
            send, 404, [[b"content-type", b"text/plain"]], b"Not Found"
        )
        return

    # /mcp — MCP protocol (bearer auth required). The principal resolved here
    # is carried through the handler via a contextvar, so every tool call —
    # including those handed to worker threads — runs in the caller's tenant.
    mcp_principal = auth.principal_from_asgi_headers(headers)
    if mcp_principal is None:
        await _send_response(
            send, 401, [[b"content-type", b"text/plain"]], b"Unauthorized"
        )
        return
    auth.set_principal(mcp_principal)

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
            sse_event = f"data: {json.dumps({'jsonrpc': '2.0', 'id': payload.get('id'), 'result': result})}\n\n"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"text/event-stream"],
                        [b"cache-control", b"no-cache, no-transform"],
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": sse_event.encode("utf-8")}
            )
            return

        if method == "tools/list":
            await _mcp_handler_strip_output_schema(
                scope, _make_receive(raw, receive), send
            )
            return
    except Exception:
        pass
    await _mcp_handler(scope, _make_receive(raw, receive), send)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
