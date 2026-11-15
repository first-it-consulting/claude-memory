"""add_memory must never drop a note silently (mem0 extraction returning no facts)."""
import json
import time

import pytest

from test_mcp_tools import call_tool


def _adds(fake):
    return [c for c in fake.calls if c[0] == "add"]


def test_no_facts_extracted_falls_back_to_raw(fake_mem):
    import memory as mem_store
    r = mem_store.add("Backups run at 3:00 (daily schedule).", category="reference", tags=["backups"])
    adds = _adds(fake_mem)
    assert [c[2]["infer"] for c in adds] == [True, False]
    raw_kwargs = adds[1][2]
    assert raw_kwargs["metadata"]["category"] == "reference"
    assert "backups" in raw_kwargs["metadata"]["tags"]
    assert r["fallback"] == "raw" and r["results"][0]["memory"] == "Backups run at 3:00 (daily schedule)."


def test_extracted_facts_no_fallback(fake_mem):
    import memory as mem_store
    fake_mem.infer_results = [{"id": "m1", "memory": "Backups run daily at 03:00", "event": "ADD"}]
    r = mem_store.add("Backups run at 3:00 (daily schedule).")
    assert [c[2]["infer"] for c in _adds(fake_mem)] == [True]
    assert "fallback" not in r


def test_explicit_infer_false_single_call(fake_mem):
    import memory as mem_store
    mem_store.add("store as is", infer=False)
    assert [c[2]["infer"] for c in _adds(fake_mem)] == [False]


def test_fallback_can_be_disabled(fake_mem, settings_override):
    import memory as mem_store
    settings_override(add_raw_fallback=False)
    r = mem_store.add("Backups run at 3:00")
    assert [c[2]["infer"] for c in _adds(fake_mem)] == [True]
    assert r == {"results": []}


def test_worker_summary_counts_events_without_content():
    from queue_worker import _summarize
    assert _summarize({"results": []}) == "nothing stored"
    assert _summarize({"results": [{"event": "ADD"}, {"event": "ADD"}, {"event": "UPDATE"}]}) == "ADD=2, UPDATE=1"
    s = _summarize({"results": [{"event": "ADD", "memory": "SECRET"}], "fallback": "raw"})
    assert s == "ADD=1 (fallback: raw, no facts extracted)" and "SECRET" not in s


def test_custom_instructions_reach_mem0_config():
    import memory as mem_store
    from config import settings
    cfg = mem_store._build_config()
    assert cfg.custom_instructions == settings.mem0_custom_instructions
    assert "Never return an empty list" in cfg.custom_instructions


@pytest.mark.db
def test_mcp_add_memory_with_infer_falls_back_when_no_facts(server, fake_mem):
    out = json.loads(call_tool(server, "add_memory", content="Backups run at 3:00 (daily schedule).",
                               category="reference", tags=["backups"], infer=True))
    assert out["queued"] is True
    deadline = time.time() + 10
    while time.time() < deadline and len(_adds(fake_mem)) < 2:
        time.sleep(0.1)
    assert [c[2]["infer"] for c in _adds(fake_mem)] == [True, False]


@pytest.mark.db
def test_mcp_add_memory_stores_as_written_by_default(server, fake_mem):
    call_tool(server, "add_memory", content="Backups run at 3:00 (daily schedule).", category="reference")
    deadline = time.time() + 10
    while time.time() < deadline and not _adds(fake_mem):
        time.sleep(0.1)
    time.sleep(0.3)
    adds = _adds(fake_mem)
    assert [c[2]["infer"] for c in adds] == [False]
    assert adds[0][1] == "Backups run at 3:00 (daily schedule)."
