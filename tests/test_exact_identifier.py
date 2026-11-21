"""Exact-identifier keyword path + hybrid RRF (bean 4aqv).

The db test database has no mem0 table (the app uses the fake memory store
there), so this module creates a minimal stand-in with the same columns the
keyword SQL reads (id, payload jsonb) and seeds payloads directly.
"""

import json

import psycopg2
import pytest
import requests
from conftest import DB_URL, TOKEN

pytestmark = pytest.mark.db

ID_EHOST = "aaaaaaaa-0000-0000-0000-000000000001"
ID_OBJSTORE = "aaaaaaaa-0000-0000-0000-000000000002"
ID_TURTLE = "aaaaaaaa-0000-0000-0000-000000000003"
ID_OTHER = "aaaaaaaa-0000-0000-0000-000000000004"


@pytest.fixture
def mem0_table():
    conn = psycopg2.connect(DB_URL)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mem0 (id uuid PRIMARY KEY, payload jsonb)"
            )
            cur.execute("DELETE FROM mem0")
            rows = [
                (
                    ID_EHOST,
                    {
                        "data": "agent-shell node processes get EHOSTUNREACH on LAN while curl works",
                        "text_lemmatized": "agent shell node processes get ehostunreach on lan while curl works",
                        "user_id": "default",
                        "category": "reference",
                        "created_at": "2026-11-20T10:00:00+00:00",
                        "updated_at": "2026-11-20T10:00:00+00:00",
                    },
                ),
                (
                    ID_OBJSTORE,
                    {
                        "data": "restore fails with ObjectStoreConfiguration invalid no credentials defined",
                        "text_lemmatized": "restore fails with objectstoreconfiguration invalid no credentials defined",
                        "user_id": "default",
                        "created_at": "2026-11-21T10:00:00+00:00",
                        "updated_at": "2026-11-21T10:00:00+00:00",
                    },
                ),
                (
                    ID_TURTLE,
                    {
                        "data": "helper left root-owned WiredTiger.turtle so mongo crash-looped",
                        "text_lemmatized": "helper left root owned wiredtiger.turtle so mongo crash looped",
                        "user_id": "default",
                        "created_at": "2026-11-18T10:00:00+00:00",
                        "updated_at": "2026-11-18T10:00:00+00:00",
                    },
                ),
                (
                    ID_OTHER,
                    {
                        "data": "EHOSTUNREACH in another tenant must stay invisible",
                        "text_lemmatized": "ehostunreach in another tenant must stay invisible",
                        "user_id": "other",
                        "created_at": "2026-11-19T10:00:00+00:00",
                        "updated_at": "2026-11-19T10:00:00+00:00",
                    },
                ),
            ]
            for rid, p in rows:
                cur.execute(
                    "INSERT INTO mem0 (id, payload) VALUES (%s, %s)",
                    (rid, json.dumps(p)),
                )
    conn.close()
    yield
    conn = psycopg2.connect(DB_URL)
    with conn:
        conn.cursor().execute("DROP TABLE IF EXISTS mem0")
    conn.close()


def test_keyword_hits_exact_identifier(server, mem0_table):
    import memory

    hits = memory.keyword_hits("EHOSTUNREACH", user_id="default")
    ids = [h["id"] for h in hits]
    assert ID_EHOST in ids
    assert ID_OTHER not in ids  # tenant isolation
    assert any(h["match"] == "keyword" for h in hits)


def test_keyword_substring_via_trgm(server, mem0_table):
    import memory

    hits = memory.keyword_hits("WiredTiger.turtl", user_id="default")
    assert ID_TURTLE in [h["id"] for h in hits]  # substring, not a whole token


def test_rrf_prefers_both_lists(server):
    import memory

    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    out = memory._rrf_merge([a, b], [b, c], limit=3)
    assert out[0]["id"] == "b"  # in both lists -> best fused score
    assert {r["id"] for r in out} == {"a", "b", "c"}
    assert out[0]["hybrid_score"] >= out[1]["hybrid_score"]


def test_hybrid_finds_what_semantic_misses(server, mem0_table, api):
    import memory

    sem = memory.search("EHOSTUNREACH", limit=5, user_id="default")
    hyb = memory.search_hybrid("EHOSTUNREACH", limit=5, user_id="default")
    sem_ids = {str(r.get("id")) for r in sem}
    assert ID_EHOST not in sem_ids  # fake semantic store misses it
    assert ID_EHOST in {str(r.get("id")) for r in hyb}


def test_rest_hybrid_param(server, mem0_table):
    r = requests.get(
        server + "/api/memories",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params={"q": "ObjectStoreConfiguration", "hybrid": "1"},
        timeout=20,
    )
    assert r.status_code == 200
    assert ID_OBJSTORE in {m["id"] for m in r.json()}


def test_mcp_hybrid_param(server, mem0_table):
    from test_mcp_tools import call_tool

    out = call_tool(
        server, "search_memory", query="ObjectStoreConfiguration", hybrid=True
    )
    assert "objectstoreconfiguration" in out.lower()
