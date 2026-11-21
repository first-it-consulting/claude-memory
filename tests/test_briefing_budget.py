"""SessionStart briefing: compact index within BRIEFING_TOKEN_BUDGET (bean x4di).

Default (budget=0) must stay byte-identical to the classic briefing; a set
budget switches /api/hooks/briefing to the compact index: header + session_id
+ checkpoint (truncated if needed) + one short id-bearing line per memory
until the budget runs out, plus a footer with the token stats.
"""

import pytest

pytestmark = pytest.mark.db


def briefing(api, **params):
    r = api.get("/api/hooks/briefing", **params)
    assert r.status_code == 200
    return r.json()["context"]


def test_default_unchanged_when_budget_zero(api, settings_override):
    settings_override(briefing_token_budget=0)
    api.put(
        "/api/sessions/s1",
        {"state": "## Goal\nFix kyverno require-labels", "project": "p1"},
    )
    ctx = briefing(api, session_id="s1", project="p1", source="startup")
    assert "budget ~" not in ctx
    assert "compact index" not in ctx
    assert "Relevant long-term memories" in ctx
    # full (400-char-capped) hit text as before
    assert "Kyverno policy require-labels" in ctx


def test_budget_switches_to_compact_index(api, settings_override):
    settings_override(briefing_token_budget=200, recall_min_score=0.05)
    api.put(
        "/api/sessions/s1",
        {
            "state": "## Goal\nKyverno require-labels\n" + ("padding words " * 120),
            "project": "p1",
        },
    )
    ctx = briefing(api, session_id="s1", project="p1", source="startup")
    assert "compact index" in ctx
    assert "budget ~200 tok" in ctx
    assert "Memory index" in ctx
    # compact lines carry the memory id so the agent can fetch full text
    assert "id=" in ctx
    # chars/4 estimate stays within budget plus footer/slack
    assert len(ctx) // 4 <= 200 + 80
    # the oversized checkpoint was truncated, not dumped
    assert "padding words padding words padding words padding words padding" not in ctx


def test_budget_footer_reports_dropped_memories(api, settings_override):
    settings_override(briefing_token_budget=130, recall_min_score=0.05)
    api.put(
        "/api/sessions/s1",
        {"state": "## Goal\nKyverno and CNPG barman MinIO memories", "project": "p1"},
    )
    ctx = briefing(api, session_id="s1", project="p1", source="startup")
    footer = ctx.rsplit("(", 1)[-1]
    assert "memories " in footer and "dropped " in footer
    kept = int(footer.split("memories ")[1].split("/")[0])
    dropped = int(footer.split("dropped ")[1].split(";")[0])
    assert kept + dropped >= 1  # hits existed and were accounted for
    assert kept >= 1  # a 100-token budget still fits the essentials + >=1 line
