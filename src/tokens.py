"""Per-user API tokens (PATs) behind the cm_ bearer scheme.

One api_tokens row = one token: shown once, stored as a SHA-256 hash (the
secrets are 256-bit urlsafe random, so a plain hash is the right primitive —
no pepper, no KDF). The format cm_<id>_<secret> gives leak scanners a marker
and lets the resolver route straight to the row by id; the legacy per-user
shape cm_<random> (users.token_hash) keeps resolving through its own path,
untouched.

Expiry is the only NEW rejection path (revoked/expired rows) — log every
rejection with the id segment so a mis-migrated device is one log line to
diagnose. Audit rows are append-only; nothing here updates or deletes them.
"""
import hashlib
import logging
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from config import settings

logger = logging.getLogger(__name__)

TOKEN_COLUMNS = ("id, user_id, name, prefix, last4, can_admin, default_harness, "
                 "created_at, created_by, expires_at, last_used_at, revoked_at, revoked_by")
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 365
NEVER = "infinity"

_ID_RE = re.compile(r"^t[0-9a-f]{8}$")

_schema_ok = False
_schema_lock = threading.Lock()


def _db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(settings.database_url)


def ensure_table() -> None:
    """Create api_tokens + token_audit lazily, idempotent and additive-only
    (an old server image keeps working against a migrated database). Also
    imports existing users.token_hash rows once as legacy api_tokens so they
    show up in the admin token view; the users.token_hash column itself is
    never touched and keeps resolving exactly as before."""
    global _schema_ok
    if _schema_ok:
        return
    with _schema_lock:
        if _schema_ok:
            return
        conn = _db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS api_tokens (
                            id              TEXT PRIMARY KEY,
                            user_id         TEXT NOT NULL,
                            name            TEXT NOT NULL DEFAULT '',
                            prefix          TEXT NOT NULL,
                            last4           TEXT NOT NULL,
                            token_hash      TEXT NOT NULL UNIQUE,
                            can_admin       BOOLEAN NOT NULL DEFAULT FALSE,
                            default_harness TEXT NOT NULL DEFAULT '',
                            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                            created_by      TEXT NOT NULL DEFAULT '',
                            expires_at      TIMESTAMPTZ NOT NULL,
                            last_used_at    TIMESTAMPTZ,
                            revoked_at      TIMESTAMPTZ,
                            revoked_by      TEXT NOT NULL DEFAULT ''
                        )
                    """)
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS api_tokens_user_id_idx "
                        "ON api_tokens (user_id)"
                    )
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS token_audit (
                            id        BIGSERIAL PRIMARY KEY,
                            ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
                            event     TEXT NOT NULL,
                            token_id  TEXT NOT NULL DEFAULT '',
                            user_id   TEXT NOT NULL DEFAULT '',
                            actor     TEXT NOT NULL DEFAULT '',
                            ip        TEXT NOT NULL DEFAULT '',
                            detail    TEXT NOT NULL DEFAULT ''
                        )
                    """)
                    # One-shot import of legacy per-user tokens (hash copy, so
                    # the original plaintext keeps working through BOTH paths).
                    # can_admin TRUE keeps today's row-role semantics.
                    cur.execute("""
                        INSERT INTO api_tokens
                            (id, user_id, name, prefix, last4, token_hash,
                             can_admin, expires_at, created_by)
                        SELECT 'legacy-' || substr(md5(u.token_hash), 1, 8),
                               u.id, 'imported-legacy', 'cm_legacy', '----',
                               u.token_hash, TRUE, 'infinity'::timestamptz, 'import'
                        FROM users u
                        WHERE u.token_hash IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM api_tokens a
                                          WHERE a.token_hash = u.token_hash)
                    """)
            _schema_ok = True
        finally:
            conn.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def route_id(token: str) -> str:
    """Extract the routing id from cm_<id>_<secret>, or '' when the token is
    not in the new shape (legacy cm_<random> may contain underscores — the
    id alphabet is hex-only, so the first '_' split is unambiguous)."""
    if not token.startswith("cm_"):
        return ""
    rest = token[3:]
    tid, sep, secret = rest.partition("_")
    if sep and _ID_RE.match(tid) and secret:
        return tid
    return ""


def create_token(user_id: str, name: str, created_by: str, can_admin: bool = False,
                 expires_days: int = DEFAULT_EXPIRY_DAYS,
                 default_harness: str = "") -> tuple[dict, str]:
    """Mint a token. Returns (row, plaintext) — the plaintext is shown once
    and never stored. expires_days is clamped to 1..365."""
    ensure_table()
    name = (name or "").strip()[:64] or "token"
    days = max(1, min(int(expires_days or DEFAULT_EXPIRY_DAYS), MAX_EXPIRY_DAYS))
    tid = "t" + secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    plaintext = f"cm_{tid}_{secret}"
    row_plain = {"id": tid, "user_id": user_id, "name": name, "prefix": f"cm_{tid}",
                 "last4": secret[-4:], "can_admin": bool(can_admin),
                 "default_harness": (default_harness or "").strip().lower()[:32]}
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""INSERT INTO api_tokens (id, user_id, name, prefix, last4, token_hash,
                            can_admin, default_harness, created_by, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + (%s || ' days')::interval)
                        RETURNING {TOKEN_COLUMNS}""",
                    (tid, user_id, name, row_plain["prefix"], row_plain["last4"],
                     _hash(plaintext), bool(can_admin), row_plain["default_harness"],
                     created_by, str(days)),
                )
                row = dict(cur.fetchone())
                audit("create", token_id=tid, user_id=user_id, actor=created_by,
                      detail=f"name={name} days={days} can_admin={bool(can_admin)}")
        return row, plaintext
    finally:
        conn.close()


def get_by_token(token: str) -> Optional[dict]:
    """Resolve a cm_<id>_<secret> token to its row plus the owner's role and
    expiry info, or None. Disabled owners never resolve. Throttled
    last_used_at touch on success."""
    ensure_table()
    if not token:
        return None
    tid = route_id(token)
    if not tid:
        return None
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cols = ", a.".join(TOKEN_COLUMNS.split(", "))
            cur.execute(
                f"""SELECT a.{cols},
                           u.role AS user_role, u.disabled AS user_disabled
                    FROM api_tokens a JOIN users u ON u.id = a.user_id
                    WHERE a.id = %s""",
                (tid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            now = datetime.now(timezone.utc)
            if row["revoked_at"] is not None:
                return None
            if row["expires_at"] is not None and row["expires_at"] <= now:
                audit("expire_reject", token_id=tid, user_id=row["user_id"])
                logger.info("auth: expired API token %s (id=%s)", row["prefix"], tid)
                return None
            if row["user_disabled"]:
                return None
            if row["token_hash"] != _hash(token):
                return None
            # Throttled touch: at most one write per token per 5 minutes.
            cur.execute(
                """UPDATE api_tokens SET last_used_at = now()
                   WHERE id = %s AND (last_used_at IS NULL
                                      OR last_used_at < now() - interval '5 minutes')""",
                (tid,),
            )
            return row
    finally:
        conn.close()


def list_for_user(user_id: str) -> list[dict]:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {TOKEN_COLUMNS} FROM api_tokens WHERE user_id = %s "
                "ORDER BY created_at DESC",
                (user_id,),
            )
            return [_mask(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def list_all(user_filter: str = "") -> list[dict]:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if user_filter:
                cur.execute(
                    f"SELECT {TOKEN_COLUMNS} FROM api_tokens WHERE user_id = %s "
                    "ORDER BY created_at DESC",
                    (user_filter,),
                )
            else:
                cur.execute(f"SELECT {TOKEN_COLUMNS} FROM api_tokens ORDER BY created_at DESC")
            return [_mask(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def revoke(token_id: str, revoked_by: str, owner_user_id: str = "") -> bool:
    """Revoke by id. When owner_user_id is set (self-service), only that
    owner's token is revoked. Already-revoked rows report False."""
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                sql = ("UPDATE api_tokens SET revoked_at = now(), revoked_by = %s "
                       "WHERE id = %s AND revoked_at IS NULL")
                params: list = [revoked_by, token_id]
                if owner_user_id:
                    sql += " AND user_id = %s"
                    params.append(owner_user_id)
                sql += " RETURNING id, user_id"
                cur.execute(sql, params)
                row = cur.fetchone()
                if not row:
                    return False
                audit("revoke", token_id=row["id"], user_id=row["user_id"], actor=revoked_by)
                return True
    finally:
        conn.close()


def revoke_for_user(user_id: str, revoked_by: str) -> int:
    """Revoke every active token of a user (used when the user is disabled).
    Returns the number revoked."""
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE api_tokens SET revoked_at = now(), revoked_by = %s "
                    "WHERE user_id = %s AND revoked_at IS NULL",
                    (revoked_by, user_id),
                )
                n = cur.rowcount
                if n:
                    audit("revoke_all", user_id=user_id, actor=revoked_by, detail=f"count={n}")
                return n
    finally:
        conn.close()


def audit(event: str, token_id: str = "", user_id: str = "", actor: str = "",
          ip: str = "", detail: str = "") -> None:
    """Append-only audit trail. Never raises into the caller — a broken audit
    log must not break auth."""
    try:
        conn = _db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO token_audit (event, token_id, user_id, actor, ip, detail) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (event, token_id, user_id, actor, ip, detail[:500]),
                    )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("token audit write failed (%s): %s", event, e)


def _mask(row: dict) -> dict:
    """Display shape for every token listing: prefix + last4 only, plus
    convenience flags. The hash never leaves this module."""
    now = datetime.now(timezone.utc)
    exp = row.get("expires_at")
    days_left = None
    if exp is not None and row.get("revoked_at") is None:
        # psycopg2 maps 'infinity'::timestamptz to datetime.max (year 9999).
        if exp.year < 9999:
            days_left = max(0, int((exp - now).total_seconds() / 86400))
    return {
        "id": row["id"], "user_id": row["user_id"], "name": row["name"],
        "display": f"{row['prefix']}…{row['last4']}", "can_admin": row["can_admin"],
        "default_harness": row["default_harness"],
        "created_at": str(row["created_at"]), "created_by": row["created_by"],
        "expires_at": str(exp) if exp else None, "expires_in_days": days_left,
        "last_used_at": str(row["last_used_at"]) if row["last_used_at"] else None,
        "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
        "revoked_by": row["revoked_by"],
    }
