# Harness adapters

Thin clients that connect each coding agent's lifecycle hooks to the claude-memory
server. All logic (what to inject, ranking, thresholds, formatting) lives in the server's
`/api/hooks/*` endpoints, so every harness behaves the same. The MCP tools work
without any of this. Adapters only add the automatic parts.

| What happens | Endpoint | Claude Code | Qwen Code | Codex | Pi |
|---|---|---|---|---|---|
| Session start: session id + last checkpoint of this project | `GET /api/hooks/briefing` | `SessionStart` | `SessionStart` | `SessionStart` | `session_start` → first `before_agent_start` |
| After compaction: re-inject current checkpoint | `GET /api/hooks/briefing?source=compact` | `SessionStart` (source `compact`) | `SessionStart` (source `compact`) | `SessionStart` (source `compact`) | `session_compact` → next `before_agent_start` |
| Per prompt: relevant memories above score threshold | `GET /api/hooks/recall` | `UserPromptSubmit` | `UserPromptSubmit` | `UserPromptSubmit` | `before_agent_start` |
| Before compaction: capture recent user turns (fallback if no checkpoint) | `POST /api/hooks/precompact` | `PreCompact` | `PreCompact` | `PreCompact` | `session_before_compact` |
| Save / read working state | `PUT/GET /api/sessions/{id}` | MCP `checkpoint` | MCP `checkpoint` | MCP `checkpoint` | tools `memory_checkpoint`, `memory_get_checkpoint` |

The project name is the git repo name of the working directory (override with
`AGENT_MEMORY_PROJECT`). The harness name is sent as `X-Agent-ID`.

## Install

Common to all harnesses:

```bash
mkdir -p ~/.config/agent-memory
cp adapters/hooks/memory_hook.py ~/.config/agent-memory/
cp adapters/env.example ~/.config/agent-memory/env && chmod 600 ~/.config/agent-memory/env
$EDITOR ~/.config/agent-memory/env          # set AGENT_MEMORY_URL + AGENT_MEMORY_TOKEN
```

Then for each harness you use:

- **Claude Code**: merge `claude-code/settings.hooks.json` into `~/.claude/settings.json`.
- **Qwen Code**: merge `qwen/settings.hooks.json` into `~/.qwen/settings.json`.
  (`qwen hooks migrate --from-claude` can also import the Claude config.)
- **Codex**: merge `codex/config.hooks.toml` into `~/.codex/config.toml`, then run `/hooks`
  once to trust the new hooks. Codex doesn't run untrusted hooks.
- **Pi**: `cp pi/agent-memory.ts ~/.pi/agent/extensions/` and run `/reload`. It reads the same
  env file. It was typechecked against pi 1.5.2 (`@earendil-works/pi-coding-agent`).

Quick check without a harness:

```bash
echo '{"session_id":"test","cwd":"'$PWD'","source":"startup"}' \
  | python3 ~/.config/agent-memory/memory_hook.py session-start --harness claude
```

## Safety rules the adapters follow

- **They never break the harness.** Every error (server down, timeout, bad config) is logged
  to stderr. The hook then exits 0 with no output, and the session continues without memory.
- **They respond within a deadline.** Hooks time out after 6–10 s, and the server's semantic
  search in hooks gives up after `HOOK_SEARCH_TIMEOUT` (8 s), so a slow Ollama can't stall a prompt.
- **They send nothing unless it was asked for.** Recall skips very short prompts like "yes"
  or "1", and only injects hits with cosine similarity ≥ `RECALL_MIN_SCORE` (0.6, max 3),
  each shortened to `HOOK_HIT_MAX_CHARS`.
  Tune both on the server.
- **They read only the end of the transcript.** Pre-compaction capture reads the last 2 MB
  of the transcript and stores at most `AUTO_NOTES_MAX_CHARS`.

## Server settings (env)

| Variable | Default | Meaning |
|---|---|---|
| `CHECKPOINT_MAX_CHARS` | 6000 | Larger checkpoints are rejected, which makes the model condense |
| `AUTO_NOTES_MAX_CHARS` | 3000 | Truncation limit for pre-compaction capture |
| `BRIEFING_MAX_CHARS` | 9000 | Cap for briefing output (~2.2k tokens; Codex default limit is ~2.5k) |
| `RECALL_MIN_SCORE` | 0.6 | Minimum cosine similarity for injected memories |
| `RECALL_LIMIT` | 3 | Memories injected per prompt |
| `RECALL_MIN_PROMPT_CHARS` | 12 | Shorter prompts skip recall |
| `HOOK_HIT_MAX_CHARS` | 400 | Each injected memory is shortened to this (description + start of body); the MCP tools still return full text |
| `HOOK_SEARCH_TIMEOUT` | 8.0 | Seconds before hook search gives up |
