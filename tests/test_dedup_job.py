"""Nightly dedup job: clustering, merge-fidelity guard, and that originals are
only deleted once their replacement is stored. No database needed."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "scripts"))
import dedup_job as d


class PairsCursor:
    def __init__(self, pairs):
        self.pairs = pairs

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self.pairs


def test_clusters_pairs_and_chains():
    a, b, c, x, y = (uuid.uuid4() for _ in range(5))
    clusters = d.find_semantic_clusters(PairsCursor([(a, b, .99), (b, c, .95), (x, y, .93)]), .92)
    assert sorted(sorted(map(str, cl)) for cl in clusters) == sorted(
        [sorted(map(str, (a, b, c))), sorted(map(str, (x, y)))])


def test_no_pairs_no_clusters():
    assert d.find_semantic_clusters(PairsCursor([]), .92) == []


def test_covering_text_picks_superset():
    texts = ["Backups run daily at 03:00", "Backups run daily at 03:00 via restic to TrueNAS"]
    assert d.covering_text(texts) == 1
    assert d.covering_text(["oauth2-proxy 7.7.28 outdated", "metallb 0.14.9 outdated"]) is None


def test_merge_rejected_when_versions_change():
    texts = ["oauth2-proxy 7.7.28->10.7.4 (MAJOR) outdated", "longhorn 1.12.0->1.13.0 outdated"]
    assert not d.merge_is_faithful("Upgraded oauth2-proxy to 10.7.4 and longhorn to 1.13.0", texts)
    assert d.merge_is_faithful(
        "Outdated: oauth2-proxy 7.7.28->10.7.4 (MAJOR), longhorn 1.12.0->1.13.0", texts)


def test_fact_tokens_cover_paths_and_times():
    t = d.fact_tokens("Config in ~/.config/hlw/config.json, backups at 03:00.")
    assert "03:00" in t and any("hlw/config.json" in x for x in t)


# --- phase 2 against a fake connection -------------------------------------

class FakeConn:
    """Holds rows {id: payload}; records archive/delete statements."""

    def __init__(self, rows, pairs):
        self.rows, self.pairs = rows, pairs
        self.deleted, self.archived = [], []

    def cursor(self, cursor_factory=None):
        return FakeCur(self)

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back = True


class FakeCur:
    def __init__(self, conn):
        self.c, self._res = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO job_runs"):
            self._res = [(uuid.uuid4(),)]
        elif "a.vector <=> b.vector" in s:
            self._res = self.c.pairs
        elif s.startswith("SELECT id, payload FROM"):
            ids = params[0]
            # psycopg2 returns the uuid column as str here (seen in production)
            self._res = [{"id": str(i), "payload": self.c.rows[i]} for i in ids if i in self.c.rows]
        elif s.startswith("INSERT INTO mem0_archive"):
            self.c.archived.append(params[2])
        elif s.startswith("DELETE FROM"):
            assert all(isinstance(i, uuid.UUID) for i in params[0]), "id = ANY(text[]) fails in postgres"
            if getattr(self.c, "fail_delete", False):
                raise RuntimeError("delete failed")
            self.c.deleted += list(params[0])
        else:
            self._res = []

    def fetchone(self):
        return self._res[0]

    def fetchall(self):
        return self._res


def _two(t1, t2):
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = {a: {"data": t1, "agent_id": "codex", "metadata": {"tags": ["x"]}},
            b: {"data": t2, "agent_id": "codex", "metadata": {"tags": ["y"]}}}
    return a, b, FakeConn(rows, [(a, b, .95)])


def test_superset_kept_verbatim_without_llm(monkeypatch):
    a, b, conn = _two("Backups run daily at 03:00", "Backups run daily at 03:00 via restic")
    monkeypatch.setattr(d, "ollama_merge", lambda t: pytest.fail("LLM must not be called"))
    assert d.phase2_semantic_dedup(conn, .92, dry_run=False) == 1
    assert conn.deleted == [a]  # UUID, not str


def test_unfaithful_merge_keeps_originals(monkeypatch):
    a, b, conn = _two("oauth2-proxy 7.7.28->10.7.4 outdated", "metallb 0.14.9->6.4.22 outdated")
    monkeypatch.setattr(d, "ollama_merge", lambda t: "Upgraded oauth2-proxy and metallb")
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: pytest.fail("must not store"))
    assert d.phase2_semantic_dedup(conn, .92, dry_run=False) == 0
    assert conn.deleted == []


def test_store_failure_keeps_originals(monkeypatch):
    a, b, conn = _two("oauth2-proxy 7.7.28 outdated", "metallb 0.14.9 outdated")
    monkeypatch.setattr(d, "ollama_merge", lambda t: "Outdated: oauth2-proxy 7.7.28, metallb 0.14.9")
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: None)
    assert d.phase2_semantic_dedup(conn, .92, dry_run=False) == 0
    assert conn.deleted == []


def test_faithful_merge_replaces_originals(monkeypatch):
    a, b, conn = _two("oauth2-proxy 7.7.28 outdated", "metallb 0.14.9 outdated")
    new_id = str(uuid.uuid4())
    seen = {}
    monkeypatch.setattr(d, "ollama_merge", lambda t: "Outdated: oauth2-proxy 7.7.28, metallb 0.14.9")

    def store(content, **kw):
        seen.update(kw, content=content)
        return new_id

    monkeypatch.setattr(d, "store_memory_direct", store)
    assert d.phase2_semantic_dedup(conn, .92, dry_run=False) == 2
    assert sorted(map(str, conn.deleted)) == sorted(map(str, (a, b)))
    assert seen["agent_id"] == "codex" and seen["tags"] == ["x", "y"]


def test_delete_failure_removes_stored_merge(monkeypatch):
    a, b, conn = _two("oauth2-proxy 7.7.28 outdated", "metallb 0.14.9 outdated")
    conn.fail_delete = True
    removed = []
    monkeypatch.setattr(d, "ollama_merge", lambda t: "Outdated: oauth2-proxy 7.7.28, metallb 0.14.9")
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: str(uuid.uuid4()))
    monkeypatch.setattr(d, "delete_memory_direct", removed.append)
    assert d.phase2_semantic_dedup(conn, .92, dry_run=False) == 0
    assert len(removed) == 1 and conn.rolled_back


def test_strip_preamble():
    assert d._strip_preamble("Memory:\n- a 1\n- b 2") == "- a 1\n- b 2"
    assert d._strip_preamble("**Merged Memory:**\nx 1") == "x 1"
    assert d._strip_preamble("Here is the merged memory:\nx 1") == "x 1"
    assert d._strip_preamble("Memory usage on node4 is 10g") == "Memory usage on node4 is 10g"
    assert d._strip_preamble("- a 1\n  - (no specific date or name mentioned in both memories)") == "- a 1"
    assert d._strip_preamble("node4 (TrueNAS VM 114) OOM") == "node4 (TrueNAS VM 114) OOM"
