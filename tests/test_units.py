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
