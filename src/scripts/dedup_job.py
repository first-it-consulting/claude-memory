#!/usr/bin/env python3
"""
Memory deduplication, LLM tagging, and consolidation job.

Designed to run as a Kubernetes CronJob during off-peak hours.
All originals are archived to mem0_archive before any modification —
the archive is append-only and never touched by this script after writing.

Phase 0 — LLM tagging (requires Ollama):
  Find memories with no tags. Ask Ollama to assign tags from the tag_rules dictionary.
  Skip if TAGGING_MODE=keyword (keyword-only, handled at write time).

Phase 1 — Hash dedup (fast, exact):
  Find rows sharing the same payload->>'hash'. Keep the oldest, archive the rest.

Phase 2 — Semantic dedup (slow, Ollama):
  Find clusters of memories with cosine similarity > SEMANTIC_THRESHOLD.
  For each cluster: archive originals → ask Ollama to merge → store merged (infer=False).

Usage:
  python dedup_job.py [--dry-run] [--phase1-only] [--phase2-only] [--threshold 0.92]
  TAGGING_MODE=llm python dedup_job.py   # run LLM tagging phase
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import os
import sys
import time
import uuid
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

import promotion  # sibling module: checkpoint parsing

log = logging.getLogger("dedup")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
MEMORY_URL = os.environ.get("CLAUDE_MEMORY_URL", "http://claude-memory:8080")
MEMORY_TOKEN = os.environ.get("CLAUDE_MEMORY_TOKEN", "")
TAGGING_MODE = os.environ.get("TAGGING_MODE", "keyword")  # keyword | llm | hybrid
# Checkpoint promotion: off | dry-run (report only, the default) | on
PROMOTE_MODE = os.environ.get("PROMOTE_MODE", "dry-run").strip().lower()
PROMOTE_IDLE_HOURS = float(os.environ.get("PROMOTE_IDLE_HOURS", "12"))
PROMOTE_DUP_THRESHOLD = float(os.environ.get("PROMOTE_DUP_THRESHOLD", "0.92"))

ARCHIVE_TABLE = "mem0_archive"
MEM_TABLE = "mem0"

DEFAULT_SEMANTIC_THRESHOLD = 0.92
MIN_CLUSTER_SIZE = 2

# Batch size for LLM tagging — smaller = fewer tokens per request
LLM_TAG_BATCH = 20


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    psycopg2.extras.register_uuid(conn)
    return conn


def ensure_archive_table(cur: psycopg2.extensions.cursor) -> None:
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            original_id     UUID NOT NULL,
            payload         JSONB NOT NULL,
            archived_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            archive_reason  TEXT NOT NULL,
            merged_into_id  UUID
        );
        CREATE INDEX IF NOT EXISTS {ARCHIVE_TABLE}_original_id_idx
            ON {ARCHIVE_TABLE}(original_id);
        CREATE INDEX IF NOT EXISTS {ARCHIVE_TABLE}_archived_at_idx
            ON {ARCHIVE_TABLE}(archived_at DESC);
    """)


def ensure_job_runs_table(cur: psycopg2.extensions.cursor) -> None:
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
    cur.execute("ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS details TEXT")


def start_job_phase(conn, phase: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (phase, status) VALUES (%s, 'running') RETURNING id",
            (phase,),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    return run_id


def finish_job_phase(
    conn,
    run_id: str,
    status: str,
    scanned: int = 0,
    changed: int = 0,
    rules_added: int = 0,
    error: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE job_runs SET
               details          = COALESCE(%s, details),
               finished_at      = now(),
               status           = %s,
               memories_scanned = %s,
               memories_changed = %s,
               rules_added      = %s,
               duration_seconds = EXTRACT(EPOCH FROM (now() - started_at)),
               error            = %s
               WHERE id = %s""",
            (details, status, scanned, changed, rules_added, error, run_id),
        )
    conn.commit()


def _uuids(ids) -> list[uuid.UUID]:
    """Row ids may come back as str; the id column is uuid (no uuid = text)."""
    return [i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in ids]


def archive_rows(
    cur: psycopg2.extensions.cursor,
    row_ids: list[uuid.UUID],
    reason: str,
    merged_into_id: Optional[uuid.UUID] = None,
) -> None:
    if merged_into_id is not None:
        merged_into_id = _uuids([merged_into_id])[0]
    for rid in _uuids(row_ids):
        cur.execute(
            f"""
            INSERT INTO {ARCHIVE_TABLE} (original_id, payload, archive_reason, merged_into_id)
            SELECT id, payload, %s, %s
            FROM {MEM_TABLE}
            WHERE id = %s
            """,
            (reason, merged_into_id, rid),
        )


def delete_rows(cur: psycopg2.extensions.cursor, row_ids: list[uuid.UUID]) -> None:
    cur.execute(
        f"DELETE FROM {MEM_TABLE} WHERE id = ANY(%s)",
        (_uuids(row_ids),),
    )


# ---------------------------------------------------------------------------
# Metadata helpers (mirrors _extract_meta in main.py)
# ---------------------------------------------------------------------------

def _extract_meta(payload: dict) -> dict:
    meta = payload.get("metadata") or {}
    inner = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else None
    return inner if inner is not None else meta


def _set_tags_in_payload(payload: dict, tags: list[str]) -> dict:
    meta = payload.get("metadata") or {}
    if isinstance(meta.get("metadata"), dict):
        meta["metadata"]["tags"] = tags
    else:
        meta["tags"] = tags
    payload["metadata"] = meta
    return payload


def _merge_tags_and_mark_reviewed(payload: dict, new_tags: list[str]) -> dict:
    """Merge new_tags with any existing tags and set llm_reviewed=True."""
    meta = payload.get("metadata") or {}
    if isinstance(meta.get("metadata"), dict):
        inner = meta["metadata"]
        existing = inner.get("tags") or []
        inner["tags"] = sorted(set(existing) | set(new_tags))
        inner["llm_reviewed"] = True
    else:
        existing = meta.get("tags") or []
        meta["tags"] = sorted(set(existing) | set(new_tags))
        meta["llm_reviewed"] = True
    payload["metadata"] = meta
    return payload


def load_tag_list(cur) -> list[str]:
    """Load all tag names from tag_rules table."""
    try:
        cur.execute("SELECT DISTINCT tag FROM tag_rules ORDER BY tag")
        return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log.warning("Could not load tag_rules: %s — using empty list", e)
        return []


# ---------------------------------------------------------------------------
# Phase 0 — LLM tagging
# ---------------------------------------------------------------------------

def ollama_assign_tags(text: str, available_tags: list[str]) -> list[str]:
    """Ask Ollama which tags apply to a memory. Returns list of tag names."""
    tag_list = ", ".join(available_tags)
    prompt = (
        f"You are a memory tagging system. Given a memory entry, select which tags apply.\n"
        f"Available tags: {tag_list}\n\n"
        f"Memory: \"{text}\"\n\n"
        f"Reply with ONLY a JSON array of matching tag names, or [] if none apply.\n"
        f"Example: [\"prod-k8s\", \"cnpg\"]\n"
        f"Array:"
    )
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Extract JSON array from response
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return []
        candidates = json.loads(raw[start:end + 1])
        valid = set(available_tags)
        return [t for t in candidates if isinstance(t, str) and t in valid]
    except Exception as e:
        log.warning("ollama_assign_tags failed: %s", e)
        return []


def phase0_llm_tagging(conn, dry_run: bool) -> int:
    if TAGGING_MODE == "keyword":
        log.info("Phase 0: skipped (TAGGING_MODE=keyword)")
        return 0

    log.info("Phase 0: LLM tagging (TAGGING_MODE=%s) — reviews all memories not yet llm_reviewed", TAGGING_MODE)
    run_id = start_job_phase(conn, "llm-tagging")
    scanned = 0
    changed = 0

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            ensure_job_runs_table(cur)
            available_tags = load_tag_list(cur)
            if not available_tags:
                log.warning("No tags in tag_rules table — skipping LLM tagging")
                finish_job_phase(conn, run_id, "completed", 0, 0)
                return 0

            cur.execute(f"SELECT id, payload FROM {MEM_TABLE}")
            rows = cur.fetchall()

        # Process any memory not yet reviewed by LLM — includes keyword-tagged ones
        pending = []
        for row in rows:
            payload = dict(row["payload"]) if isinstance(row["payload"], dict) else json.loads(row["payload"])
            meta = _extract_meta(payload)
            if not meta.get("llm_reviewed"):
                text = payload.get("data", "").strip()
                if text:
                    pending.append({"id": str(row["id"]), "payload": payload, "text": text})

        log.info("Found %d memories not yet llm_reviewed", len(pending))
        scanned = len(pending)

        for item in pending:
            llm_tags = ollama_assign_tags(item["text"], available_tags)
            existing = _extract_meta(item["payload"]).get("tags") or []
            merged = sorted(set(existing) | set(llm_tags))
            added = sorted(set(llm_tags) - set(existing))
            log.info("  %s → existing=%s llm=%s added=%s", item["text"][:60], existing, llm_tags, added)
            if not dry_run:
                updated_payload = _merge_tags_and_mark_reviewed(item["payload"], llm_tags)
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {MEM_TABLE} SET payload = %s WHERE id = %s",
                        (json.dumps(updated_payload), item["id"]),
                    )
                conn.commit()
            if added:
                changed += 1

        finish_job_phase(conn, run_id, "completed", scanned, changed)
    except Exception as e:
        conn.rollback()
        finish_job_phase(conn, run_id, "failed", scanned, changed, error=str(e))
        log.error("Phase 0 failed: %s", e)

    log.info(
        "Phase 0 done: scanned=%d new_tags_added_to=%d%s",
        scanned, changed, " (dry run)" if dry_run else "",
    )
    return changed


# ---------------------------------------------------------------------------
# Phase 1 — Hash-based exact dedup
# ---------------------------------------------------------------------------

def phase1_hash_dedup(conn, dry_run: bool) -> int:
    log.info("Phase 1: hash-based exact dedup")
    run_id = start_job_phase(conn, "hash-dedup")
    removed = 0

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            ensure_archive_table(cur)
            ensure_job_runs_table(cur)

            # Dedup per tenant: identical texts in two workspaces are two
            # legitimate memories, not duplicates of each other.
            cur.execute(f"""
                SELECT payload->>'hash' AS hash,
                       payload->>'user_id' AS uid,
                       array_agg(id ORDER BY (payload->>'created_at') ASC) AS ids
                FROM {MEM_TABLE}
                WHERE payload->>'hash' IS NOT NULL
                  AND payload->>'hash' != ''
                GROUP BY payload->>'hash', payload->>'user_id'
                HAVING COUNT(*) > 1
            """)
            clusters = cur.fetchall()
            log.info("Found %d hash clusters with duplicates", len(clusters))

            for row in clusters:
                keep_id = row["ids"][0]
                drop_ids = [uuid.UUID(str(i)) for i in row["ids"][1:]]
                log.info("Hash dedup: keeping %s, dropping %d", keep_id, len(drop_ids))
                if not dry_run:
                    archive_rows(cur, drop_ids, "hash-dedup")
                    delete_rows(cur, drop_ids)
                removed += len(drop_ids)

            if not dry_run:
                conn.commit()

        finish_job_phase(conn, run_id, "completed", scanned=len(clusters), changed=removed)
    except Exception as e:
        conn.rollback()
        finish_job_phase(conn, run_id, "failed", error=str(e))
        log.error("Phase 1 failed: %s", e)

    log.info("Phase 1 done: removed %d%s", removed, " (dry run)" if dry_run else "")
    return removed


# ---------------------------------------------------------------------------
# Phase 2 — Semantic near-dedup via pgvector
# ---------------------------------------------------------------------------

def find_semantic_clusters(
    cur: psycopg2.extensions.cursor, threshold: float
) -> list[list[uuid.UUID]]:
    cosine_distance = 1.0 - threshold

    # Only within a tenant: a merge would move the result into one workspace
    # and silently strip it from the other.
    cur.execute(
        f"""
        SELECT a.id AS id_a, b.id AS id_b,
               1 - (a.vector <=> b.vector) AS similarity
        FROM {MEM_TABLE} a
        JOIN {MEM_TABLE} b ON a.id < b.id
        WHERE (a.vector <=> b.vector) < %s
          AND COALESCE(a.payload->>'user_id','default') = COALESCE(b.payload->>'user_id','default')
        ORDER BY similarity DESC
        """,
        (cosine_distance,),
    )
    pairs = cur.fetchall()
    log.info("Found %d near-duplicate pairs (threshold=%.2f)", len(pairs), threshold)

    if not pairs:
        return []

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(x: str, y: str) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[py] = px

    for row in pairs:
        union(str(row[0]), str(row[1]))

    groups: dict[str, list[str]] = {}
    all_ids: set[str] = set()
    for row in pairs:
        all_ids.add(str(row[0]))
        all_ids.add(str(row[1]))
    for nid in all_ids:
        root = find(nid)
        groups.setdefault(root, []).append(nid)

    clusters = [
        [uuid.UUID(i) for i in ids]
        for ids in groups.values()
        if len(ids) >= MIN_CLUSTER_SIZE
    ]
    log.info("Clustered into %d semantic groups", len(clusters))
    return clusters


def _strip_preamble(text: str) -> str:
    """Drop a leading 'Memory:' / 'Merged memory:' / 'Here is ...:' line."""
    lines = (text or "").strip().splitlines()
    while lines and re.fullmatch(r"\s*(\*\*)?((merged|combined|final)\s+)?memory(\s+text)?:?(\*\*)?:?\s*|\s*here is.*:\s*", lines[0], re.I):
        lines = lines[1:]
    # drop the model's own remarks, e.g. "- (no specific date mentioned in both memories)"
    lines = [l for l in lines if not re.fullmatch(r"\s*(?:[-*]\s*)?\([^()]*\)\s*", l)]
    return "\n".join(lines).strip()


def ollama_merge(texts: list[str]) -> Optional[str]:
    numbered = "\n\n".join(f"Memory {i+1}:\n{t}" for i, t in enumerate(texts))
    prompt = (
        f"You have {len(texts)} similar memory entries. "
        "Produce a single, comprehensive merged memory that captures all unique information. "
        "Be concise but complete. Preserve specific details like dates, names, file paths, "
        "and technical values. Do not add notes, comments or remarks about the merge "
        "or about what is missing. Output ONLY the merged memory text, nothing else.\n\n"
        f"{numbered}"
    )
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return _strip_preamble(resp.json().get("response", ""))
    except Exception as e:
        log.warning("Ollama merge failed: %s", e)
        return None


_FACT_RE = re.compile(
    r"[\w.~-]*/[\w./-]+"       # paths, URLs, repo names
    r"|\b\w*\d[\w.:-]*"        # numbers, versions, times, ids
)


def fact_tokens(text: str) -> set[str]:
    """Concrete details an LLM merge must not drop or alter: numbers, versions,
    times, paths. llama3.2 has been seen rewriting '7.7.28->10.7.4 (outdated)'
    into an 'upgrade', so a merge is accepted only if all of these survive."""
    return {t.strip(".,;:()[]'\"-").lower() for t in _FACT_RE.findall(text or "")} - {""}


def words(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


def covering_text(texts: list[str]) -> Optional[int]:
    """Index of a text that already contains every word of the others, if any:
    the cluster can then be reduced to it verbatim, without an LLM rewrite."""
    for i in sorted(range(len(texts)), key=lambda k: -len(texts[k])):
        w = words(texts[i])
        if all(words(t) <= w for j, t in enumerate(texts) if j != i):
            return i
    return None


def merge_is_faithful(merged: str, texts: list[str]) -> bool:
    missing = set().union(*(fact_tokens(t) for t in texts)) - fact_tokens(merged)
    if missing:
        log.info("  merge rejected, details lost: %s", sorted(missing)[:10])
        return False
    return True


def delete_memory_direct(memory_id: str) -> None:
    try:
        import memory as mstore  # imported by store_memory_direct before this runs
        mstore.delete(memory_id)
    except Exception as e:
        log.warning("could not remove merged memory %s: %s", memory_id, e)


def store_memory_direct(content: str, agent_id: str = "", category: str = "",
                        tags: Optional[list] = None,
                        extra_metadata: Optional[dict] = None,
                        user_id: Optional[str] = None) -> Optional[str]:
    """Store the merged memory verbatim, in-process (the job runs in the server
    image and gets the server's env). Returns the new id, or None — callers must
    then keep the originals. (The old MCP-over-HTTP call could not parse the
    stateless JSON response and add_memory is queued, so it never got an id.)"""
    try:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        if src not in sys.path:
            sys.path.insert(0, src)
        import memory as mstore  # server module

        result = mstore.add(content, user_id=user_id, agent_id=agent_id, infer=False,
                            category=category, tags=tags or [],
                            extra_metadata=extra_metadata)
        results = (result or {}).get("results") or []
        return results[0].get("id") if results else None
    except Exception as e:
        log.warning("store_memory_direct failed: %s", e)
        return None


def phase2_semantic_dedup(conn, threshold: float, dry_run: bool) -> int:
    log.info("Phase 2: semantic dedup (threshold=%.2f)", threshold)
    run_id = start_job_phase(conn, "semantic-dedup")
    merged = 0
    clusters_seen = 0

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            ensure_archive_table(cur)
            clusters = find_semantic_clusters(cur, threshold)

            for cluster_ids in clusters:
                cur.execute(
                    f"SELECT id, payload FROM {MEM_TABLE} WHERE id = ANY(%s)",
                    (cluster_ids,),
                )
                rows = cur.fetchall()

                categories = [
                    (_extract_meta(r["payload"]).get("category") or "")
                    for r in rows
                ]
                if len(set(categories)) > 1:
                    log.info("Skipping cross-category cluster: %s", set(categories))
                    continue

                cluster_category = categories[0] if categories else ""
                rows = [r for r in rows if (r["payload"].get("data") or "").strip()]
                if len(rows) < MIN_CLUSTER_SIZE:
                    continue
                texts = [r["payload"]["data"] for r in rows]
                metas = [_extract_meta(r["payload"]) for r in rows]
                tags = sorted({t for m in metas for t in (m.get("tags") or []) if isinstance(t, str)})
                agent = rows[0]["payload"].get("agent_id") or ""
                clusters_seen += 1

                keep = covering_text(texts)
                if keep is not None:
                    # One entry already says everything: keep it as is, drop the rest.
                    drop = [r["id"] for i, r in enumerate(rows) if i != keep]
                    log.info("Cluster of %d: keeping %s verbatim", len(rows), rows[keep]["id"])
                    if not dry_run:
                        archive_rows(cur, drop, "semantic-dedup", merged_into_id=rows[keep]["id"])
                        delete_rows(cur, drop)
                        conn.commit()
                    merged += len(drop)
                    continue

                log.info("Merging cluster of %d (category=%r)", len(texts), cluster_category)
                if dry_run:
                    merged += len(rows)
                    continue

                merged_text = ollama_merge(texts)
                if not merged_text or not merge_is_faithful(merged_text, texts):
                    log.warning("Merge failed or unfaithful — keeping originals")
                    continue

                log.info("  → merged: %s", merged_text[:120])
                # Clusters never span tenants (see find_semantic_clusters), so
                # the merged memory stays in the cluster's workspace.
                cluster_user = rows[0]["payload"].get("user_id")
                new_id = store_memory_direct(merged_text, agent_id=agent,
                                             category=cluster_category, tags=tags,
                                             user_id=cluster_user)
                if not new_id:
                    log.warning("Storing merged memory failed — keeping originals")
                    continue
                ids = [r["id"] for r in rows]
                try:
                    archive_rows(cur, ids, "semantic-dedup", merged_into_id=new_id)
                    delete_rows(cur, ids)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    delete_memory_direct(new_id)  # originals stay; don't leave a duplicate
                    raise
                merged += len(rows)

        finish_job_phase(conn, run_id, "completed", scanned=clusters_seen, changed=merged)
    except Exception as e:
        conn.rollback()
        finish_job_phase(conn, run_id, "failed", error=str(e))
        log.error("Phase 2 failed: %s", e)

    log.info(
        "Phase 2 done: processed %d%s",
        merged, " (dry run)" if dry_run else "",
    )
    return merged


# ---------------------------------------------------------------------------
# Checkpoint promotion
# ---------------------------------------------------------------------------

def _mstore():
    """The server's memory module (the job runs in the server image)."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if src not in sys.path:
        sys.path.insert(0, src)
    import memory as mstore  # noqa: E402
    return mstore


def find_duplicate(text: str, threshold: float, user_id: Optional[str] = None) -> Optional[float]:
    """Best similarity of an existing memory to `text` if >= threshold, else None.
    Raises when the search itself fails, so nothing is promoted blind.
    Searches the owning tenant (plus the shared space), never other tenants."""
    hits = _mstore().search(text, limit=3, user_id=user_id) or []
    best = max((h.get("score") or 0.0 for h in hits), default=0.0)
    return best if best >= threshold else None


def promotable_sessions(conn, idle_hours: float) -> list[dict]:
    """Finished sessions (idle for idle_hours) with a checkpoint that was not
    promoted yet, or that changed since it was promoted."""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("ALTER TABLE session_state ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ")
        cur.execute(
            """SELECT session_id, owner, project, harness, state, updated_at
               FROM session_state
               WHERE state <> ''
                 AND updated_at < now() - make_interval(secs => %s)
                 AND (promoted_at IS NULL OR updated_at > promoted_at)
               ORDER BY updated_at""",
            (idle_hours * 3600,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def phase_promote_checkpoints(conn, dry_run: bool, mode: str = PROMOTE_MODE,
                              idle_hours: float = PROMOTE_IDLE_HOURS,
                              threshold: float = PROMOTE_DUP_THRESHOLD) -> int:
    """Copy Decisions / Dead ends of finished sessions into long-term memory,
    verbatim, tagged auto-promoted. dry-run writes only the job_runs report."""
    if mode == "off":
        log.info("Checkpoint promotion: skipped (PROMOTE_MODE=off)")
        return 0
    dry = dry_run or mode != "on"
    log.info("Checkpoint promotion (%s, idle >= %.0fh)", "dry run" if dry else "on", idle_hours)
    run_id = start_job_phase(conn, "checkpoint-promotion")
    promoted, sessions, report = 0, [], []
    try:
        sessions = promotable_sessions(conn, idle_hours)
        seen: set[str] = set()
        for s in sessions:
            kept, dropped = promotion.candidates(s["state"], s["project"], s["harness"])
            if not kept and not dropped:
                if not dry:
                    _mark_promoted(conn, s["session_id"])
                continue
            owner = s.get("owner") or "default"  # promoted lines land in the session's workspace
            report.append(f"## {s['project'] or '-'} · {s['harness'] or '-'} · session {s['session_id']} ({owner})")
            for c in kept:
                key = (owner, c["text"].lower())
                if key in seen:
                    report.append(f"  skip (same line in another session): {c['content']}")
                    continue
                seen.add(key)
                dup = find_duplicate(c["content"], threshold, owner) or find_duplicate(c["text"], threshold, owner)
                if dup is not None:
                    report.append(f"  skip (already known, {dup:.2f}): {c['content']}")
                    continue
                if dry:
                    report.append(f"  WOULD PROMOTE: {c['content']}")
                    promoted += 1
                    continue
                new_id = store_memory_direct(
                    c["content"], agent_id=s["harness"] or "", category="project", tags=c["tags"],
                    extra_metadata={"source": "checkpoint", "session_id": s["session_id"]},
                    user_id=owner)
                if not new_id:
                    raise RuntimeError(f"storing failed: {c['content'][:80]}")
                report.append(f"  promoted: {c['content']}")
                promoted += 1
            for kind, text in dropped:
                report.append(f"  ignored (too short or session-only): {text}")
            if not dry:
                _mark_promoted(conn, s["session_id"])
        header = (f"{'DRY RUN — nothing stored. Set PROMOTE_MODE=on to promote.' if dry else 'Promoted.'}"
                  f" Sessions idle >= {idle_hours:.0f}h: {len(sessions)}.")
        finish_job_phase(conn, run_id, "completed", scanned=len(sessions), changed=promoted,
                         details="\n".join([header, ""] + report)[:20000])
    except Exception as e:
        conn.rollback()
        finish_job_phase(conn, run_id, "failed", scanned=len(sessions), changed=promoted,
                         error=str(e), details="\n".join(report)[:20000] or None)
        log.error("Checkpoint promotion failed: %s", e)
    log.info("Checkpoint promotion done: %d line(s)%s", promoted, " (dry run)" if dry else "")
    return promoted


def _mark_promoted(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE session_state SET promoted_at = now() WHERE session_id = %s", (session_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Memory deduplication job")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--phase1-only", action="store_true")
    parser.add_argument("--phase2-only", action="store_true")
    parser.add_argument("--tagging-only", action="store_true", help="Only run LLM tagging phase")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SEMANTIC_THRESHOLD)
    args = parser.parse_args()

    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    log.info(
        "Starting job (dry_run=%s, threshold=%.2f, tagging_mode=%s, promote_mode=%s)",
        args.dry_run, args.threshold, TAGGING_MODE, PROMOTE_MODE,
    )

    conn = connect()
    try:
        # Ensure tables exist (idempotent)
        with conn.cursor() as cur:
            ensure_archive_table(cur)
            ensure_job_runs_table(cur)
        conn.commit()

        if args.tagging_only:
            phase0_llm_tagging(conn, args.dry_run)
            return

        if not args.phase1_only and not args.phase2_only:
            # Promote first, so tonight's dedup already sees the new entries.
            phase_promote_checkpoints(conn, args.dry_run)
            phase0_llm_tagging(conn, args.dry_run)

        if not args.phase2_only:
            phase1_hash_dedup(conn, args.dry_run)

        if not args.phase1_only:
            phase2_semantic_dedup(conn, args.threshold, args.dry_run)

        log.info("Job complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
