"""REST: /api/sessions — checkpoint storage."""

import pytest

pytestmark = pytest.mark.db


def test_put_and_get_roundtrip(api):
    r = api.put(
        "/api/sessions/s1",
        {"state": "## Goal\nShip it", "project": "p1", "harness": "claude"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert (
        body["session_id"] == "s1"
        and body["project"] == "p1"
        and body["harness"] == "claude"
    )

    got = api.get("/api/sessions/s1").json()
    assert got["state"] == "## Goal\nShip it"
    assert got["auto_notes"] == ""


def test_put_overwrites_state_but_keeps_project_and_harness(api):
    api.put("/api/sessions/s1", {"state": "v1", "project": "p1", "harness": "claude"})
    first = api.get("/api/sessions/s1").json()
    api.put("/api/sessions/s1", {"state": "v2"})  # no project/harness sent
    got = api.get("/api/sessions/s1").json()
    assert got["state"] == "v2"
    assert got["project"] == "p1"
    assert got["harness"] == "claude"
    assert got["updated_at"] >= first["updated_at"]
    assert got["created_at"] == first["created_at"]


def test_harness_falls_back_to_agent_header(api):
    r = api.put("/api/sessions/s1", {"state": "x"}, headers={"X-Agent-ID": "qwen"})
    assert r.status_code == 200
    assert api.get("/api/sessions/s1").json()["harness"] == "qwen"


def test_session_id_is_url_decoded(api):
    api.put("/api/sessions/proj%2F2026", {"state": "x"})
    assert api.get("/api/sessions/proj%2F2026").json()["session_id"] == "proj/2026"


def test_too_long_state_is_rejected(api, settings_override):
    settings_override(checkpoint_max_chars=100)
    r = api.put("/api/sessions/s1", {"state": "x" * 101})
    assert r.status_code == 413
    assert "condense" in r.json()["error"]
    assert api.get("/api/sessions/s1").status_code == 404


@pytest.mark.parametrize("payload", [{"state": ""}, {"state": "   "}, {}])
def test_empty_state_is_rejected(api, payload):
    assert api.put("/api/sessions/s1", payload).status_code == 400


def test_invalid_json_is_rejected(api):
    r = api.put(
        "/api/sessions/s1",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_unknown_session_is_404(api):
    assert api.get("/api/sessions/nope").status_code == 404


def test_list_filters_by_project_newest_first(api):
    api.put("/api/sessions/a", {"state": "a", "project": "p1"})
    api.put("/api/sessions/b", {"state": "b", "project": "p2"})
    api.put("/api/sessions/c", {"state": "c", "project": "p1"})
    ids = [r["session_id"] for r in api.get("/api/sessions", project="p1").json()]
    assert ids == ["c", "a"]
    assert len(api.get("/api/sessions").json()) == 3
    assert len(api.get("/api/sessions", limit=1).json()) == 1


def test_list_hides_sessions_without_content(api):
    # A session that only ever got auto notes is listed; one with neither is impossible
    # through the API, so just verify auto-notes-only sessions appear.
    api.post(
        "/api/hooks/precompact",
        {"session_id": "n1", "project": "p1", "notes": "user asked X"},
    )
    assert [r["session_id"] for r in api.get("/api/sessions", project="p1").json()] == [
        "n1"
    ]


def test_requires_auth(api):
    assert api.get("/api/sessions", auth=False).status_code == 401
    assert api.get("/api/sessions/s1", auth=False).status_code == 401


def test_list_filters_by_harness(api):
    api.put("/api/sessions/c1", {"state": "x", "project": "p1", "harness": "claude"})
    api.put("/api/sessions/x1", {"state": "y", "project": "p1", "harness": "codex"})
    assert [
        r["session_id"] for r in api.get("/api/sessions", harness="codex").json()
    ] == ["x1"]
    assert [
        r["session_id"]
        for r in api.get("/api/sessions", project="p1", harness="claude").json()
    ] == ["c1"]


def test_delete_session(api):
    api.put("/api/sessions/d1", {"state": "to delete", "project": "p1"})
    r = api.delete("/api/sessions/d1")
    assert r.status_code == 200 and r.json() == {"ok": True, "session_id": "d1"}
    assert api.get("/api/sessions/d1").status_code == 404
    assert api.delete("/api/sessions/d1").status_code == 404


def test_delete_requires_auth(api):
    api.put("/api/sessions/d2", {"state": "keep"})
    assert api.delete("/api/sessions/d2", auth=False).status_code == 401
    assert api.get("/api/sessions/d2").status_code == 200
