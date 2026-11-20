"""PATs (api_tokens): mint, route, resolve, revoke, expiry, admin view."""
import psycopg2
import pytest

import tokens as tok_mod
import users as users_store
from conftest import DB_URL, TOKEN

pytestmark = pytest.mark.db


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def user_with_token():
    users_store.ensure_table()
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id LIKE 'pat-%'")
                cur.execute("DELETE FROM api_tokens WHERE user_id LIKE 'pat-%'")
    finally:
        conn.close()
    u, cm = users_store.create_user("pat-u1", name="Pat One")
    a, cm_admin = users_store.create_user("pat-admin", name="Pat Admin", role="admin")
    return {"u1": cm, "admin": cm_admin}


def whoami(server, tok):
    import requests
    r = requests.get(server + "/api/auth/whoami", headers=_auth(tok), timeout=20)
    return r


def test_pat_mint_use_list_revoke(server, user_with_token):
    # per-user legacy cm_ token authenticates as the row (role user)
    w = whoami(server, user_with_token["u1"]).json()
    assert w["user_id"] == "pat-u1" and w["via"] == "token" and w["role"] == "user"

    # mint a PAT through the self-service endpoint
    import requests
    r = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["u1"]),
                      json={"name": "mac-hook", "expires_days": 30}, timeout=20)
    body = r.json()
    assert r.status_code == 200 and body["ok"]
    pat = body["token"]
    assert pat.startswith("cm_t") and pat.count("_") == 2

    # the PAT resolves and whoami carries token context
    w = whoami(server, pat).json()
    assert w["user_id"] == "pat-u1" and w["role"] == "user"
    assert w["token_name"] == "mac-hook"
    assert 28 <= w["token_expires_in_days"] <= 30

    # listing is masked — no hash, no plaintext
    lst = requests.get(server + "/api/me/tokens", headers=_auth(pat), timeout=20).json()["tokens"]
    mine = [t for t in lst if t["id"] == body["id"]][0]
    assert "…" in mine["display"] and pat not in str(mine) and "token_hash" not in mine

    # revoke, then it must not authenticate
    d = requests.delete(server + f"/api/me/tokens/{body['id']}",
                        headers=_auth(user_with_token["u1"]), timeout=20)
    assert d.json()["ok"] is True
    assert whoami(server, pat).status_code == 401


def test_can_admin_semantics(server, user_with_token):
    import requests
    # non-admin cannot mint an admin token
    r = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["u1"]),
                      json={"name": "sneaky", "can_admin": True}, timeout=20).json()
    w = whoami(server, r["token"]).json()
    assert w["role"] == "user"

    # admin row + can_admin PAT -> admin; without can_admin -> user (new semantics)
    r2 = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["admin"]),
                       json={"name": "ops", "can_admin": True}, timeout=20).json()
    assert whoami(server, r2["token"]).json()["role"] == "admin"
    r3 = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["admin"]),
                       json={"name": "plain"}, timeout=20).json()
    assert whoami(server, r3["token"]).json()["role"] == "user"


def test_expiry_rejects(server, user_with_token):
    import requests
    r = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["u1"]),
                      json={"name": "short"}, timeout=20).json()
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE api_tokens SET expires_at = now() - interval '1 hour' "
                            "WHERE id = %s", (r["id"],))
    finally:
        conn.close()
    assert whoami(server, r["token"]).status_code == 401


def test_disable_user_revokes_pats(server, user_with_token):
    import requests
    r = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["u1"]),
                      json={"name": "doomed"}, timeout=20).json()
    users_store.update_user("pat-u1", disabled=True)
    assert whoami(server, r["token"]).status_code == 401
    # and the legacy per-user cm_ token is dead too (pre-existing behaviour)
    assert whoami(server, user_with_token["u1"]).status_code == 401


def test_admin_view_and_revoke_any(server, user_with_token):
    import requests
    # legacy bearer (role user) gets 403 on the admin view
    assert requests.get(server + "/api/admin/tokens", headers=_auth(TOKEN),
                        timeout=20).status_code == 403
    # an admin PAT sees everything masked and can revoke any token
    r2 = requests.post(server + "/api/me/tokens", headers=_auth(user_with_token["admin"]),
                       json={"name": "audit", "can_admin": True}, timeout=20).json()
    lst = requests.get(server + "/api/admin/tokens?user=pat-u1",
                       headers=_auth(r2["token"]), timeout=20).json()["tokens"]
    assert lst and all(t["user_id"] == "pat-u1" for t in lst)
    victim = lst[0]["id"]
    assert requests.delete(server + f"/api/admin/tokens/{victim}",
                           headers=_auth(r2["token"]), timeout=20).json()["ok"] is True


def test_backfill_import_idempotent(server, user_with_token):
    # ensure_table imported the per-user legacy hashes once
    tok_mod.ensure_table()
    before = tok_mod.list_all("pat-u1")
    tok_mod.ensure_table()  # second run must not duplicate
    after = tok_mod.list_all("pat-u1")
    assert len(before) == len(after)
    assert any(t["name"] == "imported-legacy" for t in before)
    # the legacy cm_ token still authenticates exactly as before
    assert whoami(server, user_with_token["u1"]).status_code == 200
