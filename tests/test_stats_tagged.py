"""GET /api/stats: 'tagged' must count DISTINCT memories with >=1 tag.

Regression: the UI derived 'tagged memories' from the sum of by_tag
occurrences, so a memory with several tags counted multiple times and
'untagged' (total - tagged) could go negative (observed live: total 229,
tagged 1096, untagged -867).
"""

import requests
from conftest import TOKEN


def _stats(server, params=None, token=TOKEN):
    r = requests.get(
        server + "/api/stats",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    assert r.status_code == 200
    return r.json()


def test_tagged_is_distinct_not_occurrences(server):
    # CORPUS: 3 memories; entry 1 carries TWO tags, entry 2 one, entry 3 none.
    s = _stats(server)
    assert s["total"] == 3
    assert sum(s["by_tag"].values()) == 3  # occurrences
    assert s["tagged"] == 2  # distinct memories with >=1 tag
    assert s["total"] - s["tagged"] == 1  # untagged stays >= 0


def test_tagged_multi_tag_memory_counted_once(server, fake_mem):
    fake_mem.seed(
        "multi-tag note",
        user_id="default",
        metadata={"metadata": {"tags": ["a", "b", "c", "d"]}},
    )
    s = _stats(server)
    assert s["total"] == 4
    assert s["tagged"] == 3
    assert s["total"] - s["tagged"] == 1


def test_tagged_respects_workspace_scope(server, fake_mem):
    # A memory in another tenant must not leak into this caller's stats.
    fake_mem.seed("other tenant", user_id="other", metadata={})
    s = _stats(server)  # TOKEN is tenant 'default', non-admin
    assert s["total"] == 3
    assert s["tagged"] == 2
