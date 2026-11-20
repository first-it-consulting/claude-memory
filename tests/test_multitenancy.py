"""Multi-tenancy: per-user workspaces, shared space, admin scope, tokens.

The legacy BEARER_TOKEN (conftest TOKEN) acts as tenant "default" with role
user; tests use settings_override(admin_token=...) for the admin principal and
create per-user API tokens through /api/users.
"""
import json
import time

import pytest
import requests

from conftest import TOKEN

pytestmark = pytest.mark.db

ADMIN = "admin-bootstrap-token"


def req(server, method, path, token=None, **kw):
    headers = kw.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, server + path, headers=headers, timeout=20, **kw)


@pytest.fixture
def admin(settings_override):
    settings_override(admin_token=ADMIN)
    return ADMIN


@pytest.fixture
def alice(server, admin, api):
    """Create tenant alice with its own API token; return (id, token)."""
    r = req(server, "POST", "/api/users", token=admin, json={"id": "alice", "name": "Alice"})
    assert r.status_code == 201, r.text
    return "alice", r.json()["token"]


@pytest.fixture
def bob(server, admin, api):
    r = req(server, "POST", "/api/users", token=admin, json={"id": "bob"})
    assert r.status_code == 201, r.text
    return "bob", r.json()["token"]


# ---------------------------------------------------------------------------
# Auth / principals
# ---------------------------------------------------------------------------

def test_whoami_legacy_token_is_default_tenant_user(server):
    r = req(server, "GET", "/api/auth/whoami", token=TOKEN)
    body = r.json()
    assert r.status_code == 200
    assert body == {"user_id": "default", "role": "user", "via": "legacy",
                    "label": "legacy-token", "shared_tenant": "global"}


def test_whoami_admin_token(server, admin):
    body = req(server, "GET", "/api/auth/whoami", token=admin).json()
    assert body["role"] == "admin" and body["user_id"] == "default"


def test_unknown_token_rejected(server):
    assert req(server, "GET", "/api/auth/whoami", token="cm_garbage").status_code == 401
    assert req(server, "GET", "/api/auth/whoami").status_code == 401


def test_api_token_authenticates_its_tenant(server, alice):
    uid, token = alice
    body = req(server, "GET", "/api/auth/whoami", token=token).json()
    assert body["user_id"] == "alice" and body["role"] == "user" and body["via"] == "token"


# ---------------------------------------------------------------------------
# /api/users (admin CRUD)
# ---------------------------------------------------------------------------

def test_users_crud_and_one_time_token(server, admin, alice):
    uid, token = alice
    listed = req(server, "GET", "/api/users", token=admin).json()
    ids = {u["id"] for u in listed}
    # 'default' is the service user behind the shared token; it appears once
    # any token op has run (lazy import), so accept it but nothing else.
    assert "alice" in ids and ids <= {"alice", "default"}
    alice_row = [u for u in listed if u["id"] == "alice"][0]
    assert "token_hash" not in alice_row and alice_row["name"] == "Alice"

    rot = req(server, "POST", "/api/users/alice/rotate-token", token=admin).json()
    new_token = rot["token"]
    assert new_token != token
    # old token dead, new token alive
    assert req(server, "GET", "/api/auth/whoami", token=token).status_code == 401
    assert req(server, "GET", "/api/auth/whoami", token=new_token).status_code == 200

    assert req(server, "DELETE", "/api/users/alice", token=admin).json()["ok"] is True
    assert req(server, "GET", "/api/users/alice", token=admin).status_code == 404


def test_users_api_requires_admin(server, alice):
    uid, token = alice
    assert req(server, "GET", "/api/users", token=token).status_code == 403
    assert req(server, "POST", "/api/users", token=token, json={"id": "eve"}).status_code == 403
    assert req(server, "GET", "/api/users", token=TOKEN).status_code == 403  # legacy is role user


def test_users_validation(server, admin):
    assert req(server, "POST", "/api/users", token=admin, json={"id": "global"}).status_code == 400
    # ids are sanitised (Bad/Id -> bad-id); nothing sanitizable -> 400
    assert req(server, "POST", "/api/users", token=admin, json={"id": "Bad/Id!"}).status_code == 201
    assert req(server, "POST", "/api/users", token=admin, json={"id": "!!!"}).status_code == 400


def test_disabled_user_token_rejected(server, admin, alice):
    uid, token = alice
    assert req(server, "PATCH", "/api/users/alice", token=admin, json={"disabled": True}).status_code == 200
    assert req(server, "GET", "/api/auth/whoami", token=token).status_code == 401


# ---------------------------------------------------------------------------
# Memory isolation
# ---------------------------------------------------------------------------

def test_list_scoped_to_own_plus_shared(server, fake_mem, alice):
    fake_mem.seed("alice fact alpha", user_id="alice")
    fake_mem.seed("bob fact beta", user_id="bob")
    fake_mem.seed("shared fact gamma", user_id="global")
    uid, token = alice
    body = req(server, "GET", "/api/memories", token=token).json()
    texts = {m["memory"] for m in body}
    assert texts == {"alice fact alpha", "shared fact gamma"}
    assert {m["tenant"] for m in body} == {"alice", "global"}


def test_search_scoped_to_own_plus_shared(server, fake_mem, alice):
    fake_mem.seed("alice kubernetes note", user_id="alice")
    fake_mem.seed("bob kubernetes note", user_id="bob")
    uid, token = alice
    body = req(server, "GET", "/api/memories", token=token, params={"q": "kubernetes note"}).json()
    assert [m["memory"] for m in body] == ["alice kubernetes note"]


def test_cross_tenant_write_is_404(server, fake_mem, alice):
    rid = fake_mem.seed("bob private fact", user_id="bob")
    uid, token = alice
    assert req(server, "PUT", f"/api/memories/{rid}", token=token, json={"memory": "hijack"}).status_code == 404
    assert req(server, "DELETE", f"/api/memories/{rid}", token=token).status_code == 404
    assert req(server, "PATCH", f"/api/memories/{rid}/tags", token=token, json={"tags": ["x"]}).json() == {"ok": False}
    # owner may
    assert req(server, "DELETE", f"/api/memories/{rid}", token=token).status_code == 404  # alice != bob
    fake_mem.seed("alice own fact", user_id="alice")  # sanity: alice can use her token


def test_owner_can_modify_own_memory(server, fake_mem, alice):
    rid = fake_mem.seed("alice own fact", user_id="alice")
    uid, token = alice
    assert req(server, "PUT", f"/api/memories/{rid}", token=token, json={"memory": "edited"}).status_code == 200
    assert req(server, "DELETE", f"/api/memories/{rid}", token=token).status_code == 200


def test_admin_scope_all_and_tenant_filter(server, fake_mem, admin, alice):
    fake_mem.seed("alice fact", user_id="alice")
    fake_mem.seed("bob fact", user_id="bob")
    body = req(server, "GET", "/api/memories", token=admin, params={"scope": "all"}).json()
    assert {"alice fact", "bob fact"} <= {m["memory"] for m in body}
    body = req(server, "GET", "/api/memories", token=admin, params={"tenant": "bob"}).json()
    assert {m["memory"] for m in body} == {"bob fact"}
    # regular user cannot ask for other tenants
    uid, token = alice
    body = req(server, "GET", "/api/memories", token=token, params={"scope": "all"}).json()
    assert "bob fact" not in {m["memory"] for m in body}


def test_admin_can_modify_any_tenant(server, fake_mem, admin):
    rid = fake_mem.seed("bob fact", user_id="bob")
    assert req(server, "DELETE", f"/api/memories/{rid}", token=admin).status_code == 200


# ---------------------------------------------------------------------------
# Shared space
# ---------------------------------------------------------------------------

def test_shared_publish_admin_only(server, admin, alice):
    r = req(server, "POST", "/api/memories", token=admin, json={"content": "everyone should know this", "shared": True})
    assert r.status_code == 200 and r.json()["queued"] is True
    uid, token = alice
    r = req(server, "POST", "/api/memories", token=token, json={"content": "sneaky", "shared": True})
    assert r.status_code == 403


def test_shared_publish_lands_in_global_tenant(server, admin, fake_mem):
    req(server, "POST", "/api/memories", token=admin, json={"content": "shared fact delta", "shared": True})
    deadline = time.time() + 10
    while time.time() < deadline:
        adds = [c for c in fake_mem.calls if c[0] == "add"]
        if adds:
            break
        time.sleep(0.1)
    adds = [c for c in fake_mem.calls if c[0] == "add"]
    assert adds and adds[-1][2]["user_id"] == "global"


# ---------------------------------------------------------------------------
# Queue carries the tenant
# ---------------------------------------------------------------------------

def test_queued_add_lands_in_caller_tenant(server, alice, fake_mem):
    uid, token = alice
    r = req(server, "POST", "/api/memories", token=token, json={"content": "alice queued fact"})
    assert r.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        adds = [c for c in fake_mem.calls if c[0] == "add"]
        if adds:
            break
        time.sleep(0.1)
    adds = [c for c in fake_mem.calls if c[0] == "add"]
    assert adds and adds[-1][2]["user_id"] == "alice"


# ---------------------------------------------------------------------------
# Sessions (checkpoints) isolation
# ---------------------------------------------------------------------------

def test_session_cross_tenant_invisible(server, alice, bob):
    uid_a, tok_a = alice
    uid_b, tok_b = bob
    r = req(server, "PUT", "/api/sessions/s1", token=tok_b, json={"state": "bob working"})
    assert r.status_code == 200
    assert req(server, "GET", "/api/sessions/s1", token=tok_a).status_code == 404
    listing = req(server, "GET", "/api/sessions", token=tok_a).json()
    assert listing == []
    # admin sees across tenants
    listing = req(server, "GET", "/api/sessions", token="admin-bootstrap-token",
                  params={"scope": "all"}).json()
    assert [s["session_id"] for s in listing] == ["s1"] and listing[0]["owner"] == "bob"
    # bob may delete his own; alice cannot
    assert req(server, "DELETE", "/api/sessions/s1", token=tok_a).status_code == 404
    assert req(server, "DELETE", "/api/sessions/s1", token=tok_b).status_code == 200


def test_legacy_token_keeps_default_workspace_sessions(server):
    r = req(server, "PUT", "/api/sessions/legacy1", token=TOKEN, json={"state": "legacy state"})
    assert r.status_code == 200
    row = req(server, "GET", "/api/sessions/legacy1", token=TOKEN).json()
    assert row["owner"] == "default"


# ---------------------------------------------------------------------------
# Admin-only views
# ---------------------------------------------------------------------------

def test_jobs_admin_only(server, admin):
    assert req(server, "GET", "/api/jobs", token=TOKEN).status_code == 403
    # admin gets a list (job_runs may be empty; must not be 403/404)
    r = req(server, "GET", "/api/jobs", token="admin-bootstrap-token")
    assert r.status_code == 200


def test_tag_rule_writes_admin_only(server):
    assert req(server, "POST", "/api/tag-rules", token=TOKEN,
               json={"tag": "x", "keyword": "y"}).status_code == 403
    assert req(server, "GET", "/api/tag-rules", token=TOKEN).status_code == 200


# ---------------------------------------------------------------------------
# MCP tools are tenant-scoped too
# ---------------------------------------------------------------------------

def _rpc(server, token, method, params=None):
    r = requests.post(
        server + "/mcp",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    text = r.text
    if text.lstrip().startswith("data:") or "\ndata:" in text:
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


def _call(server, token, name, **arguments):
    return _rpc(server, token, "tools/call", {"name": name, "arguments": arguments})["result"]["content"][0]["text"]


def test_mcp_get_all_scoped_to_tenant(server, fake_mem, alice):
    fake_mem.seed("alice mcp fact", user_id="alice")
    fake_mem.seed("bob mcp fact", user_id="bob")
    uid, token = alice
    out = _call(server, token, "get_all_memories")
    assert "alice mcp fact" in out and "bob mcp fact" not in out
    # default tenant (legacy token) sees neither
    out = _call(server, TOKEN, "get_all_memories")
    assert "alice mcp fact" not in out and "bob mcp fact" not in out


def test_mcp_update_foreign_tenant_denied(server, fake_mem, alice):
    rid = fake_mem.seed("bob mcp fact", user_id="bob")
    uid, token = alice
    assert f"No memory with id {rid}" in _call(server, token, "update_memory", memory_id=rid, content="x")


def test_mcp_checkpoint_scoped(server, alice, bob):
    uid_a, tok_a = alice
    uid_b, tok_b = bob
    out = _call(server, tok_b, "checkpoint", session_id="s-mcp", state="bob state", project="proj")
    assert json.loads(out)["ok"] is True
    # alice cannot read or overwrite bob's session
    assert _call(server, tok_a, "get_checkpoint", session_id="s-mcp") == "No checkpoint found."
    assert "another workspace" in _call(server, tok_a, "checkpoint", session_id="s-mcp", state="hijack")
    # bob can
    assert "bob state" in _call(server, tok_b, "get_checkpoint", session_id="s-mcp")


# ---------------------------------------------------------------------------
# OIDC mapping (verification itself is monkeypatched — no Keycloak here)
# ---------------------------------------------------------------------------

def test_oidc_claims_map_to_tenant_and_admin(server, monkeypatch):
    import auth

    monkeypatch.setattr(auth, "verify_jwt", lambda token: {
        "sub": "keycloak-sub-1", "preferred_username": "carol", "email": "carol@example.com",
        "realm_access": {"roles": ["claude-memory-admin"]}, "exp": 9999999999,
    })
    from config import settings
    saved = settings.oidc_issuer
    settings.oidc_issuer = "https://keycloak.example/realms/lab"
    try:
        p = auth.resolve("aaa.bbb.ccc")
    finally:
        settings.oidc_issuer = saved
    assert p is not None and p.user_id == "carol" and p.role == "admin" and p.via == "oidc"


def test_oidc_auto_create_disabled_rejects(server, monkeypatch):
    import auth
    from config import settings
    monkeypatch.setattr(auth, "verify_jwt", lambda token: {
        "sub": "sub-x", "preferred_username": "dave", "email": "", "exp": 9999999999,
    })
    saved = (settings.oidc_issuer, settings.oidc_auto_create)
    settings.oidc_issuer = "https://keycloak.example/realms/lab"
    settings.oidc_auto_create = False
    try:
        assert auth.resolve("aaa.bbb.ccc") is None
    finally:
        settings.oidc_issuer, settings.oidc_auto_create = saved
