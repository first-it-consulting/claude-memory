"""Token-efficient search: index mode, fetch-by-ids, timeline (bean rcos).

Backwards compatibility matters: every existing default path must be
untouched; the new modes are opt-in per request.
"""

import pytest
import requests
from conftest import TOKEN

pytestmark = pytest.mark.db

CORPUS_ID_1 = "11111111-1111-1111-1111-111111111111"
CORPUS_ID_2 = "22222222-2222-2222-2222-222222222222"


def _get(server, params, token=TOKEN):
    r = requests.get(
        server + "/api/memories",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    assert r.status_code == 200, r.text
    return r


def test_default_search_unchanged(server):
    body = _get(server, {"q": "kyverno"}).json()
    assert body and "memory" in body[0]  # full records as before


def test_index_rows_are_compact(server):
    full = _get(server, {"q": "kyverno"}).json()
    idx = _get(server, {"q": "kyverno", "format": "index"}).json()
    assert idx
    row = idx[0]
    assert set(row) == {
        "id",
        "snippet",
        "score",
        "date",
        "category",
        "tags",
        "est_tokens_full",
    }
    assert len(row["snippet"]) <= 143  # 140 + ellipsis
    assert row["est_tokens_full"] >= 1
    # index response must be materially smaller than the full one
    assert len(str(idx)) < len(str(full))


def test_fetch_by_ids_roundtrip(server):
    recs = _get(server, {"ids": f"{CORPUS_ID_1},{CORPUS_ID_2}"}).json()
    assert {r["id"] for r in recs} == {CORPUS_ID_1, CORPUS_ID_2}
    assert "barman" in recs[0]["memory"].lower()  # full text returned


def test_fetch_by_ids_skips_foreign(server, fake_mem):
    fake_mem.seed("other tenant secret", user_id="other", metadata={})
    other = [r for r in fake_mem.corpus if r.get("user_id") == "other" and r.get("id")]
    if not other:
        # fake store assigns ids on seed; fall back to listing via admin-less path
        pytest.skip("fake seed id not exposed")
    recs = _get(server, {"ids": other[0]["id"]}).json()
    assert recs == []  # foreign id indistinguishable from missing


def test_timeline(server):
    r = requests.get(
        server + f"/api/memories/timeline/{CORPUS_ID_2}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params={"window": 5},
        timeout=20,
    )
    assert r.status_code == 200
    tl = r.json()
    assert tl["self"]["id"] == CORPUS_ID_2
    # CORPUS created_at order: entry1 < entry2 < entry3 -> entry1 before
    assert any(x["id"] == CORPUS_ID_1 for x in tl["before"])


def test_timeline_unknown_404(server):
    r = requests.get(
        server + "/api/memories/timeline/does-not-exist",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=20,
    )
    assert r.status_code == 404


def _mcp_text(server, name, **args):
    from test_mcp_tools import call_tool

    return call_tool(server, name, **args)


def test_mcp_index_mode_and_fetch(server):
    out = _mcp_text(server, "search_memory", query="kyverno", mode="index")
    assert "tok full" in out and "fetch_memories" in out
    out_full = _mcp_text(server, "search_memory", query="kyverno")
    assert "tok full" not in out_full  # default unchanged
    fetched = _mcp_text(server, "fetch_memories", ids=f"{CORPUS_ID_1},{CORPUS_ID_2}")
    assert "barman" in fetched.lower()


def test_mcp_timeline(server):
    out = _mcp_text(server, "memory_timeline", id=CORPUS_ID_2)
    assert ">>> " in out
    assert CORPUS_ID_1 in out  # earlier entry in 'before'
    assert _mcp_text(server, "memory_timeline", id="nope") == (
        "Memory not found (or not visible to you)."
    )
