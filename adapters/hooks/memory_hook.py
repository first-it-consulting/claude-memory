#!/usr/bin/env python3
"""claude-memory hook adapter for Claude Code, Qwen Code and Codex.

All three harnesses use the same hook contract: a JSON event on stdin, and for
SessionStart / UserPromptSubmit a JSON `hookSpecificOutput.additionalContext`
on stdout that is added to the model's context. This script translates those
events into calls to the claude-memory server; the server does all the work.

    memory_hook.py session-start --harness claude   # SessionStart (startup|resume|clear|compact)
    memory_hook.py prompt        --harness claude   # UserPromptSubmit -> recall
    memory_hook.py pre-compact   --harness claude   # PreCompact -> auto-capture recent turns

Config (env vars, or KEY=VALUE lines in ~/.config/agent-memory/env):
    AGENT_MEMORY_URL       e.g. https://claude-memory.example.lan   (required)
    AGENT_MEMORY_TOKEN     bearer token                              (required)
    AGENT_MEMORY_PROJECT   override the project name (default: git repo / cwd name)
    AGENT_MEMORY_RECALL    "0" disables per-prompt recall

Design rule: a memory hook must never break or stall the harness. Every error
is swallowed (logged to stderr) and the script exits 0 with no output.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "agent-memory" / "env"
TIMEOUTS = {"session-start": 10, "prompt": 6, "pre-compact": 10}
MAX_USER_TURNS = 8
MAX_ASSISTANT_TURNS = 2


def log(msg: str) -> None:
    print(f"[agent-memory] {msg}", file=sys.stderr)


def load_config() -> dict:
    cfg = {}
    if CONFIG_FILE.is_file():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    for k in ("AGENT_MEMORY_URL", "AGENT_MEMORY_TOKEN", "AGENT_MEMORY_PROJECT", "AGENT_MEMORY_RECALL"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def project_name(cwd: str, cfg: dict) -> str:
    if cfg.get("AGENT_MEMORY_PROJECT"):
        return cfg["AGENT_MEMORY_PROJECT"]
    cwd = cwd or os.getcwd()
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if top:
            return Path(top).name
    except Exception:
        pass
    return Path(cwd).name


def request(cfg: dict, method: str, path: str, harness: str, timeout: float,
            params: dict | None = None, body: dict | None = None) -> dict:
    url = cfg["AGENT_MEMORY_URL"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {cfg['AGENT_MEMORY_TOKEN']}",
        "Content-Type": "application/json",
        "X-Agent-ID": harness,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def emit_context(event_name: str, context: str) -> None:
    if context.strip():
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }}))


# --- transcript extraction (pre-compact) -----------------------------------
# Transcript formats differ per harness (Claude: {"type":"user","message":{...}},
# Codex: {"type":"response_item","payload":{"type":"message",...}}). Walk every
# JSON line generically and pick up {role, content} message objects.

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (None, "text", "input_text", "output_text"):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _find_messages(obj, out: list) -> None:
    if isinstance(obj, dict):
        role = obj.get("role")
        if role in ("user", "assistant") and "content" in obj:
            text = _text_of(obj["content"]).strip()
            if text:
                out.append((role, text))
            return
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _find_messages(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_messages(v, out)


# Transcript lines that are not conversation turns. Claude Code also logs
# request snapshots ("api-request-blob"), which repeat earlier messages plus
# injected reminders and tool results; reading them produced duplicates and
# entries like "Tool loaded.".
_SKIP_LINE_TYPES = {"attachment", "system", "summary", "queue-operation", "last-prompt",
                    "session-owner", "atis-latch", "tools-used", "cost-state", "mode",
                    "event_msg", "turn_context", "session_meta"}


def _is_turn_line(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    t = obj.get("type")
    if isinstance(t, str) and (t in _SKIP_LINE_TYPES or t.startswith("api-request")):
        return False
    # Claude Code flags: compaction summary, meta caveats, tool results, subagents.
    if obj.get("isCompactSummary") or obj.get("isMeta") or obj.get("isSidechain") or "toolUseResult" in obj:
        return False
    return True


def _is_noise(role: str, text: str) -> bool:
    # Harness-injected context (reminders, hook output, command wrappers) is
    # not something the user said.
    t = text.lstrip()
    return role == "user" and (
        t.startswith("<")
        or t.startswith("[Request interrupted")
        or t.startswith("This session is being continued from a previous conversation")
        # our own injected output, when a harness logs it as a user message
        # (exact headers only: a user mentioning claude-memory is a real request)
        or t.startswith(("## Memory briefing (claude-memory)", "### Possibly relevant memories (claude-memory"))
    )


def _dedupe_keep_last(items: list) -> list:
    seen, out = set(), []
    for x in reversed(items):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[::-1]


def extract_recent_turns(transcript_path: str) -> str:
    path = Path(transcript_path or "")
    if not path.is_file():
        return ""
    # Read only the tail: transcripts can be tens of MB.
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - 2_000_000)
        f.seek(start)
        lines = f.read().decode("utf-8", errors="replace").splitlines()
        if start > 0:
            lines = lines[1:]  # first line is partial
    messages: list = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if _is_turn_line(obj):
            _find_messages(obj, messages)
    users = _dedupe_keep_last([t for r, t in messages if r == "user" and not _is_noise(r, t)])[-MAX_USER_TURNS:]
    assistants = [t for r, t in messages if r == "assistant"][-MAX_ASSISTANT_TURNS:]
    if not users and not assistants:
        return ""
    out = ["Recent user requests (oldest first):"]
    out += [f"- {u[:300].replace(chr(10), ' ')}" for u in users]
    if assistants:
        out.append("Last assistant output:")
        out += [f"> {a[:600].replace(chr(10), ' ')}" for a in assistants]
    return "\n".join(out)


# --- event handlers ----------------------------------------------------------

def on_session_start(ev: dict, cfg: dict, harness: str) -> None:
    source = ev.get("source") or "startup"
    if source == "clear":
        source = "startup"
    res = request(cfg, "GET", "/api/hooks/briefing", harness, TIMEOUTS["session-start"], params={
        "session_id": ev.get("session_id", ""),
        "project": project_name(ev.get("cwd", ""), cfg),
        "source": source,
        "harness": harness,
    })
    emit_context("SessionStart", res.get("context", ""))


def on_prompt(ev: dict, cfg: dict, harness: str) -> None:
    if cfg.get("AGENT_MEMORY_RECALL", "1") == "0":
        return
    res = request(cfg, "GET", "/api/hooks/recall", harness, TIMEOUTS["prompt"],
                  params={"q": (ev.get("prompt") or "")[:1000]})
    emit_context("UserPromptSubmit", res.get("context", ""))


def on_pre_compact(ev: dict, cfg: dict, harness: str) -> None:
    notes = extract_recent_turns(ev.get("transcript_path", ""))
    if not notes or not ev.get("session_id"):
        return
    request(cfg, "POST", "/api/hooks/precompact", harness, TIMEOUTS["pre-compact"], body={
        "session_id": ev["session_id"],
        "project": project_name(ev.get("cwd", ""), cfg),
        "harness": harness,
        "notes": notes,
    })


HANDLERS = {"session-start": on_session_start, "prompt": on_prompt, "pre-compact": on_pre_compact}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in HANDLERS:
        log(f"usage: memory_hook.py {{{'|'.join(HANDLERS)}}} [--harness NAME]")
        return 0
    harness = "agent"
    if "--harness" in args:
        i = args.index("--harness")
        if i + 1 < len(args):
            harness = args[i + 1]
    try:
        cfg = load_config()
        if not cfg.get("AGENT_MEMORY_URL") or not cfg.get("AGENT_MEMORY_TOKEN"):
            log("AGENT_MEMORY_URL / AGENT_MEMORY_TOKEN not set — skipping")
            return 0
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
        HANDLERS[args[0]](ev, cfg, harness)
    except Exception as e:  # never break the harness
        log(f"{args[0]} failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
