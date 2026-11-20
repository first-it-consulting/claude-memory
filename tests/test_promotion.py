"""Checkpoint promotion: parsing of Decisions / Dead ends and the nightly phase
(dry-run vs on, duplicates, failures). No database needed."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "scripts"))
import dedup_job as d
import promotion as p

CODEX_STATE = """## Goal
Update outdated packages in the chart repo.

## Done
- Updated image tag 8.0.39 -> 8.0.42

## Decisions
- Removed kubeval from CI due to SSL cert issues with kubernetesjsonschema.dev
- Bumped chart version to 0.2.0
- Fixed release workflow: git config must come before tag creation,
  tag_name must be explicit

## Key files
- .github/workflows/ci.yml is the pipeline definition for this repository
"""

QWEN_STATE = """### Goal
Analyse the repo.

**Dead ends:**
1. Digest-pinning images failed because registry GC removed untagged multi-arch children
2. none

### Decisions (+ why)
* Use sed for the bulk edit for now, revisit later when there is time for it
"""


def test_extracts_only_decisions_and_dead_ends():
    got = p.extract(CODEX_STATE)
    assert [k for k, _ in got] == ["decision"] * 3
    assert got[2][1] == ("Fixed release workflow: git config must come before tag creation, "
                         "tag_name must be explicit")
    assert all("ci.yml" not in t for _, t in got)


def test_bold_and_numbered_headings_and_kinds():
    got = p.extract(QWEN_STATE)
    assert got[0] == ("dead-end", "Digest-pinning images failed because registry GC removed untagged multi-arch children")
    assert ("dead-end", "none") in got
    assert got[-1][0] == "decision"


def test_keep_filters_short_and_session_only_lines():
    kept, dropped = p.candidates(QWEN_STATE + CODEX_STATE, "example-project", "Qwen")
    texts = [c["text"] for c in kept]
    assert any(t.startswith("Digest-pinning") for t in texts)
    assert not any("for now" in t for t in texts)       # session-only
    assert not any(t == "Bumped chart version to 0.2.0" for t in texts)  # too short
    assert ("dead-end", "none") in dropped


def test_content_and_tags():
    kept, _ = p.candidates(CODEX_STATE, "mysql-galera", "codex")
    c = kept[0]
    assert c["content"] == "Decision (mysql-galera): Removed kubeval from CI due to SSL cert issues with kubernetesjsonschema.dev"
    assert c["tags"] == ["auto-promoted", "codex", "decision", "mysql-galera"]
    assert p.content_for("dead-end", "x " * 400, "")[:10] == "Dead end: "
    assert len(p.content_for("decision", "y" * 1000, "p")) < 640


def test_no_sections_no_candidates():
    assert p.candidates("## Goal\nfoo\n## Done\n- a long line that is not a decision at all, really", "p", "h") == ([], [])


# --- phase against a fake connection -------------------------------------------

class Conn:
    def __init__(self, sessions):
        self.sessions = sessions
        self.marked, self.runs = [], {}

    def cursor(self, cursor_factory=None):
        return Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class Cur:
    def __init__(self, c):
        self.c, self._res = c, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO job_runs"):
            self._res = [("run-1",)]
        elif s.startswith("UPDATE job_runs"):
            self.c.runs["details"], self.c.runs["status"] = params[0], params[1]
            self.c.runs["changed"], self.c.runs["error"] = params[3], params[5]
        elif s.startswith("SELECT session_id"):
            self._res = self.c.sessions
        elif s.startswith("UPDATE session_state SET promoted_at"):
            self.c.marked.append(params[0])

    def fetchone(self):
        return self._res[0]

    def fetchall(self):
        return self._res


def _sessions():
    return [
        {"session_id": "s1", "project": "mysql-galera", "harness": "codex", "state": CODEX_STATE},
        {"session_id": "s2", "project": "example-project", "harness": "qwen", "state": QWEN_STATE},
        {"session_id": "s3", "project": "x", "harness": "pi", "state": "## Goal\nnothing to promote here"},
    ]


@pytest.fixture
def no_dups(monkeypatch):
    monkeypatch.setattr(d, "find_duplicate", lambda text, thr, user_id=None: None)


def test_dry_run_reports_without_storing(monkeypatch, no_dups):
    conn = Conn(_sessions())
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: pytest.fail("dry run must not store"))
    n = d.phase_promote_checkpoints(conn, dry_run=False, mode="dry-run", idle_hours=12)
    assert n == 3
    assert conn.marked == []                       # sessions stay pending
    assert conn.runs["details"].startswith("DRY RUN")
    assert "WOULD PROMOTE: Decision (mysql-galera): Removed kubeval" in conn.runs["details"]


def test_on_stores_tags_metadata_and_marks(monkeypatch, no_dups):
    conn, stored = Conn(_sessions()), []

    def store(content, **kw):
        stored.append((content, kw))
        return f"id-{len(stored)}"

    monkeypatch.setattr(d, "store_memory_direct", store)
    assert d.phase_promote_checkpoints(conn, dry_run=False, mode="on") == 3
    assert conn.marked == ["s1", "s2", "s3"]
    content, kw = stored[0]
    assert kw["agent_id"] == "codex" and kw["category"] == "project"
    assert "auto-promoted" in kw["tags"]
    assert kw["extra_metadata"] == {"source": "checkpoint", "session_id": "s1"}


def test_global_dry_run_flag_wins(monkeypatch, no_dups):
    conn = Conn(_sessions())
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: pytest.fail("must not store"))
    d.phase_promote_checkpoints(conn, dry_run=True, mode="on")
    assert conn.marked == []


def test_known_lines_and_repeats_are_skipped(monkeypatch):
    sessions = _sessions() + [{"session_id": "s4", "project": "mysql-galera", "harness": "qwen", "state": CODEX_STATE}]
    conn, stored = Conn(sessions), []
    monkeypatch.setattr(d, "find_duplicate", lambda text, thr, user_id=None: 0.97 if "kubeval" in text else None)
    monkeypatch.setattr(d, "store_memory_direct", lambda c, **k: stored.append(c) or "id")
    d.phase_promote_checkpoints(conn, dry_run=False, mode="on")
    assert not any("kubeval" in c for c in stored)
    assert sum("release workflow" in c for c in stored) == 1   # s4 repeats s1
    assert "already known, 0.97" in conn.runs["details"]


def test_store_failure_fails_phase_and_keeps_session_pending(monkeypatch, no_dups):
    conn = Conn(_sessions())
    monkeypatch.setattr(d, "store_memory_direct", lambda *a, **k: None)
    assert d.phase_promote_checkpoints(conn, dry_run=False, mode="on") == 0
    assert conn.runs["status"] == "failed" and "storing failed" in conn.runs["error"]
    assert "s1" not in conn.marked


def test_off_does_nothing():
    conn = Conn(_sessions())
    assert d.phase_promote_checkpoints(conn, dry_run=False, mode="off") == 0
    assert conn.runs == {}


# --- recall ranking ------------------------------------------------------------

def test_auto_promoted_ranks_below_deliberate():
    sys.path.insert(0, str(ROOT / "src"))
    import main
    deliberate = {"score": 0.80, "metadata": {"tags": ["ci"]}}
    promoted = {"score": 0.80, "metadata": {"metadata": {"tags": ["auto-promoted", "decision"]}}}
    assert main._rank_score(deliberate) == 0.80
    assert main._rank_score(promoted) == pytest.approx(0.80 - main.settings.promoted_recall_penalty)
    assert main._rank_score({"score": None}) is None
