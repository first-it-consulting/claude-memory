"""Share-to-space (bean vd3y): admin PATCH /api/memories/{id}/share moves a
memory into the shared space keeping its id; MCP add_memory(shared=true) is
admin-only. Synthetic tenants only - the demo tenant is never touched.

The move is a direct SQL payload update (no mem0 instance, no embedder), so
these tests exercise the real mem0 table with rows inserted by SQL.
"""

import json
import uuid

import pytest
import requests
from conftest import DB_URL

pytestmark = pytest.mark.db

SHARE = "vd3y move me to everyone"


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def tenants():
    """Three synthetic tenants: owner (user), other (user), boss (admin)."""
    import users as users_store

    ids = ("share-a", "share-b", "share-admin")
    conn = __import__("psycopg2").connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute("DELETE FROM users WHERE id = ANY(%s)", (list(ids),))
    finally:
        conn.close()
    _, tok_a = users_store.create_user("share-a", name="A")
    _, tok_b = users_store.create_user("share-b", name="B")
    _, tok_adm = users_store.create_user("share-admin", name="Boss", role="admin")
    return {"tok_a": tok_a, "tok_b": tok_b, "tok_adm": tok_adm}


def _ensure_mem0_table():
    """CI uses plain postgres:16 (no pgvector, no mem0 table). Create the
    real shape (mem0's pgvector collection: id/embedding/payload) when the
    extension is available; skip the test otherwise. This keeps the tests
    honest about the actual table the move UPDATEs."""
    import psycopg2

    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except psycopg2.errors.FeatureNotSupported:
                pytest.skip("no pgvector extension on this database")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mem0"
                " (id uuid PRIMARY KEY, embedding vector(1536), payload jsonb)"
            )
    finally:
        conn.close()


@pytest.fixture
def row_in_db():
    """Insert one synthetic mem0 row owned by share-a; returns its id."""
    import psycopg2

    _ensure_mem0_table()
    rid = str(uuid.uuid4())
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute(
                "INSERT INTO mem0 (id, embedding, payload) VALUES (%s, %s::vector, %s)",
                (
                    rid,
                    "[" + ",".join(["0.1"] * 1536) + "]",
                    json.dumps(
                        {
                            "data": SHARE,
                            "hash": rid[:8],
                            "user_id": "share-a",
                            "text_lemmatized": SHARE,
                            "metadata": {"category": "project"},
                        }
                    ),
                ),
            )
    finally:
        conn.close()
    yield rid
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute("DELETE FROM mem0 WHERE id = %s", (rid,))
    finally:
        conn.close()


def _payload(rid):
    import psycopg2

    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM mem0 WHERE id = %s", (rid,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def test_share_requires_admin(server, tenants, row_in_db):
    r = requests.patch(
        f"{server}/api/memories/{row_in_db}/share",
        headers=_auth(tenants["tok_a"]),
        timeout=20,
    )
    assert r.status_code == 403  # auth before existence check, no leak


def test_share_move_keeps_id_and_provenance(server, tenants, row_in_db):
    r = requests.patch(
        f"{server}/api/memories/{row_in_db}/share",
        headers=_auth(tenants["tok_adm"]),
        timeout=20,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "shared": True, "already_shared": False}
    p = _payload(row_in_db)
    assert p["user_id"] == "global"  # id kept (same row), tenant moved
    assert p["shared_by"] == "share-admin"
    assert p["original_tenant"] == "share-a"
    assert p["shared_at"]
    assert p["metadata"]["original_tenant"] == "share-a"
    # idempotent: second share is a no-op, provenance of the first move kept
    r2 = requests.patch(
        f"{server}/api/memories/{row_in_db}/share",
        headers=_auth(tenants["tok_adm"]),
        timeout=20,
    )
    assert r2.status_code == 200 and r2.json()["already_shared"] is True
    assert _payload(row_in_db)["shared_at"] == p["shared_at"]


def test_shared_row_visible_like_shared_space(server, tenants, row_in_db):
    # The move keeps the row in the one mem0 table (vector + payload), so
    # every read path that filters payload->>'user_id' sees it for any
    # tenant once it is in the shared space. keyword_hits (SQL path) proves
    # it for a DIFFERENT tenant without needing the fake mem0 corpus.
    requests.patch(
        f"{server}/api/memories/{row_in_db}/share",
        headers=_auth(tenants["tok_adm"]),
        timeout=20,
    )
    import memory as mem_store

    hits = mem_store.keyword_hits("vd3y", user_id="share-b", limit=10)
    assert row_in_db in [str(h["id"]) for h in hits]
    own = mem_store.keyword_hits("vd3y", user_id="share-a", limit=10)
    assert row_in_db in [str(o["id"]) for o in own]  # owner keeps seeing it


def test_share_unknown_and_bad_id_404(server, tenants):
    _ensure_mem0_table()
    r = requests.patch(
        f"{server}/api/memories/11111111-2222-3333-4444-555555555555/share",
        headers=_auth(tenants["tok_adm"]),
        timeout=20,
    )
    assert r.status_code == 404
    r2 = requests.patch(
        f"{server}/api/memories/not-a-uuid/share",
        headers=_auth(tenants["tok_adm"]),
        timeout=20,
    )
    assert r2.status_code == 404  # identical to missing - no leak, no 500


def test_mcp_shared_flag_admin_only(server, fake_mem):
    from test_mcp_tools import rpc

    res = rpc(
        server,
        "tools/call",
        {
            "name": "add_memory",
            "arguments": {"content": "vd3y shared via mcp", "shared": True},
        },
    )
    text = res["result"]["content"][0]["text"]
    assert "requires an admin token" in text and "nothing was stored" in text
    assert not any(
        "vd3y shared via mcp" in c.get("memory", "") for c in fake_mem.corpus
    )
    # plain path unchanged for the same caller
    res2 = rpc(
        server,
        "tools/call",
        {"name": "add_memory", "arguments": {"content": "vd3y own workspace note"}},
    )
    assert json.loads(res2["result"]["content"][0]["text"])["queued"] is True
