"""Fresh-database pgvector catalog race (bean v4al).

On a brand-new DB the first concurrent writes collided inside CREATE
EXTENSION vector (Postgres: duplicate key pg_type_typname_nsp_index) and the
queue job was dropped. Fixes: serial priming in _ensure_schema at startup,
plus one retry in the worker for that exact error.
"""

import psycopg2
import pytest
from conftest import DB_URL

pytestmark = pytest.mark.db


def _ext_exists():
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            return cur.fetchone() is not None
    finally:
        conn.close()


def _ext_available():
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'")
            return cur.fetchone() is not None
    finally:
        conn.close()


def test_startup_primes_vector_extension(caplog):
    """_ensure_schema must create the vector extension when it is missing -
    that is what removes the concurrent-creation race. On test databases
    without the pgvector binaries (plain postgres:16) the priming must instead
    degrade to a logged skip without aborting schema setup. The real
    pgvector-image coverage is the concurrent first-write smoke test."""
    import logging

    import memory

    if not _ext_available():
        memory._ensure_schema()  # must not raise
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_tables WHERE tablename='tag_rules'")
                assert cur.fetchone()  # schema setup continued past the skip
        return

    conn = psycopg2.connect(DB_URL)
    with conn:
        conn.cursor().execute("DROP EXTENSION IF EXISTS vector CASCADE")
    assert not _ext_exists()
    with caplog.at_level(logging.WARNING, logger="memory"):
        memory._ensure_schema()
    assert _ext_exists()


def test_worker_retries_catalog_race(server, api, fake_mem, monkeypatch):
    """A first write that dies with pg_type_typname is retried once and the
    memory is stored; other errors are still dropped, not retried."""
    calls = {"n": 0}
    real_add = fake_mem.add

    def flaky_add(content, *a, **kw):
        # stragglers from earlier tests must not consume the simulated race
        if "v4al race probe" in str(content):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    'duplicate key value violates unique constraint "pg_type_typname_nsp_index"'
                )
        return real_add(content, *a, **kw)

    monkeypatch.setattr(fake_mem, "add", flaky_add)
    # MCP add_memory honours infer=false (REST POST always queues with
    # infer=True), so exactly one worker add call per job here.
    from test_mcp_tools import call_tool

    out = call_tool(server, "add_memory", content="v4al race probe", infer=False)
    assert "queued" in out
    import time

    for _ in range(40):
        if any(c[0] == "add" and "v4al race probe" in c[1] for c in fake_mem.calls):
            break
        time.sleep(0.5)
    else:
        pytest.fail("job was dropped instead of retried")
    assert calls["n"] == 2  # first attempt raced, retry stored it


def test_worker_does_not_retry_other_errors(server, fake_mem, monkeypatch):
    calls = {"n": 0}

    def bad_add(content, *a, **kw):
        if "v4al other err" in str(content):
            calls["n"] += 1
            raise RuntimeError("some other permanent failure")
        return fake_mem.calls  # harmless pass-through for straggler jobs

    monkeypatch.setattr(fake_mem, "add", bad_add)
    from test_mcp_tools import call_tool

    call_tool(server, "add_memory", content="v4al other err", infer=False)
    import time

    time.sleep(3.0)  # enough for a (wrong) retry to have happened
    assert calls["n"] == 1  # non-catalog errors are dropped, not retried
