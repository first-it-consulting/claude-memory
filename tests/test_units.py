"""Pure functions — no database or server needed."""
import importlib.util
import json

import pytest
from conftest import HOOK_SCRIPT


@pytest.fixture(scope="module")
def main_mod():
    import main
    return main


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("memory_hook", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- main.py formatting ------------------------------------------------------------

def test_format_memory_line_full(main_mod):
    r = {"id": "abc", "memory": "text", "score": 0.8123, "created_at": "2026-01-02T03:04:05",
         "metadata": {"metadata": {"category": "project", "tags": ["a", "b"]}}}
    assert main_mod._format_memory_line(r, with_id=True) == "[0.81] 2026-01-02 · project · #a,b · id=abc — text"


def test_format_memory_line_minimal(main_mod):
    assert main_mod._format_memory_line({"memory": "just text"}) == "just text"
    assert main_mod._format_memory_line({"memory": "t", "score": None}) == "t"


def test_format_memory_line_flat_metadata_and_no_score(main_mod):
    r = {"memory": "t", "score": 0.5, "metadata": {"category": "user"}}
    assert main_mod._format_memory_line(r, with_score=False) == "user — t"


def test_format_checkpoint(main_mod):
    row = {"session_id": "s1", "project": "p", "harness": "pi", "state": "S", "auto_notes": "A",
           "updated_at": "2026-11-15 13:02:50.66+01:00"}
    out = main_mod._format_checkpoint(row, "Title")
    assert out.splitlines()[0] == "### Title (session s1 · p · pi, updated 2026-11-15 13:02)"
    assert "S" in out and "#### Auto-captured" in out and out.endswith("A")


def test_truncate(main_mod):
    assert main_mod._truncate("short", 100) == "short"
    out = main_mod._truncate("x" * 500, 100)
    assert len(out) <= 100 and out.endswith("[truncated by claude-memory]")


@pytest.mark.parametrize("value,default,expected", [("3", 1, 3), ("x", 1, 1), (None, 7, 7)])
def test_int_parsing(main_mod, value, default, expected):
    assert main_mod._int(value, default) == expected


@pytest.mark.parametrize("value,default,expected", [("0.5", 1.0, 0.5), ("nan?", 1.0, 1.0), (None, 0.6, 0.6)])
def test_float_parsing(main_mod, value, default, expected):
    assert main_mod._float(value, default) == expected


# --- memory_hook.py transcript parsing ---------------------------------------------------

def test_text_of_variants(hook):
    assert hook._text_of("plain") == "plain"
    assert hook._text_of([{"type": "text", "text": "a"}, {"type": "tool_use", "text": "no"},
                          {"type": "input_text", "text": "b"}, {"text": "c"}]) == "a\nb\nc"
    assert hook._text_of(None) == ""


def test_noise_filter(hook):
    assert hook._is_noise("user", "<system-reminder>x")
    assert hook._is_noise("user", "[Request interrupted by user]")
    assert hook._is_noise("user", "## Memory briefing (claude-memory) ...")
    assert not hook._is_noise("user", "real request")
    assert not hook._is_noise("assistant", "<b>html answer</b>")


def test_extract_limits_turn_count_and_length(hook, tmp_path):
    rows = [{"role": "user", "content": f"request {i} " + "z" * 400} for i in range(20)]
    rows.append({"role": "assistant", "content": "answer\nwith newline"})
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    out = hook.extract_recent_turns(str(path))
    user_lines = [l for l in out.splitlines() if l.startswith("- ")]
    assert len(user_lines) == hook.MAX_USER_TURNS
    assert user_lines[0].startswith("- request 12 ")  # last 8 of 20
    assert all(len(l) <= 302 for l in user_lines)
    assert "> answer with newline" in out


def test_extract_reads_only_tail_of_large_file(hook, tmp_path):
    path = tmp_path / "big.jsonl"
    filler = json.dumps({"role": "user", "content": "old " + "q" * 1000})
    with path.open("w") as f:
        for _ in range(2500):  # ~2.5 MB, more than the 2 MB tail window
            f.write(filler + "\n")
        f.write(json.dumps({"role": "user", "content": "the latest request"}) + "\n")
    out = hook.extract_recent_turns(str(path))
    assert "- the latest request" in out


def test_extract_skips_garbage_lines(hook, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('not json\n{"role":"user","content":"ok request"}\n{broken\n')
    assert "- ok request" in hook.extract_recent_turns(str(path))


def test_extract_empty_or_missing(hook, tmp_path):
    assert hook.extract_recent_turns("") == ""
    assert hook.extract_recent_turns(str(tmp_path / "missing.jsonl")) == ""
    (tmp_path / "empty.jsonl").write_text("")
    assert hook.extract_recent_turns(str(tmp_path / "empty.jsonl")) == ""


def test_load_config_env_overrides_file(hook, tmp_path, monkeypatch):
    cfg = tmp_path / "env"
    cfg.write_text("AGENT_MEMORY_URL=http://file\nAGENT_MEMORY_TOKEN=filetoken\n")
    monkeypatch.setattr(hook, "CONFIG_FILE", cfg)
    monkeypatch.setenv("AGENT_MEMORY_TOKEN", "envtoken")
    monkeypatch.delenv("AGENT_MEMORY_URL", raising=False)
    c = hook.load_config()
    assert c["AGENT_MEMORY_URL"] == "http://file"
    assert c["AGENT_MEMORY_TOKEN"] == "envtoken"


def test_emit_context_skips_empty(hook, capsys):
    hook.emit_context("SessionStart", "   ")
    assert capsys.readouterr().out == ""
    hook.emit_context("SessionStart", "hi")
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "hi"


# --- hook output condensing (recall / briefing) ----------------------------------

IMPORTED_FILE = (
    "[origin: some-project | file: k3s-node-upgrades.md] ---\n"
    "name: k3s-node-upgrades\n"
    "description: How k3s node OS updates are automated (kured + staggered upgrades)\n"
    "metadata:\n  type: project\n---\n\n"
    "Since 2026-11-11 unattended-upgrades run staggered per node. " + "x" * 2000
)


def test_condense_keeps_description_and_body_start(main_mod):
    out = main_mod._condense(IMPORTED_FILE, 200)
    assert out.startswith("How k3s node OS updates are automated (kured + staggered upgrades) — Since 2026-11-11")
    assert "[origin:" not in out and "metadata:" not in out and "\n" not in out
    assert len(out) <= 200 and out.endswith("…")


def test_condense_short_plain_text_unchanged(main_mod):
    assert main_mod._condense("Prod uses CNPG with daily backups", 400) == "Prod uses CNPG with daily backups"


def test_format_memory_line_condenses_only_when_asked(main_mod):
    r = {"memory": IMPORTED_FILE, "score": 0.7, "metadata": {}}
    assert len(main_mod._format_memory_line(r, max_chars=300)) < 360
    assert "x" * 2000 in main_mod._format_memory_line(r)  # MCP tools: full text


# --- pre-compact transcript filtering ---------------------------------------------

def test_transcript_skips_request_blobs_tool_results_and_summaries(hook, tmp_path):
    rows = [
        {"type": "user", "message": {"role": "user", "content": "Call the claude-memory checkpoint tool please"}},
        {"type": "api-request-blob", "message": {"role": "user", "content": [
            {"type": "text", "text": "Call the claude-memory checkpoint tool please"},
            {"type": "text", "text": "Tool loaded."}]}},
        {"type": "user", "toolUseResult": {}, "message": {"role": "user", "content": [{"type": "text", "text": "Tool loaded."}]}},
        {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued from a previous conversation..."}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat text"}},
        {"type": "attachment", "attachment": {"role": "user", "content": "hook output"}},
        {"type": "user", "message": {"role": "user", "content": "second request"}},
        {"type": "user", "message": {"role": "user", "content": "Call the claude-memory checkpoint tool please"}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    notes = hook.extract_recent_turns(str(path))
    users = [l for l in notes.splitlines() if l.startswith("- ")]
    # mentions of claude-memory are real requests; repeats collapse to the latest position
    assert users == ["- second request", "- Call the claude-memory checkpoint tool please"]
    assert "Tool loaded." not in notes and "caveat" not in notes and "hook output" not in notes
    assert "continued from a previous" not in notes


def test_norm_harness(main_mod):
    f = main_mod._norm_harness
    assert f("  Claude ") == "claude"
    assert f("QWEN") == "qwen"
    assert f("pi") == "pi"
    assert f("") == ""
    assert f("   ") == ""
    assert f("x" * 33) == ""          # too long -> dropped
    assert f("x" * 32) == "x" * 32    # exactly at the cap passes
    assert f("bad\x01harness") == ""  # control char -> dropped
    assert f("bell\x07") == ""
    assert f("del\x7f") == ""


def test_pat_route_id():
    import tokens
    assert tokens.route_id("cm_t1a2b3c4d_secretstuff") == "t1a2b3c4d"
    assert tokens.route_id("cm_-Abc_D_ef") == ""        # legacy urlsafe shape
    assert tokens.route_id("cm_nootheere") == ""
    assert tokens.route_id("notcm_x") == ""
    assert tokens.route_id("cm_tttttttt_x") == ""       # id must be t+8 hex
    assert tokens.route_id("cm_t12345678_") == ""       # empty secret


def test_pat_mask_never_exposes_and_handles_infinity():
    import datetime as dt

    import tokens
    row = {"id": "t1", "user_id": "u", "name": "n", "prefix": "cm_t1", "last4": "abcd",
           "can_admin": False, "default_harness": "", "created_at": "x", "created_by": "",
           "expires_at": dt.datetime(9999, 12, 31, tzinfo=dt.UTC),
           "last_used_at": None, "revoked_at": None, "revoked_by": "",
           "token_hash": "HASHMUSTNOTLEAK"}
    m = tokens._mask(row)
    assert m["expires_in_days"] is None           # 'infinity' -> never
    assert m["display"] == "cm_t1…abcd"
    assert "HASHMUSTNOTLEAK" not in str(m)
