"""REST: /api/hooks/* — briefing, recall, precompact."""

import time

import pytest

pytestmark = pytest.mark.db


def briefing(api, **params):
    r = api.get("/api/hooks/briefing", **params)
    assert r.status_code == 200
    return r.json()["context"]


# --- briefing ------------------------------------------------------------------


def test_briefing_without_state_only_gives_session_instructions(api):
    ctx = briefing(api, session_id="s1", project="p1", source="startup")
    assert "Memory session_id: `s1`" in ctx
    assert "`checkpoint`" in ctx
    assert "checkpoint (session" not in ctx.lower()
    assert "compacted" not in ctx


def test_briefing_tool_name_is_configurable(api):
    ctx = briefing(api, session_id="s1", tool="memory_checkpoint")
    assert "`memory_checkpoint`" in ctx


def test_briefing_after_compaction_reinjects_checkpoint_and_auto_notes(api):
    api.put(
        "/api/sessions/s1",
        {"state": "## Goal\nFix kyverno require-labels", "project": "p1"},
    )
    api.post(
        "/api/hooks/precompact", {"session_id": "s1", "notes": "User: also fix trivy"}
    )
    ctx = briefing(api, session_id="s1", project="p1", source="compact")
    assert "Context was just compacted" in ctx
    assert "Current session checkpoint" in ctx
    assert "Fix kyverno require-labels" in ctx
    assert "User: also fix trivy" in ctx
    # memories related to the checkpoint goal are attached
    assert "Relevant long-term memories" in ctx
    assert "Kyverno policy require-labels" in ctx


def test_briefing_new_session_shows_last_session_of_same_project_only(api):
    api.put("/api/sessions/old-p1", {"state": "goal for p1", "project": "p1"})
    api.put("/api/sessions/old-p2", {"state": "goal for p2", "project": "p2"})
    ctx = briefing(api, session_id="new", project="p1", source="startup")
    assert "Last session on p1" in ctx
    assert "goal for p1" in ctx
    assert "goal for p2" not in ctx
    assert "only continue it if" in ctx


def test_briefing_prefers_current_session_over_previous(api):
    api.put("/api/sessions/old", {"state": "old goal", "project": "p1"})
    api.put("/api/sessions/cur", {"state": "current goal", "project": "p1"})
    ctx = briefing(api, session_id="cur", project="p1", source="resume")
    assert "current goal" in ctx and "old goal" not in ctx


def test_briefing_is_capped(api, settings_override):
    settings_override(briefing_max_chars=400)
    api.put("/api/sessions/s1", {"state": "y" * 3000})
    ctx = briefing(api, session_id="s1", source="compact")
    assert len(ctx) <= 400
    assert ctx.endswith("[truncated by claude-memory]")


def test_briefing_survives_search_failure(api, fake_mem):
    api.put("/api/sessions/s1", {"state": "goal kyverno"})
    fake_mem.mode = "raise"
    ctx = briefing(api, session_id="s1", source="compact")
    assert "goal kyverno" in ctx
    assert "Relevant long-term memories" not in ctx


def test_briefing_does_not_wait_for_slow_search(api, fake_mem, settings_override):
    settings_override(hook_search_timeout=0.5)
    fake_mem.mode, fake_mem.slow_seconds = "slow", 3
    api.put("/api/sessions/s1", {"state": "goal kyverno"})
    t0 = time.monotonic()
    ctx = briefing(api, session_id="s1", source="compact")
    assert time.monotonic() - t0 < 2.5
    assert "goal kyverno" in ctx


def test_briefing_without_session_id(api):
    ctx = briefing(api, project="p1")
    assert ctx.startswith("## Memory briefing")
    assert "session_id" not in ctx


# --- recall ----------------------------------------------------------------------


def test_recall_returns_hits_above_threshold(api):
    ctx = api.get(
        "/api/hooks/recall", q="how are cnpg backups configured on prod-k8s"
    ).json()["context"]
    assert ctx.startswith("### Possibly relevant memories")
    assert "barman backups" in ctx
    assert "Ollama runs on CPU" not in ctx  # below threshold


def test_recall_skips_short_prompts(api, fake_mem):
    assert api.get("/api/hooks/recall", q="yes").json()["context"] == ""
    assert not any(c[0] == "search" for c in fake_mem.calls)


def test_recall_min_score_and_limit_params(api):
    q = "prod-k8s cnpg kyverno require-labels ollama cpu lab"
    assert api.get("/api/hooks/recall", q=q, min_score=0.99).json()["context"] == ""
    ctx = api.get("/api/hooks/recall", q=q, min_score=0, limit=1).json()["context"]
    assert ctx.count("\n- ") == 1


def test_recall_bad_params_fall_back_to_defaults(api):
    r = api.get(
        "/api/hooks/recall", q="cnpg backups prod-k8s minio", limit="abc", min_score="x"
    )
    assert r.status_code == 200 and "barman" in r.json()["context"]


def test_recall_returns_empty_on_search_error(api, fake_mem):
    fake_mem.mode = "raise"
    r = api.get("/api/hooks/recall", q="how are cnpg backups configured")
    assert r.status_code == 200 and r.json()["context"] == ""


# --- precompact ------------------------------------------------------------------


def test_precompact_stores_and_truncates_notes(api, settings_override):
    settings_override(auto_notes_max_chars=200)
    r = api.post(
        "/api/hooks/precompact",
        {"session_id": "s1", "project": "p1", "harness": "codex", "notes": "n" * 500},
    )
    assert r.status_code == 200
    row = api.get("/api/sessions/s1").json()
    assert len(row["auto_notes"]) <= 200
    assert row["harness"] == "codex" and row["project"] == "p1"
    assert row["state"] == ""


def test_precompact_does_not_touch_state(api):
    api.put("/api/sessions/s1", {"state": "keep me"})
    api.post("/api/hooks/precompact", {"session_id": "s1", "notes": "auto"})
    row = api.get("/api/sessions/s1").json()
    assert row["state"] == "keep me" and row["auto_notes"] == "auto"


@pytest.mark.parametrize(
    "payload",
    [{"notes": "x"}, {"session_id": "s1"}, {"session_id": "s1", "notes": " "}],
)
def test_precompact_requires_session_and_notes(api, payload):
    assert api.post("/api/hooks/precompact", payload).status_code == 400


def test_hooks_require_auth(api):
    assert api.get("/api/hooks/briefing", auth=False).status_code == 401
    assert api.get("/api/hooks/recall", auth=False, q="x").status_code == 401


def test_recall_hits_are_condensed(api, fake_mem, settings_override):
    settings_override(hook_hit_max_chars=120)
    fake_mem.corpus.append(
        {
            "id": "long1",
            "memory": "cnpg backups prod-k8s " + "detail " * 400,
            "metadata": {"category": "project"},
            "agent_id": "t",
            "created_at": "2026-11-01",
            "updated_at": "2026-11-01",
        }
    )
    ctx = api.get(
        "/api/hooks/recall", q="how do cnpg backups work on prod-k8s?"
    ).json()["context"]
    assert "full text: search_memory" in ctx
    assert all(len(ln) < 220 for ln in ctx.splitlines())


def test_briefing_registers_project_for_later_mcp_checkpoint(api):
    # session starts (hook knows project+harness), then the model checkpoints via MCP (knows neither)
    briefing(api, session_id="reg1", project="pX", source="startup", harness="claude")
    row = api.get("/api/sessions/reg1").json()
    assert row["project"] == "pX" and row["harness"] == "claude" and row["state"] == ""
    # an empty registered row is not offered as "last session"
    assert "Last session on pX" not in briefing(api, session_id="other", project="pX")
    api.put(
        "/api/sessions/reg1", {"state": "## Goal\nregistered goal"}
    )  # no project, like MCP
    ctx = briefing(api, session_id="next", project="pX", source="startup")
    assert "Last session on pX" in ctx and "registered goal" in ctx


def test_auto_promoted_memories_rank_lower_and_filter_by_tag(api, fake_mem):
    base = {"agent_id": "t", "created_at": "2026-11-01", "updated_at": "2026-11-01"}
    fake_mem.corpus += [
        {
            "id": "promo",
            "memory": "kubeval ci ssl errors removed",
            **base,
            "metadata": {"tags": ["auto-promoted", "decision"]},
        },
        {
            "id": "manual",
            "memory": "kubeval ci ssl errors removed",
            **base,
            "metadata": {"tags": ["ci"]},
        },
    ]
    ctx = api.get(
        "/api/hooks/recall", q="why was kubeval removed from ci ssl errors?"
    ).json()["context"]
    lines = [ln for ln in ctx.splitlines() if "kubeval" in ln]
    assert "#ci" in lines[0] and "auto-promoted" in lines[1]
    ids = [m["id"] for m in api.get("/api/memories", tag="auto-promoted").json()]
    assert ids == ["promo"]
