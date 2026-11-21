#!/usr/bin/env bash
# claude-memory harness setup + doctor (bean yws4).
#
#   scripts/setup.sh setup  [--url URL] [--token cm_..] [options]   configure
#   scripts/setup.sh doctor [--url URL] [--token cm_..]             verify
#
# Detects installed harnesses (Claude Code, Qwen Code, Codex, pi, opencode),
# writes the shared hook env + per-harness MCP/hook config with a per-user
# token, and verifies connectivity. Idempotent: re-running changes nothing.
# --dry-run prints every planned action and writes nothing.
#
# The token is never echoed and never put on another command's argv; the only
# places it lands are ~/.config/agent-memory/env (chmod 600) and the harness
# MCP config that the harness itself reads.
set -euo pipefail

MODE="${1:-setup}"
case "$MODE" in setup|doctor) shift ;; *) MODE=setup ;; esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CM_URL="${AGENT_MEMORY_URL:-}"
CM_TOKEN="${AGENT_MEMORY_TOKEN:-}"
ADMIN_TOKEN=""   # export as CM_ADMIN for api(); never on argv
CREATE_USER=""
USER_NAME=""
TOKEN_NAME="setup"
ONLY=""
DRY=0

say() { printf '%s\n' "$*"; }
run() { # run <description> <command...> - honours --dry-run
  local desc="$1"; shift
  if [ "$DRY" = 1 ]; then say "dry-run: $desc"; else say "  $desc"; "$@"; fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) CM_URL="$2"; shift 2 ;;
    --token) CM_TOKEN="$2"; shift 2 ;;
    --admin-token) ADMIN_TOKEN="$2"; shift 2 ;;
    --create-user) CREATE_USER="$2"; shift 2 ;;
    --name) USER_NAME="$2"; shift 2 ;;
    --token-name) TOKEN_NAME="$2"; shift 2 ;;
    --harness) ONLY="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) say "unknown option: $1" >&2; exit 2 ;;
  esac
done

export CM_ADMIN="$ADMIN_TOKEN"
[ -n "$CM_URL" ] || { printf 'claude-memory server URL [http://localhost:8080]: '; read -r CM_URL; CM_URL="${CM_URL:-http://localhost:8080}"; }
CM_URL="${CM_URL%/}"

api() { # api <method> <path> [json-body] [bearer-env-var] -> body on stdout
  # The bearer header goes through curl --config (stdin), never argv: argv is
  # world-readable in ps.
  local method="$1" path="$2" body="${3:-}" tok="${4:-CM_TOKEN}"
  local cfg="header = \"Content-Type: application/json\"\n"
  [ -n "${!tok:-}" ] && cfg+="header = \"Authorization: Bearer ${!tok}\"\n"
  [ -n "$body" ] && cfg+="data = \"$(printf '%s' "$body" | sed 's/"/\\\\\"/g')\""
  printf "$cfg" | curl -sS -m 15 --config - -X "$method" "$CM_URL$path"
}

install_file() { # like install -D but BSD-safe
  local src="$1" dst="$2"
  if [ "$DRY" = 1 ]; then say "dry-run: install $dst"; return; fi
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then say "  up to date: $dst"; return; fi
  say "  install $dst"
  mkdir -p "$(dirname "$dst")"
  install -m 644 "$src" "$dst"
}

mask() { # token fingerprint: prefix + length only, never the secret
  local t="$1"
  [ -n "$t" ] || { printf '(none)'; return; }
  printf '%s... (%d chars)' "${t:0:7}" "${#t}"
}

# ---------------------------------------------------------------- doctor ----
doctor() {
  local rc=0
  say "claude-memory doctor - server $CM_URL"

  local health
  if health=$(curl -sS -m 10 "$CM_URL/health" 2>&1); then
    say "  [ok]   /health reachable"
    printf '%s' "$health" | grep -q '"status": *"ok"' || say "  [warn] server reports degraded: $health"
  else
    say "  [FAIL] server unreachable: $health"; rc=1
  fi

  if [ -n "$CM_TOKEN" ]; then
    local who
    if who=$(api GET /api/auth/whoami "" CM_TOKEN 2>/dev/null) && printf '%s' "$who" | grep -q '"user_id"'; then
      say "  [ok]   token valid: $(printf '%s' "$who" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("user=%s role=%s via=%s" % (d["user_id"], d["role"], d["via"]))')"
      printf '%s' "$who" | grep -q token_expiring_soon && say "  [warn] token expires in <14 days"
    else
      say "  [FAIL] token rejected ($(mask "$CM_TOKEN"))"; rc=1
    fi
    local tools
    if tools=$(printf 'header = "Authorization: Bearer %s"\n' "$CM_TOKEN" \
        | curl -sS -m 15 --config - -X POST -H 'Content-Type: application/json' \
          -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$CM_URL/mcp" 2>/dev/null) && printf '%s' "$tools" | grep -q add_memory; then
      say "  [ok]   MCP /mcp answers tools/list"
    else
      say "  [FAIL] MCP /mcp not usable"; rc=1
    fi
  else
    say "  [warn] no token given (--token / AGENT_MEMORY_TOKEN); skipped token + MCP checks"
  fi

  local envf="$HOME/.config/agent-memory/env"
  if [ -f "$envf" ]; then
    local perms; perms=$(stat -f '%Lp' "$envf" 2>/dev/null || stat -c '%a' "$envf")
    [ "$perms" = 600 ] && say "  [ok]   $envf (mode 600)" || say "  [warn] $envf is mode $perms - chmod 600 it"
    grep -q '^AGENT_MEMORY_TOKEN=..' "$envf" || { say "  [FAIL] $envf has no token"; rc=1; }
  else
    say "  [warn] $envf missing (run: scripts/setup.sh setup)"
  fi
  [ -f "$HOME/.config/agent-memory/memory_hook.py" ] \
    && say "  [ok]   memory_hook.py installed" \
    || say "  [warn] ~/.config/agent-memory/memory_hook.py missing"

  local h
  for h in claude qwen codex pi opencode; do
    if hook_configured "$h"; then say "  [ok]   $h configured"; else say "  [ . ]  $h not configured (setup will add it if detected)"; fi
  done
  say "$([ "$rc" = 0 ] && echo 'doctor: all checks passed' || echo 'doctor: failures above')"
  return "$rc"
}

# ------------------------------------------------------------ detection ----
detected() { # detected <harness> -> 0 if in use
  local h="$1"
  if [ -n "$ONLY" ]; then
    printf '%s' ",$ONLY," | grep -q ",$h," || return 1
  fi
  case "$h" in
    claude)   command -v claude   >/dev/null 2>&1 || [ -d "$HOME/.claude" ] || [ -f "$HOME/.claude.json" ] ;;
    qwen)     command -v qwen     >/dev/null 2>&1 || [ -d "$HOME/.qwen" ] ;;
    codex)    command -v codex    >/dev/null 2>&1 || [ -d "$HOME/.codex" ] ;;
    pi)       command -v pi       >/dev/null 2>&1 || [ -d "$HOME/.pi" ] ;;
    opencode) command -v opencode >/dev/null 2>&1 || [ -d "$HOME/.config/opencode" ] ;;
    *) return 1 ;;
  esac
}

hook_configured() {
  local h="$1"
  case "$h" in
    claude)   [ -f "$HOME/.claude/settings.json" ] && grep -q 'agent-memory/memory_hook.py' "$HOME/.claude/settings.json" ;;
    qwen)     [ -f "$HOME/.qwen/settings.json" ] && grep -q 'agent-memory/memory_hook.py' "$HOME/.qwen/settings.json" ;;
    codex)    { [ -f "$HOME/.codex/config.toml" ] && grep -q 'agent-memory/memory_hook.py' "$HOME/.codex/config.toml"; } || { [ -f "$HOME/.codex/hooks.json" ] && grep -q 'agent-memory/memory_hook.py' "$HOME/.codex/hooks.json"; } ;;
    pi)       [ -f "$HOME/.pi/agent/extensions/agent-memory.ts" ] ;;
    opencode) [ -f "$HOME/.config/opencode/opencode.json" ] && grep -q 'claude-memory' "$HOME/.config/opencode/opencode.json" ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- setup ----
merge_hooks_json() { # merge adapters/<h>/settings.hooks.json into target
  local src="$1" dst="$2" harness="$3"
  CM_SRC="$src" CM_DST="$dst" CM_DRY="$DRY" python3 - <<'PY'
import json, os
src, dst, dry = os.environ["CM_SRC"], os.environ["CM_DST"], os.environ["CM_DRY"] == "1"
want = json.load(open(src))
cur = {}
if os.path.exists(dst):
    cur = json.load(open(dst))
added = []
hooks = cur.setdefault("hooks", {})
for event, groups in want["hooks"].items():
    entries = hooks.setdefault(event, [])
    have = json.dumps(entries)
    for g in groups:
        for h in g.get("hooks", []):
            if h["command"] not in have:
                g2 = dict(g); g2["hooks"] = [h]
                entries.append(g2)
                added.append(event)
if added and not dry:
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(dst) and not os.path.exists(dst + ".agent-memory.bak"):
        import shutil; shutil.copy2(dst, dst + ".agent-memory.bak")
    tmp = dst + ".tmp"
    json.dump(cur, open(tmp, "w"), indent=2)
    os.replace(tmp, dst)
for e in sorted(set(added)):
    print(f"  hook {e} -> {dst}")
if not added:
    print(f"  hooks already present in {dst}")
PY
}

merge_mcp_json() { # mcpServers entry: claude.json (claude) or opencode.json (opencode)
  local dst="$1" harness="$2"
  CM_DST="$dst" CM_H="$harness" CM_URL_M="$CM_URL" CM_TOK="$CM_TOKEN" CM_DRY="$DRY" python3 - <<'PY'
import json, os
dst, h = os.environ["CM_DST"], os.environ["CM_H"]
url, tok, dry = os.environ["CM_URL_M"], os.environ["CM_TOK"], os.environ["CM_DRY"] == "1"
cur = json.load(open(dst)) if os.path.exists(dst) else {}
if h == "claude":
    entry = {"type": "http", "url": url + "/mcp",
             "headers": {"Authorization": f"Bearer {tok}"}}
    servers = cur.setdefault("mcpServers", {})
    changed = servers.get("claude-memory") != entry
    servers["claude-memory"] = entry
else:  # opencode: remote MCP with headers
    entry = {"type": "remote", "url": url + "/mcp", "enabled": True,
             "headers": {"Authorization": f"Bearer {tok}"}}
    servers = cur.setdefault("mcp", {})
    changed = servers.get("claude-memory") != entry
    servers["claude-memory"] = entry
if changed and not dry:
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.exists(dst) and not os.path.exists(dst + ".agent-memory.bak"):
        import shutil; shutil.copy2(dst, dst + ".agent-memory.bak")
    tmp = dst + ".tmp"
    json.dump(cur, open(tmp, "w"), indent=2)
    os.replace(tmp, dst)
print("  " + ("MCP server added -> " + dst if changed else "MCP server already configured in " + dst))
PY
}

setup() {
  # token: create via admin if requested, else require one (prompt hidden)
  if [ -n "$ADMIN_TOKEN" ] && [ -n "$CREATE_USER" ] && [ "$DRY" != 1 ]; then
    local resp
    resp=$(api POST /api/users "{\"id\":\"$CREATE_USER\",\"name\":\"${USER_NAME:-$CREATE_USER}\",\"role\":\"user\"}" CM_ADMIN)
    if printf '%s' "$resp" | grep -q '"token"'; then
      CM_TOKEN=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
      say "user '$CREATE_USER' created; personal token stored (never shown)"
    else
      say "could not create user: $(printf '%s' "$resp" | head -c 200)" >&2; exit 1
    fi
  fi
  if [ -z "$CM_TOKEN" ] && [ "$DRY" != 1 ]; then
    printf 'personal token (cm_.., hidden) [empty = write placeholders to fix later]: '
    read -rs CM_TOKEN; echo
  fi
  if [ -n "$CM_TOKEN" ] && [ "$DRY" != 1 ]; then
    api GET /api/auth/whoami "" CM_TOKEN | grep -q '"user_id"' \
      || { say "token $(mask "$CM_TOKEN") rejected by $CM_URL - aborting" >&2; exit 1; }
    say "token accepted: $(mask "$CM_TOKEN")"
  fi

  # shared hook payload + env file (all harnesses read this)
  install_file "$REPO_DIR/adapters/hooks/memory_hook.py" "$HOME/.config/agent-memory/memory_hook.py"
  local envf="$HOME/.config/agent-memory/env"
  CM_ENV_URL="$CM_URL" CM_ENV_TOK="$CM_TOKEN" CM_ENVF="$envf" CM_DRY="$DRY" python3 - <<'PY'
import os, re
f, dry = os.environ["CM_ENVF"], os.environ["CM_DRY"] == "1"
lines = open(f).read().splitlines() if os.path.exists(f) else []
def setk(lines, key, val):
    pat = re.compile(rf"^{re.escape(key)}=.*$")
    out = [f"{key}={val}" if pat.match(l) else l for l in lines]
    if not any(pat.match(l) for l in out):
        out.append(f"{key}={val}")
    return out
if lines:
    lines = setk(setk(lines, "AGENT_MEMORY_URL", os.environ["CM_ENV_URL"]),
                 "AGENT_MEMORY_TOKEN", os.environ["CM_ENV_TOK"])
else:
    lines = ["# written by scripts/setup.sh - keep this file private (600)",
             f"AGENT_MEMORY_URL={os.environ['CM_ENV_URL']}",
             f"AGENT_MEMORY_TOKEN={os.environ['CM_ENV_TOK']}"]
if dry:
    print("  dry-run: would write " + f)
else:
    os.makedirs(os.path.dirname(f), exist_ok=True)
    open(f, "w").write("\n".join(lines) + "\n")
    os.chmod(f, 0o600)
    print(f"  wrote {f} (mode 600, token {len(os.environ['CM_ENV_TOK'])} chars kept private)")
PY

  local done_h=""
  for h in claude qwen codex pi opencode; do
    detected "$h" || continue
    done_h="$done_h $h"
    say "configuring $h"
    case "$h" in
      claude)
        merge_hooks_json "$REPO_DIR/adapters/claude-code/settings.hooks.json" "$HOME/.claude/settings.json" claude
        merge_mcp_json "${CLAUDE_CONFIG_FILE:-$HOME/.claude.json}" claude ;;
      qwen)
        merge_hooks_json "$REPO_DIR/adapters/qwen/settings.hooks.json" "$HOME/.qwen/settings.json" qwen ;;
      codex)
        local ct="$HOME/.codex/config.toml"
        if [ -f "$ct" ] && grep -q 'agent-memory/memory_hook.py' "$ct"; then
          say "  hooks already present in $ct"
        else
          if [ "$DRY" != 1 ]; then
            [ -f "$ct" ] && [ ! -f "$ct.agent-memory.bak" ] && cp "$ct" "$ct.agent-memory.bak"
          fi
          if [ "$DRY" = 1 ]; then say "dry-run: append codex hook block to $ct"; else
            mkdir -p "$HOME/.codex"
            printf '\n%s\n' "# added by claude-memory scripts/setup.sh" >> "$ct"
            grep -v '^#' "$REPO_DIR/adapters/codex/config.hooks.toml" >> "$ct"
            say "  appended hooks to $ct (trust them once in Codex with /hooks)"
          fi
        fi ;;
      pi)
        install_file "$REPO_DIR/adapters/pi/agent-memory.ts" "$HOME/.pi/agent/extensions/agent-memory.ts" ;;
      opencode)
        merge_mcp_json "$HOME/.config/opencode/opencode.json" opencode ;;
    esac
  done
  [ -n "$done_h" ] || say "no harness detected (looked for: claude qwen codex pi opencode; use --harness to force)"

  say ""
  say "done:$done_h"
  say "verify with: scripts/setup.sh doctor --url $CM_URL"
  say "remember: add one checkpoint line to each harness's global instructions"
  say "(see adapters/README.md); Codex needs /hooks once to trust new hooks."
}

if [ "$MODE" = doctor ]; then doctor; else setup; fi
