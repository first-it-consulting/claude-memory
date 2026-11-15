"""adapters/hooks/memory_hook.py — run as a subprocess, the way harnesses call it."""
import json
import os
import subprocess
import sys

import pytest

from conftest import HOOK_SCRIPT, TOKEN


def run_hook(event, stdin, env_extra=None, harness="claude", home=None, args=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MEMORY_")}
    env["HOME"] = str(home) if home else "/nonexistent-home"
    env.update(env_extra or {})
    cmd = [sys.executable, str(HOOK_SCRIPT), event, "--harness", harness] if args is None else [sys.executable, str(HOOK_SCRIPT), *args]
    p = subprocess.run(cmd, input=json.dumps(stdin) if isinstance(stdin, dict) else stdin,
                       capture_output=True, text=True, env=env, timeout=30)
    return p


@pytest.fixture
def hook_env(server):
    return {"AGENT_MEMORY_URL": server, "AGENT_MEMORY_TOKEN": TOKEN, "AGENT_MEMORY_PROJECT": "p1"}


def context_of(p, event_name):
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == event_name
    return out["hookSpecificOutput"]["additionalContext"]


CLAUDE_TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "Please fix the trivy findings in the flux repo"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Looking at trivy reports now."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    {"type": "user", "message": {"role": "user", "content": "<system-reminder>injected</system-reminder>"}},
    {"type": "user", "message": {"role": "user", "content": "Use digest pinning, not tags"}},
]

CODEX_TRANSCRIPT = [
    {"type": "session_meta", "payload": {"id": "abc"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "Upgrade cert-manager to 1.19"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": "Bumped the HelmRelease."}]}},
]


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


# --- integration with a running server -------------------------------------------

@pytest.mark.db
@pytest.mark.parametrize("harness", ["claude", "qwen", "codex"])
def test_session_start_emits_briefing_json(hook_env, harness):
    p = run_hook("session-start", {"session_id": "h1", "cwd": "/tmp", "source": "startup"}, hook_env, harness)
    assert "Memory session_id: `h1`" in context_of(p, "SessionStart")


@pytest.mark.db
def test_session_start_compact_reinjects_checkpoint(api, hook_env):
    api.put("/api/sessions/h1", {"state": "## Goal\nrotate certs"})
    p = run_hook("session-start", {"session_id": "h1", "source": "compact"}, hook_env)
    ctx = context_of(p, "SessionStart")
    assert "rotate certs" in ctx and "Context was just compacted" in ctx


@pytest.mark.db
def test_clear_source_is_treated_as_startup(api, hook_env):
    api.put("/api/sessions/older", {"state": "older work", "project": "p1"})
    p = run_hook("session-start", {"session_id": "fresh", "source": "clear"}, hook_env)
    assert "Last session on p1" in context_of(p, "SessionStart")


@pytest.mark.db
def test_prompt_emits_recall(hook_env):
    p = run_hook("prompt", {"session_id": "h1", "prompt": "how do cnpg backups work on prod-k8s?"}, hook_env)
    assert "barman backups" in context_of(p, "UserPromptSubmit")


@pytest.mark.db
def test_prompt_without_hits_prints_nothing(hook_env):
    p = run_hook("prompt", {"prompt": "ok"}, hook_env)
    assert p.returncode == 0 and p.stdout == ""


@pytest.mark.db
def test_recall_can_be_disabled(hook_env):
    p = run_hook("prompt", {"prompt": "how do cnpg backups work on prod-k8s?"}, {**hook_env, "AGENT_MEMORY_RECALL": "0"})
    assert p.returncode == 0 and p.stdout == ""


@pytest.mark.db
def test_pre_compact_captures_claude_transcript(api, hook_env, tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", CLAUDE_TRANSCRIPT)
    p = run_hook("pre-compact", {"session_id": "h1", "transcript_path": path}, hook_env)
    assert p.returncode == 0 and p.stdout == ""
    notes = api.get("/api/sessions/h1").json()["auto_notes"]
    assert "- Please fix the trivy findings in the flux repo" in notes  # first line kept
    assert "- Use digest pinning, not tags" in notes
    assert "injected" not in notes and "tool_result" not in notes
    assert "> Looking at trivy reports now." in notes


@pytest.mark.db
def test_pre_compact_captures_codex_transcript(api, hook_env, tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", CODEX_TRANSCRIPT)
    run_hook("pre-compact", {"session_id": "x1", "transcript_path": path}, hook_env, harness="codex")
    row = api.get("/api/sessions/x1").json()
    assert row["harness"] == "codex" and row["project"] == "p1"
    assert "- Upgrade cert-manager to 1.19" in row["auto_notes"]
    assert "> Bumped the HelmRelease." in row["auto_notes"]


@pytest.mark.db
def test_pre_compact_without_transcript_does_nothing(api, hook_env):
    p = run_hook("pre-compact", {"session_id": "h1", "transcript_path": "/does/not/exist"}, hook_env)
    assert p.returncode == 0
    assert api.get("/api/sessions/h1").status_code == 404


@pytest.mark.db
def test_config_file_is_read(server, tmp_path):
    cfg = tmp_path / ".config" / "agent-memory"
    cfg.mkdir(parents=True)
    (cfg / "env").write_text(f"# comment\nexport AGENT_MEMORY_URL='{server}'\nAGENT_MEMORY_TOKEN=\"{TOKEN}\"\n")
    p = run_hook("session-start", {"session_id": "cfg"}, home=tmp_path)
    assert "`cfg`" in context_of(p, "SessionStart")


@pytest.mark.db
def test_project_defaults_to_git_repo_name(api, server, tmp_path):
    repo = tmp_path / "my-repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {"AGENT_MEMORY_URL": server, "AGENT_MEMORY_TOKEN": TOKEN}
    path = write_jsonl(tmp_path / "t.jsonl", CODEX_TRANSCRIPT)
    run_hook("pre-compact", {"session_id": "g1", "cwd": str(repo / "sub"), "transcript_path": path}, env)
    assert api.get("/api/sessions/g1").json()["project"] == "my-repo"


# --- failure modes: the hook must never break the harness -----------------------

def test_server_down_exits_zero_silently():
    p = run_hook("session-start", {"session_id": "x"},
                 {"AGENT_MEMORY_URL": "http://127.0.0.1:1", "AGENT_MEMORY_TOKEN": "t"})
    assert p.returncode == 0 and p.stdout == ""
    assert "session-start failed" in p.stderr


def test_missing_config_exits_zero_silently():
    p = run_hook("prompt", {"prompt": "hello there, long enough"})
    assert p.returncode == 0 and p.stdout == ""
    assert "not set" in p.stderr


def test_wrong_token_exits_zero_silently(server):
    p = run_hook("session-start", {"session_id": "x"}, {"AGENT_MEMORY_URL": server, "AGENT_MEMORY_TOKEN": "wrong"})
    assert p.returncode == 0 and p.stdout == ""
    assert "401" in p.stderr


@pytest.mark.parametrize("stdin", ["", "not json", "[]"])
def test_bad_stdin_exits_zero(stdin):
    p = run_hook("session-start", stdin, {"AGENT_MEMORY_URL": "http://127.0.0.1:1", "AGENT_MEMORY_TOKEN": "t"})
    assert p.returncode == 0 and p.stdout == ""


@pytest.mark.parametrize("args", [[], ["unknown-event"]])
def test_bad_arguments_exit_zero(args):
    p = run_hook(None, {}, args=args)
    assert p.returncode == 0 and "usage" in p.stderr
