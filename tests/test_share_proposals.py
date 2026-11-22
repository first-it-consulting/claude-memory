"""Share proposals (bean oi38): owner proposes, admin approves (with the
content-stamp refusal when the owner edited after proposing) or rejects with
a reason; withdraw, rate limit, admin-only worklist. Synthetic tenants, real
mem0 SQL rows — the proposal machinery is direct payload SQL."""

import json
import uuid

import pytest
from conftest import DB_URL
from test_multitenancy import req

pytestmark = pytest.mark.db


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _ensure_table():
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


def _insert(payload: dict) -> str:
    rid = str(uuid.uuid4())
    payload.setdefault("hash", rid[:8])
    payload.setdefault("text_lemmatized", payload.get("data", ""))
    conn = __import__("psycopg2").connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute(
                "INSERT INTO mem0 (id, embedding, payload) VALUES (%s, %s::vector, %s)",
                (rid, "[" + ",".join(["0.1"] * 1536) + "]", json.dumps(payload)),
            )
    finally:
        conn.close()
    return rid


def _payload(rid):
    import psycopg2

    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM mem0 WHERE id = %s", (rid,))
            return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def users():
    import users as users_store

    ids = ("oi38-prop", "oi38-other", "oi38-adm")
    conn = __import__("psycopg2").connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute("DELETE FROM users WHERE id = ANY(%s)", (list(ids),))
    finally:
        conn.close()
    _, tok_p = users_store.create_user("oi38-prop", name="Proposer")
    _, tok_o = users_store.create_user("oi38-other", name="Other")
    _, tok_a = users_store.create_user("oi38-adm", name="Admin", role="admin")
    return {"prop": tok_p, "other": tok_o, "adm": tok_a}


@pytest.fixture
def mem():
    _ensure_table()
    rid = _insert(
        {
            "data": "fact worth proposing",
            "user_id": "oi38-prop",
            "updated_at": "2026-11-22T00:00:00+00:00",
        }
    )
    yield rid
    conn = __import__("psycopg2").connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute("DELETE FROM mem0 WHERE id = %s", (rid,))
    finally:
        conn.close()


def test_propose_sets_state_and_provenance(server, users, mem):
    r = req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    assert r.status_code == 200 and r.json()["proposed"] is True
    p = _payload(mem)
    assert p["share_status"] == "proposed"
    assert p["proposed_by"] == "oi38-prop"
    assert p["proposed_at"] and p["proposed_stamp"]
    # idempotent
    r2 = req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    assert r2.json()["already_proposed"] is True


def test_foreign_propose_is_404(server, users, mem):
    r = req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["other"])
    assert r.status_code == 404
    assert "share_status" not in _payload(mem)


def test_withdraw_clears(server, users, mem):
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    r = req(server, "POST", f"/api/memories/{mem}/withdraw-share", token=users["prop"])
    assert r.json()["withdrawn"] is True
    p = _payload(mem)
    assert p["share_status"] == "none" and "proposed_by" not in p
    # withdraw without an open proposal -> 409
    r2 = req(server, "POST", f"/api/memories/{mem}/withdraw-share", token=users["prop"])
    assert r2.status_code == 409


def test_worklist_admin_only_and_content(server, users, mem):
    r = req(server, "GET", "/api/share-proposals", token=users["prop"])
    assert r.status_code == 403  # before any read
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    rows = req(server, "GET", "/api/share-proposals", token=users["adm"]).json()
    hit = next(x for x in rows if x["id"] == mem)
    assert hit["memory"] == "fact worth proposing"
    assert hit["tenant"] == "oi38-prop" and hit["proposed_by"] == "oi38-prop"
    # status filter
    assert (
        req(
            server, "GET", "/api/share-proposals?status=rejected", token=users["adm"]
        ).json()
        == []
    )
    assert (
        req(
            server, "GET", "/api/share-proposals?status=bogus", token=users["adm"]
        ).status_code
        == 400
    )


def test_approve_moves_with_audit(server, users, mem):
    other = _insert({"data": "other tenant fact", "user_id": "oi38-other"})
    try:
        req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
        r = req(
            server, "POST", f"/api/share-proposals/{mem}/approve", token=users["adm"]
        )
        assert r.status_code == 200 and r.json()["shared"] is True
        p = _payload(mem)
        assert p["user_id"] == "global"
        assert p["shared_by"] == "oi38-adm" and p["approved_by"] == "oi38-adm"
        assert p["approved_at"] and p["original_tenant"] == "oi38-prop"
        assert (
            p["share_status"] == "none" and p["proposed_by"] == "oi38-prop"
        )  # history kept
        # visible to another tenant via the shared space (keyword path)
        import memory as mem_store

        hits = mem_store.keyword_hits("proposing", user_id="oi38-other", limit=10)
        assert mem in [str(h["id"]) for h in hits]
        # approving again -> already
        r2 = req(
            server, "POST", f"/api/share-proposals/{mem}/approve", token=users["adm"]
        )
        assert r2.json()["already_shared"] is True
    finally:
        conn = __import__("psycopg2").connect(DB_URL)
        with conn:
            conn.cursor().execute("DELETE FROM mem0 WHERE id = %s", (other,))


def test_approve_refuses_edited_content(server, users, mem):
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    # simulate the owner editing after proposing: new content hash
    p = _payload(mem)
    p["data"] = "edited after proposing"
    p["hash"] = "deadbeef"
    conn = __import__("psycopg2").connect(DB_URL)
    with conn:
        conn.cursor().execute(
            "UPDATE mem0 SET payload = %s WHERE id = %s", (json.dumps(p), mem)
        )
    r = req(server, "POST", f"/api/share-proposals/{mem}/approve", token=users["adm"])
    assert r.status_code == 409 and "changed since proposal" in r.text
    assert _payload(mem)["user_id"] == "oi38-prop"  # not moved


def test_reject_keeps_memory_and_allows_repropose(server, users, mem):
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    r = req(
        server,
        "POST",
        f"/api/share-proposals/{mem}/reject",
        token=users["adm"],
        json={"reason": "duplicate of an existing shared fact"},
    )
    assert r.json()["rejected"] is True
    p = _payload(mem)
    assert p["share_status"] == "rejected"
    assert p["rejected_by"] == "oi38-adm" and p["rejected_reason"].startswith(
        "duplicate"
    )
    assert p["user_id"] == "oi38-prop"  # untouched workspace
    # approve after reject -> 409
    assert (
        req(
            server, "POST", f"/api/share-proposals/{mem}/approve", token=users["adm"]
        ).status_code
        == 409
    )
    # owner re-proposes: rejected fields cleared (chip disappears)
    r2 = req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    assert r2.json()["proposed"] is True
    p2 = _payload(mem)
    assert p2["share_status"] == "proposed" and "rejected_reason" not in p2


def test_rate_limit_20_open(server, users, mem):
    _ensure_table()
    # a prior crashed run must not pre-load the quota (mem re-inserted after)
    conn = __import__("psycopg2").connect(DB_URL)
    with conn:
        conn.cursor().execute(
            "DELETE FROM mem0 WHERE payload->>'user_id' = 'oi38-prop'"
        )
        conn.cursor().execute(
            "INSERT INTO mem0 (id, embedding, payload) VALUES (%s, %s::vector, %s)",
            (
                mem,
                "[" + ",".join(["0.1"] * 1536) + "]",
                json.dumps(
                    {
                        "data": "fact worth proposing",
                        "user_id": "oi38-prop",
                        "hash": mem[:8],
                        "text_lemmatized": "fact worth proposing",
                    }
                ),
            ),
        )
    conn.close()
    ids = [
        _insert({"data": f"bulk fact {i}", "user_id": "oi38-prop"}) for i in range(19)
    ]
    try:
        # mem + 19 = 20 open proposals
        for rid in ids:
            assert (
                req(
                    server,
                    "POST",
                    f"/api/memories/{rid}/propose-share",
                    token=users["prop"],
                ).status_code
                == 200
            )
        assert (
            req(
                server,
                "POST",
                f"/api/memories/{mem}/propose-share",
                token=users["prop"],
            ).status_code
            == 200
        )
        extra = _insert({"data": "one too many", "user_id": "oi38-prop"})
        try:
            r = req(
                server,
                "POST",
                f"/api/memories/{extra}/propose-share",
                token=users["prop"],
            )
            assert r.status_code == 429 and "max 20" in r.text
        finally:
            conn = __import__("psycopg2").connect(DB_URL)
            with conn:
                conn.cursor().execute("DELETE FROM mem0 WHERE id = %s", (extra,))
    finally:
        conn = __import__("psycopg2").connect(DB_URL)
        with conn:
            conn.cursor().execute(
                "DELETE FROM mem0 WHERE id::text = ANY(%s)", ([*ids, mem],)
            )
    # mem deleted here too; the mem fixture's cleanup is a no-op then


def test_search_rows_carry_share_status(server, fake_mem, users, mem):
    # The list/semantic paths read mem0 (faked in tests), so real-DB proposal
    # fields ride the keyword path: the UI chips read them off rows whose
    # payload was just UPDATEd by the proposal endpoints themselves.
    p = _payload(mem)
    p["data"] = "fact worth proposing chip marker"
    p["text_lemmatized"] = "fact worth proposing chip marker"
    conn = __import__("psycopg2").connect(DB_URL)
    with conn:
        conn.cursor().execute(
            "UPDATE mem0 SET payload = %s WHERE id = %s", (json.dumps(p), mem)
        )
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    rows = req(
        server, "GET", "/api/memories?q=chip&hybrid=1", token=users["prop"]
    ).json()
    hit = next(x for x in rows if x["id"] == mem)
    assert hit["share_status"] == "proposed"
    req(
        server,
        "POST",
        f"/api/share-proposals/{mem}/reject",
        token=users["adm"],
        json={"reason": "not useful"},
    )
    rows = req(
        server, "GET", "/api/memories?q=chip&hybrid=1", token=users["prop"]
    ).json()
    hit = next(x for x in rows if x["id"] == mem)
    assert (hit["share_status"], hit["rejected_reason"]) == ("rejected", "not useful")


def test_shared_row_shares_no_proposal_state(server, users, mem):
    # an approved row must not leak 'proposed'/'rejected' state anywhere:
    # direct SQL (list path source) and the keyword search shape
    req(server, "POST", f"/api/memories/{mem}/propose-share", token=users["prop"])
    r = req(server, "POST", f"/api/share-proposals/{mem}/approve", token=users["adm"])
    assert r.json()["shared"] is True
    p = _payload(mem)
    assert p["share_status"] == "none" and "rejected_reason" not in p
    import memory as mem_store

    hits = mem_store.keyword_hits("proposing", user_id="oi38-prop", limit=10)
    hit = next(h for h in hits if str(h["id"]) == mem)
    assert hit["share_status"] == "none" and not hit["rejected_reason"]


def test_approve_unknown_and_malformed_id_never_500(server, users):
    _ensure_table()  # CI without pgvector has no mem0 table -> 503, skip
    r = req(
        server,
        "POST",
        f"/api/share-proposals/{uuid.uuid4()}/approve",
        token=users["adm"],
    )
    assert r.status_code == 404
    r2 = req(
        server, "POST", "/api/share-proposals/not-a-uuid/approve", token=users["adm"]
    )
    assert r2.status_code == 404  # malformed id behaves like missing, never 500
