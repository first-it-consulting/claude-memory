#!/usr/bin/env python3
"""Move all tenant-keyed rows from one workspace to another.

Tenant-keyed tables (live schema, verified 2026-11-20):
  mem0           payload->>'user_id'
  mem0_archive   payload->>'user_id'
  session_state  owner
NOT touched: api_tokens / token_audit (user ids, not tenants), tag_rules
and job_runs (global), users.

Safe by construction:
  * dry-run by default — prints per-table counts for source and target and
    the cross-effects (e.g. would-be duplicate content), changes nothing;
  * --apply wraps everything in ONE transaction and first copies every
    affected row into a timestamped backup table retenant_bak_<tag>
    (tbl, pk, row) — a restore path that lives in the same database and
    survives independently of any file;
  * --restore <tag> replays that table (payloads and owners exactly as
    they were);
  * an OFFLINE dump is still recommended before applying (pg_dump via a
    one-shot Job, see docs/retenant-design.md).

Conflicts: session_state's PK is session_id and mem0's is id — rewriting
the tenant column can never collide on a PK. The real "conflict" is
semantic: after a merge the target sees source ∪ target memories, so
near-duplicates that were written while the tenants were split both show
up (dedup only runs at write time). Dry-run flags identical payload
"data" texts across the two tenants.

Run in-cluster (image has psycopg2, no pg_dump):
  DATABASE_URL=... python scripts/retenant.py --source default --target my-user
  DATABASE_URL=... python scripts/retenant.py --source default --target my-user --apply
  DATABASE_URL=... python scripts/retenant.py --restore 20261120T090000
"""

import argparse
import os
import re
import sys

import psycopg2

TENANT_TABLES = ("mem0", "mem0_archive", "session_state")


def _counts(cur, tenant):
    out = {}
    cur.execute("SELECT count(*) FROM mem0 WHERE payload->>'user_id' = %s", (tenant,))
    out["mem0"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM mem0_archive WHERE payload->>'user_id' = %s", (tenant,)
    )
    out["mem0_archive"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM session_state WHERE owner = %s", (tenant,))
    out["session_state"] = cur.fetchone()[0]
    return out


def _dup_texts(cur, source, target):
    cur.execute(
        """SELECT count(*) FROM mem0 a JOIN mem0 b
               ON b.payload->>'user_id' = %s
              AND b.payload->>'data' = a.payload->>'data'
           WHERE a.payload->>'user_id' = %s""",
        (target, source),
    )
    return cur.fetchone()[0]


def _backup_table(cur, tag):
    name = f"retenant_bak_{tag}"
    cur.execute(
        f"CREATE TABLE {name} (tbl TEXT NOT NULL, pk TEXT NOT NULL, row JSONB NOT NULL)"
    )
    return name


def _snapshot(cur, bak, source):
    cur.execute(
        f"INSERT INTO {bak} (tbl, pk, row) "
        "SELECT 'mem0', id::text, payload FROM mem0 WHERE payload->>'user_id' = %s",
        (source,),
    )
    cur.execute(
        f"INSERT INTO {bak} (tbl, pk, row) "
        "SELECT 'mem0_archive', id::text, payload FROM mem0_archive WHERE payload->>'user_id' = %s",
        (source,),
    )
    cur.execute(
        f"INSERT INTO {bak} (tbl, pk, row) "
        "SELECT 'session_state', session_id, to_jsonb(s) FROM session_state s WHERE owner = %s",
        (source,),
    )


def _restore(cur, tag, source):
    if not re.fullmatch(r"[0-9T]+", tag):
        sys.exit("bad tag")
    bak = f"retenant_bak_{tag}"
    cur.execute(f"SELECT count(*) FROM {bak}")  # fails loudly if the tag is wrong
    cur.execute(
        f"UPDATE mem0 m SET payload = b.row FROM {bak} b "
        "WHERE b.tbl = 'mem0' AND m.id::text = b.pk"
    )
    cur.execute(
        f"UPDATE mem0_archive m SET payload = b.row FROM {bak} b "
        "WHERE b.tbl = 'mem0_archive' AND m.id::text = b.pk"
    )
    cur.execute(
        f"UPDATE session_state s SET owner = b.row->>'owner' FROM {bak} b "
        "WHERE b.tbl = 'session_state' AND s.session_id = b.pk"
    )
    print("restored from", bak, "(backup table kept for audit)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="default")
    ap.add_argument("--target")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", metavar="TAG")
    args = ap.parse_args()
    if not args.restore and not args.target:
        ap.error("--target is required unless --restore is used")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    if args.restore:
        _restore(cur, args.restore, args.source)
        conn.commit()
        print("counts after restore:", _counts(cur, args.source))
        return

    if args.source == args.target:
        sys.exit("source == target")

    src, tgt = _counts(cur, args.source), _counts(cur, args.target)
    print(f"source {args.source!r}: {src}")
    print(f"target {args.target!r}: {tgt}")
    print(
        "identical memory texts across tenants (would coexist):",
        _dup_texts(cur, args.source, args.target),
    )

    if not args.apply:
        print("DRY RUN — nothing changed. Add --apply to move.")
        return

    import datetime

    tag = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
    bak = _backup_table(cur, tag)
    _snapshot(cur, bak, args.source)
    cur.execute(
        "UPDATE mem0 SET payload = jsonb_set(payload, '{user_id}', to_jsonb(%s::text)) "
        "WHERE payload->>'user_id' = %s",
        (args.target, args.source),
    )
    n1 = cur.rowcount
    cur.execute(
        "UPDATE mem0_archive SET payload = jsonb_set(payload, '{user_id}', to_jsonb(%s::text)) "
        "WHERE payload->>'user_id' = %s",
        (args.target, args.source),
    )
    n2 = cur.rowcount
    cur.execute(
        "UPDATE session_state SET owner = %s WHERE owner = %s",
        (args.target, args.source),
    )
    n3 = cur.rowcount
    expected = src["mem0"] + src["mem0_archive"] + src["session_state"]
    if n1 + n2 + n3 != expected:
        conn.rollback()
        sys.exit(f"rowcount mismatch ({n1},{n2},{n3}) != {expected} — rolled back")
    conn.commit()
    print(f"applied: mem0={n1} mem0_archive={n2} session_state={n3}; backup {bak}")
    print("rollback: python scripts/retenant.py --restore", tag)


if __name__ == "__main__":
    main()
