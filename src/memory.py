import json
import logging
import pathlib
import threading
import time

import psycopg2
import psycopg2.extras
from mem0 import Memory
from mem0.configs.base import EmbedderConfig, LlmConfig, MemoryConfig, VectorStoreConfig

from config import settings

logger = logging.getLogger(__name__)

_EMBEDDING_DIMS = 768  # nomic-embed-text via Ollama

# Seed rules — used to initialise tag_rules table on first deploy; runtime rules come from DB
_TAG_RULES_SEED: dict[str, list[str]] = {
    "prod-k8s": ["prod-k8s", "prod.k8s"],
    "prod-mgmt": ["prod-mgmt"],
    "dev-k8s": ["dev-k8s"],
    "remnant": ["remnant", "claude-memory"],
    "n8n": ["n8n"],
    "frigate": ["frigate"],
    "nextcloud": ["nextcloud"],
    "ghost": ["ghost blog", "ghost cms"],
    "matrix": ["matrix", "synapse", "element"],
    "homeassistant": ["home assistant", "homeassistant", "ha-mcp", "hass"],
    "minecraft": ["minecraft"],
    "valheim": ["valheim"],
    "firefly": ["firefly"],
    "zigbee": ["zigbee", "zigbee2mqtt", "z2m"],
    "velero": ["velero"],
    "argocd": ["argocd", "argo cd"],
    "keycloak": ["keycloak"],
    "backup-operator": ["backup-operator", "backupoperator"],
    "cnpg": ["cnpg", "cloudnativepg", "postgresql", "postgres"],
    "cilium": ["cilium"],
    "cert-manager": ["cert-manager", "certmanager"],
    "capi": ["capi", "cluster api", "clusterapi"],
    "upgrade": ["upgrade", "upgrading", "k8s upgrade", "kubernetes upgrade"],
    "backup": ["backup", "velero", "restore"],
    "etcd": ["etcd"],
    "bgp": ["bgp", "bird", "bgppeers"],
    "networking": ["networking", "network policy", "loadbalancer"],
    "monitoring": [
        "prometheus",
        "grafana",
        "alertmanager",
        "monitoring",
        "servicemonitor",
    ],
    "helm": ["helm", "helmchart", "helm chart", "helmrelease"],
    "storage": ["storage", "pvc", "persistentvolume", "nfs", "longhorn", "rook"],
    "auth": ["auth", "keycloak", "oauth", "oidc", "sso"],
    "opnsense": ["opnsense", "firewall"],
    "hpe": ["hpe", "nimble", "simplivity"],
    "worklog": ["worklog", "work log"],
    "cycletrack": ["cycletrack", "cycle track"],
    "gameledger": ["gameledger", "game ledger"],
    "castlist": ["castlist", "cast list"],
    "burst-power": ["burst-power", "burst power"],
    "version-checker": ["version-checker", "versionchecker"],
    "ansible": ["ansible"],
    "blog": ["blog", "ghost"],
}

# ---------------------------------------------------------------------------
# Tag rules — DB-backed with in-memory cache
# ---------------------------------------------------------------------------

_cache: dict = {"rules": {}, "time": 0.0}
_cache_lock = threading.Lock()
_RULES_TTL = 300.0  # refresh every 5 minutes


def _db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(settings.database_url)


def _ensure_schema() -> None:
    """Create tag_rules and job_runs tables; seed rules if empty. Idempotent."""
    try:
        conn = _db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS tag_rules (
                            tag     TEXT NOT NULL,
                            keyword TEXT NOT NULL,
                            source  TEXT NOT NULL DEFAULT 'manual',
                            PRIMARY KEY (tag, keyword)
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS job_runs (
                            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                            finished_at      TIMESTAMPTZ,
                            phase            TEXT NOT NULL,
                            status           TEXT NOT NULL DEFAULT 'running',
                            memories_scanned INT DEFAULT 0,
                            memories_changed INT DEFAULT 0,
                            rules_added      INT DEFAULT 0,
                            duration_seconds FLOAT,
                            error            TEXT
                        )
                    """)
                    cur.execute(
                        "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS details TEXT"
                    )
                    cur.execute("SELECT COUNT(*) FROM tag_rules")
                    if cur.fetchone()[0] == 0:  # type: ignore[index]  # COUNT(*) always returns exactly one row
                        for tag, keywords in _TAG_RULES_SEED.items():
                            for kw in keywords:
                                cur.execute(
                                    "INSERT INTO tag_rules (tag, keyword, source) "
                                    "VALUES (%s, %s, 'seed') ON CONFLICT DO NOTHING",
                                    (tag, kw),
                                )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("_ensure_schema failed (non-fatal): %s", e)


def _load_rules_from_db() -> dict[str, list[str]]:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tag, keyword FROM tag_rules ORDER BY tag, keyword")
            rows = cur.fetchall()
        rules: dict[str, list[str]] = {}
        for tag, kw in rows:
            rules.setdefault(tag, []).append(kw)
        return rules
    finally:
        conn.close()


def _get_rules() -> dict[str, list[str]]:
    now = time.monotonic()
    with _cache_lock:
        if _cache["rules"] and (now - _cache["time"]) < _RULES_TTL:
            return _cache["rules"]
    try:
        rules = _load_rules_from_db()
    except Exception:
        return _TAG_RULES_SEED  # fallback when DB unreachable
    with _cache_lock:
        _cache["rules"] = rules
        _cache["time"] = now
    return rules


def _invalidate_rules_cache() -> None:
    with _cache_lock:
        _cache["time"] = 0.0


def _auto_tag(text: str) -> list[str]:
    text_lower = text.lower()
    return sorted(
        tag
        for tag, keywords in _get_rules().items()
        if any(k in text_lower for k in keywords)
    )


def list_tag_rules() -> list[dict]:
    """Return all rules as list of {tag, keyword, source}."""
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT tag, keyword, source FROM tag_rules ORDER BY tag, keyword"
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_tag_rule(tag: str, keyword: str, source: str = "manual") -> bool:
    tag = tag.strip().lower()
    keyword = keyword.strip().lower()
    if not tag or not keyword:
        return False
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tag_rules (tag, keyword, source) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (tag, keyword, source),
            )
        _invalidate_rules_cache()
        return True
    finally:
        conn.close()


def delete_tag_rule(tag: str, keyword: str) -> bool:
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tag_rules WHERE tag = %s AND keyword = %s",
                (tag, keyword),
            )
            deleted = cur.rowcount > 0
        _invalidate_rules_cache()
        return deleted
    finally:
        conn.close()


def list_job_runs(limit: int = 50) -> list[dict]:
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT %s",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def get_job_run(run_id: str) -> dict | None:
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM job_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# mem0 client
# ---------------------------------------------------------------------------


def _api_key(inline: str, file: str) -> str:
    """Key from a mounted file (K8s secret) wins over the inline env value.
    Never log the result."""
    if file:
        try:
            return pathlib.Path(file).read_text().strip()
        except OSError:
            return ""
    return inline


def _llm_config() -> LlmConfig:
    if settings.llm_provider == "openai":
        cfg = {
            "model": settings.llm_model,
            "api_key": _api_key(settings.llm_api_key, settings.llm_api_key_file),
        }
        if settings.llm_base_url:
            cfg["openai_base_url"] = settings.llm_base_url
        return LlmConfig(provider="openai", config=cfg)
    return LlmConfig(
        provider="ollama",
        config={
            "model": settings.ollama_model,
            "ollama_base_url": settings.ollama_base_url,
        },
    )


def _embedder_config() -> EmbedderConfig:
    if settings.embed_provider == "openai":
        cfg = {
            "model": settings.embed_model,
            "api_key": _api_key(settings.embed_api_key, settings.embed_api_key_file),
        }
        if settings.embed_base_url:
            cfg["openai_base_url"] = settings.embed_base_url
        # embedding_dims deliberately NOT set: non-matryoshka backends
        # (vLLM, TEI, ...) reject the `dimensions` request parameter. The
        # dimension guard in get_memory() verifies the width against the
        # pgvector column instead.
        return EmbedderConfig(provider="openai", config=cfg)
    return EmbedderConfig(
        provider="ollama",
        config={
            "model": settings.ollama_embed_model,
            "ollama_base_url": settings.ollama_base_url,
            "embedding_dims": _EMBEDDING_DIMS,
        },
    )


def _provider_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


# Dimension guard: the pgvector column is fixed-width. An embedder that
# returns another width would corrupt writes, so we probe once at startup
# and refuse writes until it is fixed (re-embed path: export, switch model,
# re-embed - see README).
_dim_guard: dict = {"ok": True, "msg": ""}


def embedding_guard() -> dict:
    return dict(_dim_guard)


def _guard_for_writes() -> None:
    if not _dim_guard["ok"]:
        raise RuntimeError(f"writes disabled: {_dim_guard['msg']}")


def _build_config() -> MemoryConfig:
    return MemoryConfig(
        custom_instructions=settings.mem0_custom_instructions or None,
        vector_store=VectorStoreConfig(
            provider="pgvector",
            config={
                "connection_string": settings.database_url,
                "collection_name": "mem0",
                "embedding_model_dims": _EMBEDDING_DIMS,
                "hnsw": True,
            },
        ),
        llm=_llm_config(),
        embedder=_embedder_config(),
    )


_mem: Memory | None = None
_mem_lock = threading.Lock()


def get_memory() -> Memory:
    # Called from worker threads now — guard against double initialisation.
    global _mem
    if _mem is None:
        with _mem_lock:
            if _mem is None:
                m = Memory(config=_build_config())
                _apply_ollama_timeout(m)
                logger.info(
                    "providers: embed=%s (%s / %s) llm=%s (%s / %s)",
                    settings.embed_provider,
                    _provider_host(settings.embed_base_url or settings.ollama_base_url),
                    settings.embed_model or settings.ollama_embed_model,
                    settings.llm_provider,
                    _provider_host(settings.llm_base_url or settings.ollama_base_url),
                    settings.llm_model or settings.ollama_model,
                )
                if settings.embed_provider == "openai":
                    _probe_dims(m)
                _mem = m
    return _mem


def _probe_dims(m: Memory) -> None:
    """Embed one probe text and compare against the pgvector column width."""
    global _dim_guard
    try:
        n = len(m.embedding_model.embed("dimension probe", memory_action="add"))
    except Exception as exc:  # noqa: BLE001 - any probe failure blocks writes
        _dim_guard = {
            "ok": False,
            "msg": (
                f"embedder probe failed ({type(exc).__name__}); check "
                "EMBED_BASE_URL/EMBED_MODEL/EMBED_API_KEY"
            ),
        }
        logger.error("dimension guard: %s", _dim_guard["msg"])
        return
    if n != _EMBEDDING_DIMS:
        _dim_guard = {
            "ok": False,
            "msg": (
                f"embed model {settings.embed_model} returns {n} dims, the "
                f"pgvector column is {_EMBEDDING_DIMS}; switch the model or "
                "re-embed the store (export -> fresh collection -> re-add)"
            ),
        }
        logger.error("dimension guard: %s", _dim_guard["msg"])


def _apply_ollama_timeout(m: Memory) -> None:
    """mem0 builds its Ollama clients with no timeout, so a hung/overloaded
    Ollama blocked the writer thread forever (and every delete/update queued
    behind it). Swap in clients with a bounded timeout."""
    try:
        from ollama import Client

        for component in (getattr(m, "llm", None), getattr(m, "embedding_model", None)):
            if component is not None and isinstance(
                getattr(component, "client", None), Client
            ):
                component.client = Client(
                    host=settings.ollama_base_url, timeout=settings.ollama_timeout
                )
    except Exception as e:
        logger.warning("could not set Ollama timeout (non-fatal): %s", e)


def _agent_id(agent_id: str) -> str:
    return agent_id if agent_id else settings.mem0_agent_id


VALID_CATEGORIES = {
    "project",
    "feedback",
    "reference",
    "user",
    "session",
    "incident",
    "",
}


def _tenant(user_id: str | None) -> str:
    """The tenant (mem0 user_id namespace) an operation runs in."""
    return user_id or settings.mem0_user_id


def _results(result) -> list[dict]:
    if isinstance(result, dict) and "results" in result:
        return result["results"]
    return result if isinstance(result, list) else []


def add(
    content: str,
    user_id: str | None = None,
    agent_id: str = "",
    infer: bool | None = None,
    category: str = "",
    tags: list | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    _guard_for_writes()
    m = get_memory()
    metadata: dict = dict(extra_metadata or {})
    if category:
        metadata["category"] = category
    auto = _auto_tag(content)
    explicit = [t for t in (tags or []) if isinstance(t, str) and t.strip()]
    merged = sorted(set(auto) | set(explicit))
    if merged:
        metadata["tags"] = merged
    do_infer = settings.mem0_infer if infer is None else infer
    kwargs = dict(
        user_id=_tenant(user_id),
        agent_id=_agent_id(agent_id),
        metadata=metadata if metadata else None,
    )
    result = m.add(content, infer=do_infer, **kwargs)
    if do_infer and settings.add_raw_fallback and not (result or {}).get("results"):
        # The LLM extracted no facts (or judged the note a duplicate). The agent
        # asked to keep it, so store it as written rather than dropping it.
        result = m.add(content, infer=False, **kwargs)
        result = {**(result or {}), "fallback": "raw"}
    return result


def _search_one(m: Memory, query: str, tenant: str, top_k: int) -> list[dict]:
    return _results(m.search(query, filters={"user_id": tenant}, top_k=top_k))


def search(
    query: str,
    limit: int = 5,
    agent_id: str = "",
    user_id: str | None = None,
    include_shared: bool = True,
) -> list[dict]:
    """Semantic search inside one tenant, merged with the shared space.

    Two queries instead of one `user_id IN (...)` filter: mem0's filter
    support for list values varies by version/provider, and merging two
    result lists is deterministic across all of them."""
    m = get_memory()
    tenant = _tenant(user_id)
    tenants = [tenant]
    if include_shared and tenant != settings.shared_tenant_id:
        tenants.append(settings.shared_tenant_id)
    merged: dict[str, dict] = {}
    for t in tenants:
        for r in _search_one(m, query, t, limit):
            rid = str(r.get("id"))
            prev = merged.get(rid)
            if prev is None or (r.get("score") or 0) > (prev.get("score") or 0):
                merged[rid] = r
    out = sorted(merged.values(), key=lambda r: r.get("score") or 0, reverse=True)
    if agent_id:
        out = [r for r in out if r.get("agent_id") == agent_id]
    return out[:limit]


def search_across(
    query: str, limit: int, tenants: list[str] | None = None
) -> list[dict]:
    """Admin view: search every tenant, best scores first.

    Prefers a single unfiltered search (covers namespaces that exist in the
    store without a users row); falls back to merging per-tenant searches."""
    m = get_memory()
    try:
        out = _results(m.search(query, filters={}, top_k=limit))
        return out[:limit]
    except Exception:
        pass
    if tenants is None:
        import users as users_store

        tenants = users_store.list_tenant_ids()
    merged: dict[str, dict] = {}
    for t in tenants:
        for r in _search_one(m, query, t, limit):
            rid = str(r.get("id"))
            prev = merged.get(rid)
            if prev is None or (r.get("score") or 0) > (prev.get("score") or 0):
                merged[rid] = r
    return sorted(merged.values(), key=lambda r: r.get("score") or 0, reverse=True)[
        :limit
    ]


def get_by_ids(ids: list[str], user_id: str | None = None) -> list[dict]:
    """Full records for the given ids, restricted to the caller's tenant.
    Unknown or foreign ids are silently skipped (no existence leak)."""
    wanted = {str(i) for i in ids if str(i).strip()}
    if not wanted:
        return []
    out = [r for r in get_all(user_id=user_id) if str(r.get("id")) in wanted]
    order = {str(i): n for n, i in enumerate(ids)}
    out.sort(key=lambda r: order.get(str(r.get("id")), len(order)))
    return out


def timeline(
    memory_id: str, window: int = 3, user_id: str | None = None
) -> dict | None:
    """Chronological neighbours of a memory inside the caller's view
    (own tenant + shared space): what was stored just before/after it."""
    recs = get_all(user_id=user_id)
    recs.sort(key=lambda r: str(r.get("created_at") or ""))
    pos = next(
        (n for n, r in enumerate(recs) if str(r.get("id")) == str(memory_id)), -1
    )
    if pos < 0:
        return None
    w = max(0, min(int(window), 20))
    return {
        "before": recs[max(0, pos - w) : pos],
        "self": recs[pos],
        "after": recs[pos + 1 : pos + 1 + w],
    }


def _get_all_one(m: Memory, tenant: str, agent_id: str) -> list[dict]:
    filters = {"user_id": tenant}
    if agent_id:
        filters["agent_id"] = agent_id
    return _results(m.get_all(filters=filters, top_k=10000))


def get_all(
    agent_id: str = "", user_id: str | None = None, include_shared: bool = True
) -> list[dict]:
    m = get_memory()
    tenant = _tenant(user_id)
    tenants = [tenant]
    if include_shared and tenant != settings.shared_tenant_id:
        tenants.append(settings.shared_tenant_id)
    merged: dict[str, dict] = {}
    for t in tenants:
        for r in _get_all_one(m, t, agent_id):
            merged.setdefault(str(r.get("id")), r)
    return list(merged.values())


def get_all_across(agent_id: str = "", tenants: list[str] | None = None) -> list[dict]:
    """Admin view: every tenant's memories (includes the shared space).
    Prefers a single unfiltered scan; falls back to merging per-tenant reads."""
    m = get_memory()
    try:
        filters = {"agent_id": agent_id} if agent_id else {}
        return _results(m.get_all(filters=filters, top_k=10000))
    except Exception:
        pass
    if tenants is None:
        import users as users_store

        tenants = users_store.list_tenant_ids()
    merged: dict[str, dict] = {}
    for t in tenants:
        for r in _get_all_one(m, t, agent_id):
            merged.setdefault(str(r.get("id")), r)
    return list(merged.values())


def get_owner(memory_id: str) -> str | None:
    """The tenant a memory belongs to, or None when it does not exist.
    Used for ownership checks before update/delete — cross-tenant ids must
    behave exactly like missing ones (404, no existence leak)."""
    m = get_memory()
    try:
        r = m.get(memory_id)
    except Exception:
        return None
    if not isinstance(r, dict):
        return None
    return r.get("user_id") or None


def update(memory_id: str, content: str) -> dict:
    _guard_for_writes()
    m = get_memory()
    return m.update(memory_id, content)


def delete(memory_id: str) -> dict:
    m = get_memory()
    return m.delete(memory_id)


def update_tags(
    memory_id: str, tags: list, user_id: str | None = None, allow_all: bool = False
) -> bool:
    """Directly patch the tags field in the DB payload, preserving all other
    metadata. Fails (False) when the memory belongs to another tenant."""
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM mem0 WHERE id = %s", (memory_id,))
            row = cur.fetchone()
            if not row:
                return False
            payload = row[0]
            if not allow_all and payload.get("user_id") != _tenant(user_id):
                return False
    except Exception:
        return False  # store unreachable or bad id — indistinguishable for the caller
    try:
        with conn, conn.cursor() as cur:
            meta = payload.get("metadata") or {}
            if isinstance(meta.get("metadata"), dict):
                meta["metadata"]["tags"] = tags
            else:
                meta["tags"] = tags
            payload["metadata"] = meta
            cur.execute(
                "UPDATE mem0 SET payload = %s WHERE id = %s",
                (json.dumps(payload), memory_id),
            )
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Session working memory (checkpoints)
#
# Deliberately NOT stored in mem0: a checkpoint must be readable the instant it
# is written (no queue, no LLM extraction, no embedding) so it survives a
# context compaction that happens seconds later. One row per session, upserted.
# ---------------------------------------------------------------------------

_session_schema_ok = False
_session_schema_lock = threading.Lock()

_SESSION_COLUMNS = (
    "session_id, owner, project, harness, state, auto_notes, created_at, updated_at"
)


def _ensure_session_table() -> None:
    """Create session_state lazily — the import-time schema step is non-fatal,
    so the DB may not have been reachable when the process started. Additive
    only: `owner` (the tenant a session belongs to) is added with
    IF NOT EXISTS so an old image keeps working after migration."""
    global _session_schema_ok
    if _session_schema_ok:
        return
    with _session_schema_lock:
        if _session_schema_ok:
            return
        conn = _db_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                        CREATE TABLE IF NOT EXISTS session_state (
                            session_id  TEXT PRIMARY KEY,
                            project     TEXT NOT NULL DEFAULT '',
                            harness     TEXT NOT NULL DEFAULT '',
                            state       TEXT NOT NULL DEFAULT '',
                            auto_notes  TEXT NOT NULL DEFAULT '',
                            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """)
                cur.execute(
                    "ALTER TABLE session_state ADD COLUMN IF NOT EXISTS "
                    "owner TEXT NOT NULL DEFAULT 'default'"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS session_state_project_idx "
                    "ON session_state (project, updated_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS session_state_owner_idx "
                    "ON session_state (owner, updated_at DESC)"
                )
            _session_schema_ok = True
        finally:
            conn.close()


def _upsert_session(
    session_id: str,
    column: str,
    value: str,
    project: str,
    harness: str,
    owner: str | None = None,
) -> dict | None:
    assert column in ("state", "auto_notes")
    _ensure_session_table()
    tenant = _tenant(owner)
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Empty project/harness never overwrite known values. The owner
                # of an existing row never changes: a session belongs to the
                # tenant that created it.
                cur.execute(
                    f"""
                    INSERT INTO session_state (session_id, owner, project, harness, {column})
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE SET
                        {column}   = EXCLUDED.{column},
                        project    = COALESCE(NULLIF(EXCLUDED.project, ''), session_state.project),
                        harness    = COALESCE(NULLIF(EXCLUDED.harness, ''), session_state.harness),
                        updated_at = now()
                    WHERE session_state.owner = EXCLUDED.owner
                    RETURNING {_SESSION_COLUMNS}
                    """,
                    (session_id, tenant, project or "", harness or "", value),
                )
                row = cur.fetchone()
                if row is None:
                    # Row exists under another tenant (or insert raced): behave
                    # like it does not exist for this caller.
                    return None
                return dict(row)
    finally:
        conn.close()


def register_session(
    session_id: str, project: str = "", harness: str = "", owner: str | None = None
) -> None:
    """Create the session row or fill in missing project/harness, without
    touching state, auto_notes or updated_at (an empty row is not listed as a
    previous session, see list_checkpoints)."""
    if not session_id or not (project or harness):
        return
    _ensure_session_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_state (session_id, owner, project, harness)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE SET
                        project = COALESCE(NULLIF(EXCLUDED.project, ''), session_state.project),
                        harness = COALESCE(NULLIF(EXCLUDED.harness, ''), session_state.harness)
                    WHERE session_state.owner = EXCLUDED.owner
                    """,
                    (session_id, _tenant(owner), project or "", harness or ""),
                )
    finally:
        conn.close()


def put_checkpoint(
    session_id: str,
    state: str,
    project: str = "",
    harness: str = "",
    owner: str | None = None,
) -> dict | None:
    return _upsert_session(session_id, "state", state, project, harness, owner)


def put_auto_notes(
    session_id: str,
    notes: str,
    project: str = "",
    harness: str = "",
    owner: str | None = None,
) -> dict | None:
    return _upsert_session(session_id, "auto_notes", notes, project, harness, owner)


def delete_checkpoint(session_id: str, owner: str | None = None) -> bool:
    """Delete a session row. Returns False if it did not exist (or belongs to
    another tenant — indistinguishable on purpose)."""
    _ensure_session_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if owner is None:
                    cur.execute(
                        "DELETE FROM session_state WHERE session_id = %s", (session_id,)
                    )
                else:
                    cur.execute(
                        "DELETE FROM session_state WHERE session_id = %s AND owner = %s",
                        (session_id, _tenant(owner)),
                    )
                return cur.rowcount > 0
    finally:
        conn.close()


def get_checkpoint(session_id: str, owner: str | None = None) -> dict | None:
    """A session's checkpoint. owner=None reads across tenants (admin/scripts)."""
    _ensure_session_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if owner is None:
                cur.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM session_state WHERE session_id = %s",
                    (session_id,),
                )
            else:
                cur.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM session_state WHERE session_id = %s AND owner = %s",
                    (session_id, _tenant(owner)),
                )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def list_checkpoints(
    project: str = "",
    exclude_session: str = "",
    limit: int = 10,
    harness: str = "",
    owner: str | None = None,
) -> list[dict]:
    """Most recently updated sessions with a non-empty state, newest first.
    owner=None lists across all tenants (admin/scripts)."""
    _ensure_session_table()
    clauses = ["(state <> '' OR auto_notes <> '')"]
    params: list = []
    if owner is not None:
        clauses.append("owner = %s")
        params.append(_tenant(owner))
    if project:
        clauses.append("project = %s")
        params.append(project)
    if harness:
        clauses.append("harness = %s")
        params.append(harness)
    if exclude_session:
        clauses.append("session_id <> %s")
        params.append(exclude_session)
    params.append(max(1, min(limit, 100)))
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {_SESSION_COLUMNS} FROM session_state WHERE {' AND '.join(clauses)} "
                f"ORDER BY updated_at DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# Run schema setup at import time (non-fatal if DB not yet ready)
_ensure_schema()
try:
    _ensure_session_table()
except Exception as e:
    logger.warning("session_state schema setup deferred (non-fatal): %s", e)
