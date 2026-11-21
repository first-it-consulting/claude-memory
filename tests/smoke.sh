#!/usr/bin/env bash
# End-to-end smoke test against `docker compose -f compose.yaml -f compose.test.yaml up -d --wait`.
# Real: server image, postgres+pgvector, HTTP/MCP/auth. Faked: embeddings + LLM
# extraction (tests/ollama_stub.py - fixed 768-dim vector, fixed extracted fact).
set -uo pipefail
BASE=http://localhost:8080
ADMIN=smoke-admin-token
TS=$(date +%s)
COMPOSE="docker compose -f compose.yaml -f compose.test.yaml"

api() { # method token path [json]
  if [ -n "${4:-}" ]; then
    curl -sS -m 15 -X "$1" -H "Authorization: Bearer $2" -H 'Content-Type: application/json' -d "$4" "$BASE$3"
  else
    curl -sS -m 15 -X "$1" -H "Authorization: Bearer $2" "$BASE$3"
  fi
}
code() { curl -sS -m 15 -o /dev/null -w '%{http_code}' -X "$1" -H "Authorization: Bearer $2" "$BASE$3"; }
ok() { echo "PASS: $1"; }
die() {
  echo "FAIL: $1"
  echo "--- response: ${2:-}"
  $COMPOSE logs --tail 40 server 2>&1 | tail -25
  exit 1
}

# 1 health
for _ in $(seq 1 60); do
  curl -sS -m 5 "$BASE/health" 2>/dev/null | grep -Eq '"status": *"ok"' && break
  sleep 2
done
curl -sS "$BASE/health" | grep -Eq '"status": *"ok"' || die "server never got healthy" ""
ok "GET /health"

# 2 auth: admin token + unknown token rejected
ROLE=$(api GET "$ADMIN" /api/auth/whoami | jq -r .role)
[ "$ROLE" = "admin" ] || die "whoami admin" "$ROLE"
[ "$(code GET definitely-not-a-token /api/memories)" = "401" ] || die "unknown token should 401" ""
ok "whoami(admin) + 401 for unknown token"

# 3 second + third tenant via admin user management
BOB_JSON=$(api POST "$ADMIN" /api/users '{"id":"bob","name":"Bob","role":"user"}')
BOB_T=$(jq -r '.token // empty' <<<"$BOB_JSON")
[ -n "$BOB_T" ] || die "create bob" "$BOB_JSON"
CAROL_JSON=$(api POST "$ADMIN" /api/users '{"id":"carol","name":"Carol","role":"user"}')
CAROL_T=$(jq -r '.token // empty' <<<"$CAROL_JSON")
[ -n "$CAROL_T" ] || die "create carol" "$CAROL_JSON"
BOB_UID=$(api GET "$BOB_T" /api/auth/whoami | jq -r .user_id)
[ "$BOB_UID" = "bob" ] || die "bob whoami scoped" "$BOB_UID"
ok "POST /api/users creates tenants bob+carol (scoped whoami)"

# 4 self-service PAT
PAT_JSON=$(api POST "$BOB_T" /api/me/tokens '{"name":"smoke","expires_days":30}')
PAT=$(jq -r '.token // empty' <<<"$PAT_JSON")
case "$PAT" in cm_*) ;; *) die "PAT mint" "$PAT_JSON";; esac
ok "POST /api/me/tokens returns plaintext PAT"

# 5 MCP streamable-HTTP
TOOLS=$(curl -sS -m 15 -X POST -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$BASE/mcp")
grep -q add_memory <<<"$TOOLS" || die "MCP tools/list" "$TOOLS"
ok "POST /mcp tools/list"

# 6 two concurrent exact writes (MCP, infer=false): the first two writes on a
# fresh DB used to race inside the pgvector collection setup
# (pg_type_typname_nsp_index) and one job was dropped - bean v4al regression.
# (REST POST always queues with infer=true, so both go through MCP here, which
# honours infer=false and stores the exact text.)
FACT="smoke-exact-fact-$TS"
FACT2="smoke-exact-fact2-$TS"
mcp_add() { # token content outfile
  curl -sS -m 15 -X POST -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"add_memory\",\"arguments\":{\"content\":\"$2\",\"infer\":false}}}" "$BASE/mcp" >"$3"
}
R1=$(mktemp) R2=$(mktemp)
mcp_add "$PAT" "$FACT" "$R1" & MCP_PID=$!
mcp_add "$PAT" "$FACT2" "$R2" & MCP2_PID=$!
wait $MCP_PID || die "MCP add_memory request" ""
wait $MCP2_PID || die "MCP add_memory 2 request" ""
RES=$(cat "$R1"); RES2=$(cat "$R2"); rm -f "$R1" "$R2"
grep -q queued <<<"$RES" || die "MCP add_memory" "$RES"
grep -q queued <<<"$RES2" || die "MCP add_memory 2" "$RES2"
MID=""
for _ in $(seq 1 40); do
  LIST=$(api GET "$PAT" /api/memories)
  MID=$(jq -r --arg f "$FACT" '[.[] | select(.memory | contains($f))][0].id // empty' <<<"$LIST")
  MID2=$(jq -r --arg f "$FACT2" '[.[] | select(.memory | contains($f))][0].id // empty' <<<"$LIST")
  [ -n "$MID" ] && [ -n "$MID2" ] && break
  sleep 2
done
[ -n "$MID" ] || die "memory never appeared in list" "$LIST"
[ -n "$MID2" ] || die "concurrent second add dropped (v4al race)" "$LIST"
ok "concurrent first MCP adds both stored (pgvector write, no catalog race)"

# 7 semantic search path (embed stub -> pgvector cosine)
api GET "$PAT" "/api/memories?q=$FACT" | grep -q "$FACT" || die "search q=" ""
ok "GET /api/memories?q= finds the memory"

# 8 cross-tenant isolation
if api GET "$CAROL_T" /api/memories | grep -q "$FACT"; then
  die "carol must not see bob's memory" "$(api GET "$CAROL_T" /api/memories)"
fi
[ "$(code DELETE "$CAROL_T" "/api/memories/$MID")" = "404" ] || die "cross-tenant delete must 404" ""
ok "cross-tenant isolation (list clean, foreign delete 404)"

# 9 session checkpoint write/read (plain SQL path)
api PUT "$BOB_T" /api/sessions/smoke-s1 "{\"state\":\"checkpoint-$TS\",\"project\":\"smoke\"}" >/dev/null
api GET "$BOB_T" /api/sessions/smoke-s1 | grep -q "checkpoint-$TS" || die "session read-back" ""
[ "$(code GET "$CAROL_T" /api/sessions/smoke-s1)" = "404" ] || die "cross-tenant session must 404" ""
ok "PUT/GET /api/sessions checkpoint (+ cross-tenant 404)"

# 10 REST add (async queue + infer path through stubbed chat/LLM)
BEFORE=$(api GET "$PAT" /api/memories | jq 'length')
Q=$(api POST "$PAT" /api/memories '{"content":"rest add smoke"}')
grep -q '"queued"' <<<"$Q" || die "REST add response" "$Q"
AFTER=$BEFORE
for _ in $(seq 1 40); do
  AFTER=$(api GET "$PAT" /api/memories | jq 'length')
  [ "$AFTER" -gt "$BEFORE" ] && break
  sleep 2
done
[ "$AFTER" -gt "$BEFORE" ] || die "queued REST add never stored ($BEFORE -> $AFTER)" ""
ok "POST /api/memories queued, worker stored via stubbed LLM"

# 11 revoked PAT must 401
RID=$(jq -r .id <<<"$PAT_JSON")
api DELETE "$BOB_T" "/api/me/tokens/$RID" >/dev/null
[ "$(code GET "$PAT" /api/memories)" = "401" ] || die "revoked PAT must 401" ""
ok "revoked PAT -> 401"

# 12 owner delete works
api DELETE "$BOB_T" "/api/memories/$MID" | grep -q '"ok"' || die "owner delete" ""
if api GET "$BOB_T" "/api/memories?q=$FACT" | grep -q "$FACT"; then
  die "deleted memory still searchable" ""
fi
ok "DELETE /api/memories/{id} removes it"

echo ALL-SMOKE-PASSED
