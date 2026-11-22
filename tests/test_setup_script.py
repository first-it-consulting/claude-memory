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
from conftest import TOKEN

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
