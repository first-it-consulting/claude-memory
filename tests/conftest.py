"""Test fixtures.

The suite runs the real ASGI app (uvicorn in a background thread) against a
real PostgreSQL, with mem0 replaced by an in-process fake (no pgvector/Ollama
needed). Point TEST_DATABASE_URL at a disposable database, e.g.

    docker run --rm -d -p 5432:5432 -e POSTGRES_USER=mem0 -e POSTGRES_PASSWORD=mem0 \
        -e POSTGRES_DB=mem0_test postgres:16
    TEST_DATABASE_URL=postgresql://mem0:mem0@localhost:5432/mem0_test pytest

Without TEST_DATABASE_URL the DB-backed tests are skipped — unless
REQUIRE_DB=1 (set in CI), which turns the skip into a failure.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
HOOK_SCRIPT = ROOT / "adapters" / "hooks" / "memory_hook.py"
TOKEN = "test-token"

DB_URL = os.environ.get("TEST_DATABASE_URL", "")

# Settings are read at import time — configure the environment first.
os.environ["DATABASE_URL"] = DB_URL or "postgresql://invalid@127.0.0.1:1/none"
os.environ["BEARER_TOKEN"] = TOKEN
os.environ["REDIS_URL"] = ""
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _db_reachable() -> bool:
    if not DB_URL:
        return False
    try:
        import psycopg2

        psycopg2.connect(DB_URL, connect_timeout=3).close()
        return True
    except Exception:
        return False


DB_OK = _db_reachable()


def pytest_collection_modifyitems(config, items):
    if DB_OK:
        return
    reason = "TEST_DATABASE_URL not set or database unreachable"
    if os.environ.get("REQUIRE_DB") == "1":
        raise pytest.UsageError(f"REQUIRE_DB=1 but {reason}")
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs TEST_DATABASE_URL (real PostgreSQL)")


@pytest.fixture(scope="session")
def fake_mem():
    from fakes import FakeMemory

    import memory as mem_store

    fake = FakeMemory()
    mem_store.get_memory = lambda: fake
    return fake


@pytest.fixture(autouse=True)
def _reset_fake(request):
    if "fake_mem" in request.fixturenames or "server" in request.fixturenames:
        fake = request.getfixturevalue("fake_mem")
        fake.reset()
    yield


@pytest.fixture(scope="session")
def server(fake_mem):
    """Base URL of a running claude-memory server (real app, fake mem0)."""
    if not DB_OK:
        pytest.skip("database unavailable")
    import uvicorn

    import main

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(
        main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_sessions(request):
    """Every DB test starts with empty session_state and users tables."""
    if "db" in request.keywords and DB_OK:
        import psycopg2

        import memory as mem_store
        import users as users_store

        mem_store._ensure_session_table()
        users_store.ensure_table()
        conn = psycopg2.connect(DB_URL)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("TRUNCATE session_state, users")
        finally:
            conn.close()
    yield


@pytest.fixture
def settings_override():
    """Temporarily change server settings (the test server shares this process)."""
    from config import settings

    saved = {}

    def _set(**kw):
        for k, v in kw.items():
            saved.setdefault(k, getattr(settings, k))
            setattr(settings, k, v)

    yield _set
    for k, v in saved.items():
        setattr(settings, k, v)


@pytest.fixture
def api(server):
    """Tiny HTTP client: api.get(path, **params), api.put(path, json), api.post(...)."""
    import requests

    class Api:
        base = server
        headers = {"Authorization": f"Bearer {TOKEN}"}

        def get(self, path, auth=True, **params):
            return requests.get(
                server + path,
                params=params,
                headers=self.headers if auth else {},
                timeout=20,
            )

        def put(self, path, json=None, headers=None, **kw):
            return requests.put(
                server + path,
                json=json,
                headers={**self.headers, **(headers or {})},
                timeout=20,
                **kw,
            )

        def post(self, path, json=None, headers=None, **kw):
            return requests.post(
                server + path,
                json=json,
                headers={**self.headers, **(headers or {})},
                timeout=20,
                **kw,
            )

        def delete(self, path, auth=True):
            return requests.delete(
                server + path, headers=self.headers if auth else {}, timeout=20
            )

    return Api()
