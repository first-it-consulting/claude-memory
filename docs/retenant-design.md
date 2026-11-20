# Re-tenanting the legacy `default` data (decision needed)

## Problem

Before multi-tenancy, everything was written under tenant `default` (the
shared `BEARER_TOKEN`'s tenant). Live counts (2026-11-20):

| table          | tenant col             | `default` |
|----------------|------------------------|-----------|
| mem0           | payload->>'user_id'    | 195       |
| mem0_archive   | payload->>'user_id'    | 11        |
| session_state  | owner                  | 57        |

`tag_rules` and `job_runs` are global; `api_tokens.user_id` /
`token_audit.user_id` reference user ids, not tenants — not in scope.

Migrating a device to a personal token switches its tenant to the user's
workspace (`helge`), so a migrated device sees **none** of the 195
memories and starts writing into a split tenant. The device migration
(P3 finish) and this data decision are coupled.

## Options

**A — Move `default` -> `helge` (recommended).**
One UPDATE pass (`scripts/retenant.py`), one transaction, in-DB backup
table + restore mode. History becomes the user's personal workspace; the
shared space stays empty for deliberately shared memories. After the
shared `legacy-hooks` token is retired (P4), nothing writes to `default`
anymore. Until then, any device still on the shared token writes to an
empty `default` — acceptable for the short tail, and the admin scope=all
view sees both.

**A2 — Move `default` -> `shared`.**
Same mechanics; history becomes fleet-visible instead of personal. Right
choice if other users are ever onboarded and this history is not
personal. The 195 notes are personal work history — A fits better.

**B — Leave the data; admins read via scope=all.**
No data change. But personal devices keep writing to `helge` while old
data sits in `default`: permanently split history for the main user, and
non-admin users could never read the old data at all. Half-measure.

**C — Let `helge`'s tokens also read `default` (code change).**
Read-scope special case for one user — violates the tenant model, still
splits writes, and needs forever code. Rejected.

## Execution plan for A (only after the user's OK)

1. **Offline backup** — pg_dump via a one-shot Job (the app image has no
   pg_dump; CNPG barman backups exist, this is the targeted extra):
   Job image `postgres:16-alpine`, command
   `pg_dump $DATABASE_URL -t mem0 -t mem0_archive -t session_state -F c -f /dump/pre-retenant.dump`
   onto a PVC, verified non-empty. (Runbook in the PR.)
2. **Dry-run**: `retenant.py --source default --target helge` — prints
   per-table counts for both tenants and the count of identical memory
   texts that would coexist post-merge.
3. **Apply**: `--apply` — single transaction, rowcount guard, in-DB
   backup table `retenant_bak_<ts>` written first.
4. **Verify**: counts moved (195/11/57), whoami via a helge token sees
   memories via /api/memories, sessions page shows the history.
5. **Rollback** (if anything is off): `--restore <ts>` replays the backup
   table; worst case, restore the three tables from the pg_dump.

Duplicates: after the merge, notes written to `helge` before the move can
coexist with identical old ones (dedup is write-time only) — dry-run
prints the overlap so the user knows what to expect.
