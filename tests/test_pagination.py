"""Paging (bean pj1k): GET /api/memories gains OPTIONAL ?limit/?offset.
Without them the response is the plain JSON array as before (byte-identical
for MCP/REST clients); with them it is {"memories": [...], "total": N}
where total counts the FULL filtered set. Sort/filter/space semantics are
untouched - paging slices what they produced."""

import pytest
from test_multitenancy import ADMIN, req

pytestmark = pytest.mark.db

N = 7


@pytest.fixture
def admin(settings_override):
    settings_override(admin_token=ADMIN)
    return ADMIN


@pytest.fixture
def alice(server, admin):
    r = req(server, "POST", "/api/users", token=admin, json={"id": "pj-alice"})
    assert r.status_code == 201, r.text
    return "pj-alice", r.json()["token"]


@pytest.fixture
def seeded(fake_mem):
    ids = [
        fake_mem.seed(f"paged fact number {i} about paging", user_id="pj-alice")
        for i in range(N)
    ]
    return ids


def _ids(rows):
    return [str(r["id"]) for r in rows]


def test_no_params_response_unchanged_plain_array(server, alice, seeded):
    tok = alice[1]
    r = req(server, "GET", "/api/memories", token=tok)
    assert r.status_code == 200
    assert isinstance(r.json(), list)  # plain array, no envelope
    assert len(r.json()) == N


def test_limit_wraps_envelope_with_total(server, alice, seeded):
    tok = alice[1]
    r = req(server, "GET", "/api/memories?limit=3", token=tok)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"memories", "total"}
    assert body["total"] == N  # total counts the full filtered set
    assert len(body["memories"]) == 3


def test_pages_walk_all_rows_without_overlap(server, alice, seeded):
    tok = alice[1]
    seen = []
    offset = 0
    while True:
        body = req(
            server, "GET", f"/api/memories?limit=3&offset={offset}", token=tok
        ).json()
        page = _ids(body["memories"])
        assert body["total"] == N
        seen += page
        if len(page) < 3:
            break
        offset += 3
    assert sorted(seen) == sorted(str(i) for i in seeded)  # complete, no dupes


def test_offset_beyond_total_returns_empty_page(server, alice, seeded):
    tok = alice[1]
    body = req(server, "GET", "/api/memories?limit=3&offset=99", token=tok).json()
    assert body["memories"] == []
    assert body["total"] == N


def test_limit_zero_with_offset_returns_rest(server, alice, seeded):
    tok = alice[1]
    body = req(server, "GET", "/api/memories?limit=0&offset=5", token=tok).json()
    assert body["total"] == N
    assert len(body["memories"]) == 2


def test_negative_limit_or_offset_is_400(server, alice, seeded):
    tok = alice[1]
    for q in ("limit=-1", "offset=-2"):
        r = req(server, "GET", f"/api/memories?{q}", token=tok)
        assert r.status_code == 400, r.text
        assert "error" in r.json()


def test_paging_applies_after_search_and_filters(server, alice, seeded):
    tok = alice[1]
    # search narrows first: total reflects the search result, not everything
    body = req(server, "GET", "/api/memories?q=paging&limit=2", token=tok).json()
    assert body["total"] == N  # all seeded texts mention "paging"
    assert len(body["memories"]) == 2
    # and after a category filter (none of the seeds carry one -> empty)
    body = req(
        server, "GET", "/api/memories?category=nonexistent&limit=2", token=tok
    ).json()
    assert body["total"] == 0
    assert body["memories"] == []


def test_space_filter_and_paging_compose(server, admin, alice, fake_mem):
    tok = alice[1]
    fake_mem.seed("shared paging fact", user_id="global")
    for i in range(3):
        fake_mem.seed(f"pj own fact {i}", user_id="pj-alice")
    body = req(server, "GET", "/api/memories?space=own&limit=2", token=tok).json()
    assert body["total"] == 3
    assert all(m["tenant"] == "pj-alice" for m in body["memories"])
