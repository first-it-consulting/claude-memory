# Multi-tenancy — rollout & rollback runbook

Prepared 2026-11-19 on branch `feat/multi-tenancy` (server + claude-memory-ui).
Backup tags: `backup/pre-multi-tenancy` in both repos (server `dc51ce1`, UI `45cb379`).

## What the migration touches

All schema changes are **additive and idempotent**, applied automatically at
server start:

- new table `users` (tenant registry + hashed API tokens)
- `session_state` gains column `owner TEXT NOT NULL DEFAULT 'default'` (+ index)
- memories keep living in mem0's `user_id` namespace — existing data stays
  `user_id='default'`; **nothing is re-embedded or rewritten**

Because nothing destructive happens, an **old server image keeps working
against a migrated database** — that is the rollback guarantee.

## Rollout

1. Deploy the new image with at least `ADMIN_TOKEN` set (long random string,
   keep it in the same secret as `BEARER_TOKEN`).
   Add the Keycloak settings for the UI: `OIDC_ISSUER`, `OIDC_CLIENT_ID`
   (audience must be the client id), `OIDC_ADMIN_ROLES`.
2. Existing agents keep using `BEARER_TOKEN` — it now maps to tenant
   `default` with role `user`. Nothing breaks.
3. Create per-user tenants and hand out tokens:
   `POST /api/users {"id": "alice", "name": "Alice"}` (admin token).
   Each user points their harness adapter's `MEMORY_TOKEN` at their own token.
4. Browser users log in via the UI (Keycloak). First login provisions their
   account; grant admin via the realm role `claude-memory-admin` if needed.
5. Optionally publish shared homelab facts:
   `POST /api/memories {"content": "...", "shared": true}` (admin).
6. Retire `BEARER_TOKEN` once all adapters use personal tokens: set it to a
   random value so the shared secret stops working (or remove the env var).

## Rollback

1. Redeploy the previous image (tag `backup/pre-multi-tenancy` build, or
   whatever `rolling` pointed at before). The additive schema is ignored.
2. Caveat: memories written by *personal tokens* live in their own tenants and
   are invisible to the old single-tenant server (it only reads `default`).
   They are **not lost** — they become visible again when the multi-tenant
   image is redeployed, or can be merged back into `default` with:

   ```sql
   -- merge one workspace back into default (check for hash collisions first)
   UPDATE mem0 SET payload = jsonb_set(payload, '{user_id}', '"default"')
   WHERE payload->>'user_id' = 'alice';
   ```

3. To wipe the multi-tenancy state entirely (only if rolling back for good):

   ```sql
   DROP TABLE IF EXISTS users;
   ALTER TABLE session_state DROP COLUMN IF EXISTS owner;
   ```

## Emergency access

`ADMIN_TOKEN` bypasses the users table and Keycloak entirely (it is compared
in the server process). If Keycloak or the DB is down but the server runs,
the admin token still works for `/api/*` and `/mcp`.
