#!/usr/bin/env bash
# claude-memory harness setup + doctor (bean yws4).
#
#   scripts/setup.sh setup  [--url URL] [--token cm_..] [options]   configure
#   scripts/setup.sh doctor [--url URL] [--token cm_..]             verify
#
# Detects installed harnesses (Claude Code, Qwen Code, Codex, pi, opencode),
# writes the shared hook env + per-harness MCP/hook config with a per-user
# token, and verifies connectivity. Idempotent: re-running changes nothing.
# --dry-run prints every planned action and writes nothing. --force overrides
# the per-harness-token guards: replaces an existing MCP token and rewrites
# the env token even when the file already holds a different (working) one.
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
FORCE=0

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
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

api_whoami() { # -> whoami JSON on stdout ("" if not the server's answer)
  # A login proxy in front of the server 302-redirects /api/auth/whoami to
  # SSO even with a valid bearer (only /mcp, /api/hooks, /api/sessions pass
  # through). So: try whoami first (exact user/role output), fall back to a
  # POST to /mcp - which every server answers with JSON at HTTP 200 and
  # which ingress authors must keep bearer-reachable - and accept only a
  # real JSON body. Redirects/HTML can no longer look healthy (bean sazT).
  local out
  out=$(api GET /api/auth/whoami "" CM_TOKEN 2>/dev/null)
  printf '%s' "$out" | grep -q '"user_id"' && { printf '%s' "$out"; return 0; }
  out=$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\ndata = "{\\"jsonrpc\\":\\"2.0\\",\\"id\\":1,\\"method\\":\\"tools/list\\"}"\n' "$CM_TOKEN" \
      | curl -sS -m 15 --config - -X POST "$CM_URL/mcp" -H 'Accept: application/json, text/event-stream' 2>/dev/null)
  printf '%s' "$out" | grep -q '"result"' && printf '%s' "$out"
}

http_get_status() { # -> "<code> <body>" ('000' = transport error)
  # curl -w reports the real HTTP status; a 200-with-login-HTML or a 3xx
  # redirect can never masquerade as health anymore (bean sazT).
  local url="$1" out rc
  out=$(curl -sS -m 15 -w '\n%{http_code}' "$url" 2>/dev/null); rc=$?
  if [ $rc -ne 0 ]; then printf '000 '; return 0; fi
  printf '%s %s' "${out##*$'\n'}" "${out%$'\n'*}"
}

url_origin() { # scheme://host[:port] of an http(s) URL, lowercased ('' if none)
  printf '%s' "$1" | sed -E 's#^[[:space:]]*##; s#^(https?://[^/?#]+).*#\1#' | tr '[:upper:]' '[:lower:]' | grep -E '^https?://' || true
}

tok_probe() { # tok_probe <token> -> ok|reject|unverified; never prints the token
  # ok: whoami/MCP answered the server's JSON with this bearer.
  # reject: the MCP endpoint explicitly refused it (401/403).
  # unverified: transport error or any other answer (login proxy, 5xx) - the
  # caller must treat this as 'do not overwrite' (fail-closed, security review).
  local t="$1" out code
  out=$(CM_TOKEN="$t" api_whoami 2>/dev/null)
  printf '%s' "$out" | grep -qE '"user_id"|"result"' && { printf ok; return; }
  code=$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\ndata = "{\\"jsonrpc\\":\\"2.0\\",\\"id\\":1,\\"method\\":\\"tools/list\\"}"\n' "$t" \
    | curl -sS -m 15 --config - -o /dev/null -w '%{http_code}' -X POST "$CM_URL/mcp" \
        -H 'Accept: application/json, text/event-stream' 2>/dev/null) || code=000
  case "$code" in
    401|403) printf reject ;;
    2*) printf ok ;;
    *) printf unverified ;;
  esac
}

install_file() { # like install -D but BSD-safe; 'updated' when content differs
  local src="$1" dst="$2" verb=install
  [ -f "$dst" ] && verb=updated
  if [ "$DRY" = 1 ]; then say "dry-run: $verb $dst"; return; fi
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then say "  up to date: $dst"; return; fi
  say "  $verb $dst"
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

  # /health is usually behind a login proxy (302 to SSO) - a redirect is NOT
  # a healthy answer and HTML must never be dumped. Probe by HTTP status +
  # body: /health (direct installs), then /api/version, then a bearer POST to
  # /mcp (the path ingress authors must keep reachable for agents).
  local res code body checked="$CM_URL/health" last_code=""
  res=$(http_get_status "$CM_URL/health"); code=${res%% *}; body=${res#* }
  if [ "$code" != 200 ]; then
    last_code="$code"
    checked="$CM_URL/api/version"
    res=$(http_get_status "$CM_URL/api/version"); code=${res%% *}; body=${res#* }
  fi
  if [ "$code" != 200 ] && [ -n "$CM_TOKEN" ]; then
    last_code="$code"
    checked="$CM_URL/mcp"
    body=$(api_whoami)
    if printf '%s' "$body" | grep -q '"result"'; then code=200; else code=err; fi
  fi
  if [ "$code" = 000 ]; then
    say "  [FAIL] server unreachable: $checked"; rc=1
  elif [ "$code" = err ]; then
    say "  [FAIL] no bearer-reachable path answered the server's JSON (last saw HTTP $last_code on $checked: login redirect or error)"; rc=1
  elif [ "$code" != 200 ]; then
    say "  [FAIL] $checked answered HTTP $code (login redirect or error, not the server)"; rc=1
  elif printf '%s' "$body" | grep -qE '"status": *"ok"|"version"|"result"'; then
    say "  [ok]   server reachable ($checked, HTTP 200)"
  else
    say "  [FAIL] $checked HTTP 200 but body is not the server's JSON (login page?)"; rc=1
  fi

  if [ -n "$CM_TOKEN" ]; then
    local who
    if who=$(api_whoami) && printf '%s' "$who" | grep -q '"user_id"'; then
      say "  [ok]   token valid: $(printf '%s' "$who" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("user=%s role=%s via=%s" % (d["user_id"], d["role"], d["via"]))')"
      printf '%s' "$who" | grep -q token_expiring_soon && say "  [warn] token expires in <14 days"
    elif [ -n "$who" ]; then
      say "  [ok]   token valid (MCP answers; whoami hidden behind login proxy)"
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
  # An existing entry with a DIFFERENT token is kept unless --force (bean
  # 3mup: per-harness setups must not clobber each other's tokens silently).
  # Messages never print token material - only 'added'/'replaced'/'kept'.
  local dst="$1" harness="$2"
  CM_DST="$dst" CM_H="$harness" CM_URL_M="$CM_URL" CM_TOK="$CM_TOKEN" CM_DRY="$DRY" CM_FORCE="$FORCE" python3 - <<'PY'
import json, os
dst, h = os.environ["CM_DST"], os.environ["CM_H"]
url, tok, dry = os.environ["CM_URL_M"], os.environ["CM_TOK"], os.environ["CM_DRY"] == "1"
force = os.environ["CM_FORCE"] == "1"
cur = json.load(open(dst)) if os.path.exists(dst) else {}
key = "mcpServers" if h == "claude" else "mcp"
if h == "claude":
    entry = {"type": "http", "url": url + "/mcp",
             "headers": {"Authorization": f"Bearer {tok}"}}
else:  # opencode: remote MCP with headers
    entry = {"type": "remote", "url": url + "/mcp", "enabled": True,
             "headers": {"Authorization": f"Bearer {tok}"}}
servers = cur.setdefault(key, {})
old = servers.get("claude-memory")
if old is None:
    action, msg = "add", "MCP server added -> " + dst
elif old == entry:
    action, msg = "none", "MCP server already configured in " + dst
elif old.get("url") != entry["url"]:
    # different server URL: a real config change, not a token swap
    action, msg = "replace", "MCP server replaced (URL changed) -> " + dst
else:
    # same URL, different token (or headers differ): guard it
    if not force:
        action = "keep"
        msg = ("[warn] MCP entry in " + dst + " holds a different token - kept;"
               " re-run with --force to replace it")
    else:
        action, msg = "replace", "MCP token replaced in " + dst
if dry:
    print("  dry-run: would " + {"add": "add MCP server", "none": "leave MCP server unchanged",
          "replace": "replace MCP server entry", "keep": "keep existing MCP token (use --force to replace)"}[action]
          + (" in " + dst if action != "add" else " -> " + dst))
elif action in ("add", "replace"):
    servers["claude-memory"] = entry
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.exists(dst) and not os.path.exists(dst + ".agent-memory.bak"):
        import shutil; shutil.copy2(dst, dst + ".agent-memory.bak")
    tmp = dst + ".tmp"
    json.dump(cur, open(tmp, "w"), indent=2)
    os.replace(tmp, dst)
    print("  " + msg)
else:
    print("  " + msg)
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
    api_whoami | grep -qE '"user_id"|"result"' \
      || { say "token $(mask "$CM_TOKEN") rejected by $CM_URL (or its API path is hidden behind a login proxy) - aborting" >&2; exit 1; }
    say "token accepted: $(mask "$CM_TOKEN")"
  fi

  # shared hook payload + env file (all harnesses read this)
  install_file "$REPO_DIR/adapters/hooks/memory_hook.py" "$HOME/.config/agent-memory/memory_hook.py"
  local envf="$HOME/.config/agent-memory/env" env_tok="" env_url="" keep_reason=""
  # Bean 3mup + security review: per-harness runs pass different tokens, so a
  # foreign token in env is only ever REPLACED when the server that the env
  # file itself points at explicitly rejected it. Never send the stored token
  # to another host (env URL must match this run's URL), and on transport
  # errors or ambiguous answers keep the file + warn instead of overwriting
  # blindly (fail-closed). --force overrides everything.
  if [ -f "$envf" ]; then
    env_tok=$(grep '^AGENT_MEMORY_TOKEN=' "$envf" | tail -1 | cut -d= -f2- || true)
    env_url=$(grep '^AGENT_MEMORY_URL=' "$envf" | tail -1 | cut -d= -f2- || true)
    if [ -n "$env_tok" ] && [ "$env_tok" != "$CM_TOKEN" ] && [ "$FORCE" != 1 ]; then
      if [ "$DRY" = 1 ]; then keep_reason=dry
      elif [ "$(url_origin "$env_url")" != "$(url_origin "$CM_URL")" ]; then keep_reason=other
      else
        case "$(tok_probe "$env_tok")" in
          ok) keep_reason=works ;;
          reject) keep_reason="" ;;
          *) keep_reason=unverified ;;
        esac
      fi
    fi
  fi
  case "$keep_reason" in
    dry) say "  dry-run: would keep the different token already in $envf (use --force to replace)" ;;
    other) say "  [warn] $envf holds a token for a different server - kept; re-run with --force to replace it" ;;
    works) say "  [warn] $envf holds a different token that still works - kept; re-run with --force to replace it" ;;
    unverified) say "  [warn] could not verify the existing token in $envf (server unreachable or ambiguous answer) - kept; re-run with --force to replace it" ;;
    *)
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
      ;;
  esac

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
