# Per-device token migration (shared bearer → per-user tokens)

Context: every hook and MCP client in the homelab currently authenticates
with one shared `BEARER_TOKEN`. The server now supports per-user API tokens
(`cm_<id>_<secret>`, shown once, revocable individually). The shared token is
registered in the admin token view as **`legacy-hooks`** (user `default`);
revoking it stops *every* remaining client at once, so the admin UI asks for
a typed confirmation (`revoke-all-hooks`) before allowing that.

## Migrate one device

1. In the UI, open **My tokens** → *New token*. Name it after the device
   (e.g. `mac-office`), pick an expiry (90d default), set the harness if the
   device runs a single one. Copy the value immediately — it is shown once.
2. On the device, replace the shared token in `~/.config/agent-memory/env`:

   ```
   AGENT_MEMORY_URL=https://app.example.internal
   AGENT_MEMORY_TOKEN=cm_tXXXXXXXX_...   # the new personal token
   ```

   The MCP entry in `~/.claude.json` (or `~/.codex/`, `~/.qwen/`) uses the
   same env file — no separate change unless it hardcodes the bearer.
3. Restart the harness session. The first hook call proves it:

   ```
   curl -s -H "Authorization: Bearer $(grep AGENT_MEMORY_TOKEN ~/.config/agent-memory/env | cut -d= -f2)" \
     https://app.example.internal/api/sessions?limit=1 -o /dev/null -w '%{http_code}\n'
   ```

   Expect `200`. In the admin **Tokens** view the token's *last used* ticks.
4. Only when every device is migrated (admin Tokens view: no recent
   `last_used` needed for `legacy-hooks`), revoke `legacy-hooks` with the
   typed confirmation. Keep it until then — it is the rollback path.

## Notes

- A lost device is handled by revoking **its** token in **My tokens** (or the
  admin view) — no fleet impact. That is the whole point of the migration.
- Disabling a user revokes all of that user's tokens automatically.
- The `default` service user has no browser login and no per-user token of
  its own; do not delete it — its `legacy-hooks` row is the shared token's
  revocation switch.
- Do not name a Keycloak account `default` (the username would link to the
  service user row on first login).
- The config-bearer path is now DB-dependent (revocation check); during a DB
  outage hooks could not read or write anyway. `ADMIN_TOKEN` (if set) stays
  config-only as break-glass.
