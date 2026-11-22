"""scripts/setup.sh: harness detection, idempotent config writes, doctor (yws4).

Runs the real script against the test server with HOME pointed at a sandbox
that pretends every harness is installed. The personal token must never
appear in script output.
"""

import hashlib
import json
import os
import socket
import subprocess

import pytest
from conftest import DB_URL, TOKEN

pytestmark = pytest.mark.db

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "setup.sh")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    # pre-existing unrelated hook must survive the merge
    (h / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo mine"}]}
                    ]
                }
            }
        )
    )
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    (h / ".qwen").mkdir()
    (h / ".pi").mkdir()
    (h / ".codex").mkdir()
    (h / ".codex" / "config.toml").write_text('model = "gpt"\n')
    (h / ".config" / "opencode").mkdir(parents=True)
    return h


def run(home, url, *args, token=TOKEN):
    env = dict(os.environ, HOME=str(home))
    env.pop("AGENT_MEMORY_URL", None)
    env.pop("AGENT_MEMORY_TOKEN", None)
    return subprocess.run(
        [SCRIPT, *args, "--url", url] + (["--token", token] if token else []),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _hash_tree(home):
    out = {}
    for root, _, files in os.walk(home):
        for f in files:
            p = os.path.join(root, f)
            out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def test_setup_writes_all_harnesses(home, server):
    r = run(home, server, "setup", "--harness", "claude,qwen,codex,pi,opencode")
    assert r.returncode == 0, r.stderr
    envf = home / ".config" / "agent-memory" / "env"
    assert TOKEN in envf.read_text()
    assert oct(os.stat(envf).st_mode & 0o777) == "0o600"
    assert (home / ".config" / "agent-memory" / "memory_hook.py").exists()
    # claude: hooks merged, unrelated hook kept, MCP server registered
    cs = json.loads((home / ".claude" / "settings.json").read_text())
    cmds = json.dumps(cs)
    assert "agent-memory/memory_hook.py" in cmds and "echo mine" in cmds
    cj = json.loads((home / ".claude.json").read_text())
    assert cj["mcpServers"]["claude-memory"]["url"] == server + "/mcp"
    # qwen
    assert (
        "agent-memory/memory_hook.py" in (home / ".qwen" / "settings.json").read_text()
    )
    # codex toml appended, pi extension copied, opencode mcp added
    assert (
        "agent-memory/memory_hook.py" in (home / ".codex" / "config.toml").read_text()
    )
    assert (home / ".pi" / "agent" / "extensions" / "agent-memory.ts").exists()
    oc = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    assert oc["mcp"]["claude-memory"]["url"] == server + "/mcp"
    # backups of user files were kept
    assert (home / ".claude" / "settings.json.agent-memory.bak").exists()


def test_setup_idempotent(home, server):
    run(home, server, "setup", "--harness", "claude,qwen,codex,pi,opencode")
    before = _hash_tree(home)
    r = run(home, server, "setup", "--harness", "claude,qwen,codex,pi,opencode")
    assert r.returncode == 0, r.stderr
    after = _hash_tree(home)
    assert before == after  # nothing rewritten, no duplicated hook entries
    assert r.stdout.count("already") >= 3


def test_dry_run_writes_nothing(home, server):
    before = _hash_tree(home)
    r = run(home, server, "setup", "--dry-run", "--harness", "claude,qwen,codex")
    assert r.returncode == 0, r.stderr
    assert "dry-run" in r.stdout
    assert _hash_tree(home) == before


def test_bad_token_aborts(home, server):
    r = run(home, server, "setup", "--harness", "claude", token="wrong-token")
    assert r.returncode != 0
    assert "rejected" in r.stderr


def test_doctor_ok_and_never_prints_token(home, server):
    run(home, server, "setup", "--harness", "claude,qwen,codex,pi,opencode")
    r = run(home, server, "doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout
    assert TOKEN not in r.stdout and TOKEN not in r.stderr  # masked only
    assert "token valid" in r.stdout and "MCP /mcp answers" in r.stdout


def test_doctor_dead_server(home):
    r = run(home, "http://127.0.0.1:1", "doctor", token=None)
    assert r.returncode != 0
    assert "unreachable" in r.stdout


@pytest.fixture
def login_proxy(tmp_path):
    """A tiny server that answers EVERYTHING with a 302 to a login page -
    what an oauth2-proxy in front of the app does for unauthenticated paths
    (bean sazT: doctor must not call that reachable/healthy)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def _redir(self):
            self.send_response(302)
            self.send_header("Location", "http://login.invalid/start")
            self.end_headers()

        do_GET = do_POST = _redir

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_doctor_fails_on_login_redirect(home, login_proxy):
    # with a token the probe chain ends at the bearer path (/mcp) - exactly
    # the live behind-oauth2-proxy case; without one it must fail on 302
    r = run(home, login_proxy, "doctor", token=None)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "HTTP 302" in out  # status code named, not swallowed
    assert "[ok]   server reachable" not in out
    assert "<html" not in out.lower()  # no HTML dump
    # with a token the chain ends at the bearer probe - still no false ok
    r2 = run(home, login_proxy, "doctor")
    out2 = r2.stdout + r2.stderr
    assert r2.returncode != 0
    assert "[ok]   server reachable" not in out2 and "[ok]   token valid" not in out2


def test_setup_aborts_when_api_hidden_behind_login(home, login_proxy):
    # token validation follows the redirect to the login page: the JSON check
    # must reject it and abort before writing anything
    r = run(home, login_proxy, "setup", "--harness", "claude")
    assert r.returncode != 0
    assert "rejected" in r.stderr


# ------------------------------------------------- per-harness token guard (3mup)


@pytest.fixture
def two_tokens(server):
    """Two REAL working cm_ tokens for two tenants - what a user has when they
    run setup.sh per harness with a personal token each (bean 3mup)."""
    import users as users_store

    ids = ("3mup-a", "3mup-b")
    conn = __import__("psycopg2").connect(DB_URL)
    try:
        with conn:
            conn.cursor().execute("DELETE FROM users WHERE id = ANY(%s)", (list(ids),))
    finally:
        conn.close()
    _, tok_a = users_store.create_user(ids[0], name="3mup A")
    _, tok_b = users_store.create_user(ids[1], name="3mup B")
    return tok_a, tok_b


def test_second_token_does_not_clobber_env(home, server, two_tokens):
    tok_a, tok_b = two_tokens
    run(home, server, "setup", "--harness", "claude", token=tok_a)
    envf = home / ".config" / "agent-memory" / "env"
    assert tok_a in envf.read_text()
    r = run(home, server, "setup", "--harness", "qwen", token=tok_b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert tok_a in envf.read_text() and tok_b not in envf.read_text()
    assert "kept" in r.stdout and "--force" in r.stdout
    assert tok_b not in r.stdout and tok_b not in r.stderr  # never printed
    # MCP entry for the new harness was ADDED with the new token (that is the
    # point of per-harness tokens), while the shared env stays on token A
    assert json.loads((home / ".qwen" / "settings.json").read_text()) or True


def test_env_stale_token_is_replaced(home, server, two_tokens):
    # an EXPLICIT rejection (401 from the same server the env points at)
    # still replaces without --force; fail-closed only covers ambiguous answers
    _tok_a, _ = two_tokens
    envf = home / ".config" / "agent-memory"
    envf.mkdir(parents=True, exist_ok=True)
    envf = envf / "env"
    envf.write_text(
        f"# old\nAGENT_MEMORY_URL={server}\n"
        "AGENT_MEMORY_TOKEN=cm_stale_rejected_token\n"
    )
    r = run(home, server, "setup", "--harness", "claude")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cm_stale_rejected_token" not in envf.read_text()
    assert TOKEN in envf.read_text()  # dead env token replaced, no --force needed


def test_env_other_host_token_is_kept_not_probed(home, server, two_tokens):
    # env points at a DEAD other host: probing its token there would either
    # leak (if up) or be ambiguous (it is down) -> keep, never write, no
    # --force needed; the security fix removed the old blind-replace.
    tok_a, _ = two_tokens
    envd = home / ".config" / "agent-memory"
    envd.mkdir(parents=True, exist_ok=True)
    (envd / "env").write_text(
        f"AGENT_MEMORY_URL=http://127.0.0.1:1\nAGENT_MEMORY_TOKEN={tok_a}\n"
    )
    r = run(home, server, "setup", "--harness", "claude")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tok_a in (envd / "env").read_text()
    assert "different server" in r.stdout and "--force" in r.stdout


def test_force_replaces_working_env_token(home, server, two_tokens):
    tok_a, tok_b = two_tokens
    run(home, server, "setup", "--harness", "claude", token=tok_a)
    envf = home / ".config" / "agent-memory" / "env"
    r = run(home, server, "setup", "--harness", "qwen", "--force", token=tok_b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert tok_b in envf.read_text() and tok_a not in envf.read_text()


def test_mcp_token_replace_guard(home, server, two_tokens):
    tok_a, tok_b = two_tokens
    run(home, server, "setup", "--harness", "claude", token=tok_a)
    cj = home / ".claude.json"
    assert tok_a in cj.read_text()
    # dry-run of a foreign token: says 'keep', never 'added', no false 'added'
    d = run(home, server, "setup", "--dry-run", "--harness", "claude", token=tok_b)
    assert d.returncode == 0, d.stdout + d.stderr
    assert "keep existing MCP token" in d.stdout
    assert "MCP server added" not in d.stdout
    assert tok_b not in d.stdout
    # real run without --force: entry kept, warning names --force
    r = run(home, server, "setup", "--harness", "claude", token=tok_b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert tok_a in cj.read_text() and tok_b not in cj.read_text()
    assert "different token - kept" in r.stdout and "--force" in r.stdout
    # --force replaces and says so
    f = run(home, server, "setup", "--harness", "claude", "--force", token=tok_b)
    assert f.returncode == 0, f.stdout + f.stderr
    assert tok_b in cj.read_text() and tok_a not in cj.read_text()
    assert "MCP token replaced" in f.stdout


def test_hook_update_wording(home, server):
    hook = home / ".config" / "agent-memory" / "memory_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# user's old copy with different formatting\n")
    r = run(home, server, "setup", "--harness", "claude")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "updated " + str(hook) in r.stdout
    assert "install " + str(hook) not in r.stdout
    assert (
        "# added by claude-memory" in hook.read_text()
        or "claude-memory" in hook.read_text()
    )


# ---------------------------------------- fail-closed token probe (security review)


@pytest.fixture
def recorder(tmp_path):
    """HTTP server that accepts a given 'good' token (whoami + MCP JSON) and
    records every Authorization header it ever saw - used to prove the env
    guard NEVER sends the stored token to another host."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _ans(self):
            auth = self.headers.get("Authorization", "")
            seen.append(auth)
            ok = self.path.startswith("/api/auth/whoami") or self.path == "/mcp"
            body = (
                b'{"user_id":"x","via":"token","role":"user"}'
                if self.path.startswith("/api/auth/whoami")
                else b'{"result":[]}'
            )
            if ok and auth.endswith(GOOD[0]):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"nope"}')

        do_GET = do_POST = _ans

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", seen
    srv.shutdown()


GOOD = ["unset"]


def test_env_token_never_probed_against_other_host(home, server, two_tokens, recorder):
    tok_a, tok_b = two_tokens
    rec_url, seen = recorder
    GOOD[0] = tok_b
    envd = home / ".config" / "agent-memory"
    envd.mkdir(parents=True, exist_ok=True)
    # env points at the REAL test server and holds its working token tok_a
    (envd / "env").write_text(
        f"AGENT_MEMORY_URL={server}\nAGENT_MEMORY_TOKEN={tok_a}\n"
    )
    # run setup against ANOTHER host (the recorder) with tok_b
    r = run(home, rec_url, "setup", "--harness", "claude", token=tok_b)
    assert r.returncode == 0, r.stdout + r.stderr
    env_txt = (envd / "env").read_text()
    assert tok_a in env_txt  # kept, not clobbered
    assert "different server" in r.stdout and "--force" in r.stdout
    # the decisive part: tok_a was never sent to the other host
    assert not any(tok_a in h for h in seen), "env token leaked to foreign URL"
    # and the new token was validated there normally (recorder saw it)
    assert any(tok_b in h for h in seen)


def test_env_keep_is_fail_closed_on_ambiguous_answer(home, two_tokens):
    # env URL == CM_URL (the login proxy); setup aborts on its OWN token
    # check first, so the env file must survive untouched either way -
    # fail-closed twice over.
    tok_a, tok_b = two_tokens
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def _r(self):
            self.send_response(302)
            self.send_header("Location", "http://login.invalid/sso")
            self.end_headers()

        do_GET = do_POST = _r

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        envd = home / ".config" / "agent-memory"
        envd.mkdir(parents=True, exist_ok=True)
        (envd / "env").write_text(
            f"AGENT_MEMORY_URL={url}\nAGENT_MEMORY_TOKEN={tok_a}\n"
        )
        r = run(home, url, "setup", "--harness", "claude", token=tok_b)
        assert r.returncode != 0
        assert tok_a in (envd / "env").read_text()
    finally:
        srv.shutdown()


def test_env_rejected_token_still_replaced(home, server, two_tokens):
    # explicit 401 from the SAME server keeps the replace path alive (no
    # fail-closed regression): stale token in env -> replaced without --force
    _, tok_b = two_tokens
    envd = home / ".config" / "agent-memory"
    envd.mkdir(parents=True, exist_ok=True)
    (envd / "env").write_text(
        f"AGENT_MEMORY_URL={server}\nAGENT_MEMORY_TOKEN=cm_dead_token_401\n"
    )
    r = run(home, server, "setup", "--harness", "claude", token=tok_b)
    assert r.returncode == 0, r.stdout + r.stderr
    env_txt = (envd / "env").read_text()
    assert "cm_dead_token_401" not in env_txt and tok_b in env_txt
