"""MCP: tools over the real /mcp endpoint (JSON-RPC over streamable HTTP)."""

import json

import pytest
import requests
from conftest import TOKEN

pytestmark = pytest.mark.db

EXPECTED_TOOLS = {
    "add_memory",
    "search_memory",
    "get_all_memories",
    "update_memory",
    "delete_memory",
    "checkpoint",
    "get_checkpoint",
    "fetch_memories",
    "memory_timeline",
}


def rpc(server, method, params=None):
    r = requests.post(
        server + "/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    text = r.text
    if text.lstrip().startswith("data:") or "\ndata:" in text:  # SSE framing
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


def call_tool(server, name, **arguments):
    res = rpc(server, "tools/call", {"name": name, "arguments": arguments})
    assert "result" in res, res
    return res["result"]["content"][0]["text"]


def test_initialize_advertises_checkpoint_instructions(server):
    res = rpc(
        server,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    assert "checkpoint" in res["result"]["instructions"]


def test_tools_list_contains_all_tools_without_output_schema(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    assert all("outputSchema" not in t for t in tools)


def test_mcp_requires_auth(server):
    r = requests.post(
        server + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=10,
    )
    assert r.status_code == 401


def test_search_memory_lines_include_date_category_tags_and_id(server):
    out = call_tool(server, "search_memory", query="cnpg backups prod-k8s", limit=1)
    assert out == (
        "[0.89] 2026-11-01 · project · #prod-k8s,cnpg · "
        "id=11111111-1111-1111-1111-111111111111 — prod-k8s CNPG cluster uses barman backups to MinIO"
    )


def test_search_memory_prefers_updated_at_for_date(server):
    out = call_tool(server, "search_memory", query="kyverno require-labels", limit=1)
    assert "2026-11-12" in out


def test_search_memory_no_results(server, fake_mem):
    fake_mem.corpus = []
    assert call_tool(server, "search_memory", query="anything") == "No memories found."


def test_get_all_memories_is_paged_newest_first(server):
    out = call_tool(server, "get_all_memories", limit=2)
    lines = out.splitlines()
    assert lines[0] == "Showing 2 of 3 memories (newest first)."
    assert "Kyverno" in lines[1] and "CNPG" in lines[2]


def test_get_all_memories_default_limit_and_category_filter(server, settings_override):
    settings_override(get_all_default_limit=1)
    assert call_tool(server, "get_all_memories").startswith("Showing 1 of 3")
    out = call_tool(server, "get_all_memories", category="user")
    assert out.startswith("Showing 1 of 1") and "Ollama" in out


def test_update_memory_calls_mem0(server, fake_mem):
    rid = "11111111-1111-1111-1111-111111111111"
    out = call_tool(server, "update_memory", memory_id=rid, content="  new text ")
    assert "updated" in out
    assert ("update", rid, "new text") in fake_mem.calls


def test_update_memory_unknown_id_is_not_written(server, fake_mem):
    out = call_tool(server, "update_memory", memory_id="does-not-exist", content="x")
    assert "No memory with id" in out
    assert not any(c[0] == "update" for c in fake_mem.calls)


def test_update_memory_rejects_empty_content(server, fake_mem):
    assert "must not be empty" in call_tool(
        server, "update_memory", memory_id="abc", content=" "
    )
    assert not any(c[0] == "update" for c in fake_mem.calls)


def test_checkpoint_roundtrip_by_session_and_by_project(server):
    out = json.loads(
        call_tool(
            server, "checkpoint", session_id="m1", state="## Goal\nTest", project="proj"
        )
    )
    assert out["ok"] is True and out["session_id"] == "m1"
    by_session = call_tool(server, "get_checkpoint", session_id="m1")
    assert "## Goal\nTest" in by_session and "session m1" in by_session
    by_project = call_tool(server, "get_checkpoint", project="proj")
    assert "## Goal\nTest" in by_project


def test_checkpoint_validation(server, settings_override):
    assert "required" in call_tool(server, "checkpoint", session_id="", state="x")
    assert "required" in call_tool(server, "checkpoint", session_id="m1", state="  ")
    settings_override(checkpoint_max_chars=10)
    assert "too long" in call_tool(
        server, "checkpoint", session_id="m1", state="x" * 11
    )


def test_get_checkpoint_missing_and_no_args(server):
    assert (
        call_tool(server, "get_checkpoint", session_id="nope") == "No checkpoint found."
    )
    assert call_tool(server, "get_checkpoint") == "Pass session_id or project."


def test_add_memory_is_still_queued(server):
    out = json.loads(
        call_tool(server, "add_memory", content="a durable fact", category="project")
    )
    assert out["queued"] is True and out["job_id"]


def test_add_memory_rejects_invalid_category(server):
    assert "Invalid category" in call_tool(
        server, "add_memory", content="x", category="nonsense"
    )


def test_checkpoint_records_harness(server):
    call_tool(
        server, "checkpoint", session_id="h1", state="## Goal\nx", harness="CLAUDE "
    )
    out = call_tool(server, "get_checkpoint", session_id="h1")
    assert "· claude" in out.splitlines()[0]


def test_checkpoint_empty_harness_keeps_known_value(server):
    # Pins the COALESCE(NULLIF(...)) upsert: a later write without a harness
    # must never erase the registered one.
    call_tool(server, "checkpoint", session_id="h2", state="v1", harness="pi")
    call_tool(server, "checkpoint", session_id="h2", state="v2")
    out = call_tool(server, "get_checkpoint", session_id="h2")
    assert "· pi" in out.splitlines()[0]


def test_checkpoint_invalid_harness_is_dropped_not_fatal(server):
    out = json.loads(
        call_tool(
            server, "checkpoint", session_id="h3", state="v1", harness="bad\x01harness"
        )
    )
    assert out["ok"] is True  # the checkpoint itself still succeeds
    header = call_tool(server, "get_checkpoint", session_id="h3").splitlines()[0]
    assert "bad" not in header
