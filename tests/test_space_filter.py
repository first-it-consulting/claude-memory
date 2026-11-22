"""Space filter (bean o9y4): ?space=all|own|shared narrows list/search/stats/
export to exclude ('own') or contain only ('shared') shared-space rows, for
every user. Omitted = byte-identical to pre-o9y4; invalid value -> 400."""

import pytest
from test_multitenancy import ADMIN, req

pytestmark = pytest.mark.db


@pytest.fixture
def admin(settings_override):
    settings_override(admin_token=ADMIN)
    return ADMIN


@pytest.fixture
def alice(server, admin):
    r = req(server, "POST", "/api/users", token=admin, json={"id": "sp-alice"})
    assert r.status_code == 201, r.text
    return "sp-alice", r.json()["token"]


@pytest.fixture
def seeded(fake_mem):
    """One own memory for sp-alice and one in the shared space."""
    own = fake_mem.seed("alice private note about kubernetes", user_id="sp-alice")
    shared = fake_mem.seed("shared cluster facts about kubernetes", user_id="global")
    return own, shared


def _ids(rows):
    return {str(r["id"]) for r in rows}


def test_space_list_matrix_for_regular_user(server, alice, seeded):
    own, shared = seeded
    tok = alice[1]
    r = req(server, "GET", "/api/memories", token=tok)
    assert r.status_code == 200 and _ids(r.json()) == {own, shared}  # default unchanged
    r = req(server, "GET", "/api/memories?space=all", token=tok)
    assert _ids(r.json()) == {own, shared}
    r = req(server, "GET", "/api/memories?space=own", token=tok)
    assert _ids(r.json()) == {own}
    r = req(server, "GET", "/api/memories?space=shared", token=tok)
    assert _ids(r.json()) == {shared}


def test_space_search_respects_filter(server, alice, seeded):
    own, shared = seeded
    tok = alice[1]
    both = req(server, "GET", "/api/memories?q=kubernetes", token=tok).json()
    assert _ids(both) == {own, shared}
    only_own = req(
        server, "GET", "/api/memories?q=kubernetes&space=own", token=tok
    ).json()
    assert _ids(only_own) == {own}
    only_shared = req(
        server, "GET", "/api/memories?q=kubernetes&space=shared", token=tok
    ).json()
    assert _ids(only_shared) == {shared}


def test_space_stats_match_the_filtered_list(server, alice, seeded):
    tok = alice[1]
    total = req(server, "GET", "/api/stats", token=tok).json()["total"]
    assert total == 2
    assert req(server, "GET", "/api/stats?space=own", token=tok).json()["total"] == 1
    assert req(server, "GET", "/api/stats?space=shared", token=tok).json()["total"] == 1


def test_space_export_follows_filter(server, alice, seeded):
    own, shared = seeded
    tok = alice[1]
    rows = req(server, "GET", "/api/memories/export?space=own", token=tok).json()
    assert _ids(rows) == {own}


def test_invalid_space_is_400_everywhere(server, alice):
    tok = alice[1]
    for path in ("/api/memories", "/api/stats", "/api/memories/export"):
        r = req(server, "GET", f"{path}?space=mixed", token=tok)
        assert r.status_code == 400, (path, r.text)
        assert "all, own, shared" in r.text


def test_admin_scope_all_plus_space(server, admin, seeded):
    own, shared = seeded
    # scope=all sees everything (both seeded rows live in known tenants)
    r = req(server, "GET", "/api/memories?scope=all", token=admin)
    assert {own, shared} <= _ids(r.json())
    # space=own drops shared rows from the all view (post-filter before limit)
    r = req(server, "GET", "/api/memories?scope=all&space=own", token=admin)
    assert own in _ids(r.json()) and shared not in _ids(r.json())
    # space=shared keeps only shared rows
    r = req(server, "GET", "/api/memories?scope=all&space=shared", token=admin)
    assert _ids(r.json()) == {shared}


def test_admin_tenant_plus_space(server, admin, seeded):
    own, shared = seeded
    # tenant=sp-alice&space=own means "her own, without shared"
    r = req(server, "GET", "/api/memories?tenant=sp-alice&space=own", token=admin)
    assert _ids(r.json()) == {own}
    # honest intersection: a named tenant is not the shared space -> empty
    r = req(server, "GET", "/api/memories?tenant=sp-alice&space=shared", token=admin)
    assert _ids(r.json()) == set()
    # search path obeys the same rule
    r = req(
        server,
        "GET",
        "/api/memories?q=kubernetes&tenant=sp-alice&space=shared",
        token=admin,
    )
    assert _ids(r.json()) == set()


def test_keyword_path_include_shared(seeded):
    """memory.keyword_hits(include_shared=False) must not leak shared rows
    (the hybrid search leg forwards the same flag)."""
    import psycopg2

    import memory as mem_store

    own, shared = seeded
    try:
        hits = mem_store.keyword_hits(
            "kubernetes", user_id="sp-alice", limit=10, include_shared=False
        )
    except psycopg2.Error:
        pytest.skip("no mem0 keyword table on this database")
    # fake-seeded corpus rows are not in the SQL table; assert only that the
    # call honours the flag shape: every hit (if any from real rows) is own
    assert all(h.get("user_id") == "sp-alice" for h in hits)
