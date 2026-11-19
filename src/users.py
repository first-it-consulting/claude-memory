"""Users, tenants and API tokens (multi-tenancy).

A tenant is a workspace: its id doubles as the mem0 `user_id` namespace, so
tenant isolation is mem0's own user_id filter. This module owns the `users`
table — the tenant registry plus how each caller authenticates:

  * agents/MCP: a static per-user API token (`cm_…`, shown once, stored as a
    SHA-256 hash — the tokens are high-entropy random strings, so a plain
    hash is the right primitive here, not a password KDF)
  * browser: a Keycloak access token; the first login auto-provisions a row
    keyed by `preferred_username`, bound to the Keycloak `sub`

Schema changes are additive only (CREATE/ALTER ... IF NOT EXISTS) so an old
server image keeps working against a migrated database (rollback path).
"""
import hashlib
import logging
import re
import secrets
import threading
from typing import Optional

import psycopg2
import psycopg2.extras

from config import settings

logger = logging.getLogger(__name__)

USER_COLUMNS = "id, name, role, oidc_sub, email, disabled, created_at, last_login_at"
VALID_ROLES = ("user", "admin")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_schema_ok = False
_schema_lock = threading.Lock()


def _db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(settings.database_url)


def ensure_table() -> None:
    """Create the users table lazily — the DB may not have been reachable at
    process start. Idempotent and additive-only."""
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
                        CREATE TABLE IF NOT EXISTS users (
                            id            TEXT PRIMARY KEY,
                            name          TEXT NOT NULL DEFAULT '',
                            role          TEXT NOT NULL DEFAULT 'user',
                            token_hash    TEXT UNIQUE,
                            oidc_sub      TEXT UNIQUE,
                            email         TEXT NOT NULL DEFAULT '',
                            disabled      BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                            last_login_at TIMESTAMPTZ
                        )
                    """)
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS users_token_hash_idx ON users (token_hash)"
                    )
            _schema_ok = True
        finally:
            conn.close()


def sanitize_id(raw: str) -> str:
    """Normalise a desired tenant id: lowercase, url-safe, max 64 chars."""
    out = re.sub(r"[^a-z0-9._-]+", "-", (raw or "").strip().lower()).strip("-._")
    return out[:64]


def valid_id(user_id: str) -> bool:
    return bool(_ID_RE.match(user_id or ""))


def generate_token() -> str:
    return "cm_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_to_user(row) -> dict:
    u = dict(row)
    u.pop("token_hash", None)  # never leave the module
    return u


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_user(user_id: str, name: str = "", role: str = "user",
                oidc_sub: Optional[str] = None, email: str = "") -> tuple[dict, str]:
    """Create a tenant. Returns (user, api_token) — the token is shown once
    and stored only as a hash. Raises ValueError on a bad/duplicate id."""
    user_id = sanitize_id(user_id)
    if not valid_id(user_id):
        raise ValueError(f"invalid user id {user_id!r} (use lowercase letters, digits, . _ -)")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    if user_id == settings.shared_tenant_id:
        raise ValueError(f"{settings.shared_tenant_id!r} is reserved for the shared space")
    token = generate_token()
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"INSERT INTO users (id, name, role, token_hash, oidc_sub, email, last_login_at)"
                    f" VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s IS NULL THEN NULL ELSE now() END)"
                    f" RETURNING {USER_COLUMNS}",
                    (user_id, name.strip(), role, hash_token(token), oidc_sub, email.strip(), oidc_sub),
                )
                return _row_to_user(cur.fetchone()), token
    except psycopg2.errors.UniqueViolation as e:
        constraint = getattr(getattr(e, "diag", None), "constraint_name", "") or ""
        if "oidc_sub" in constraint:
            raise ValueError("this Keycloak account is already linked to another user")
        raise ValueError(f"user {user_id!r} already exists")
    finally:
        conn.close()


def get_user(user_id: str) -> Optional[dict]:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return _row_to_user(row) if row else None
    finally:
        conn.close()


def list_users() -> list[dict]:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY created_at")
            return [_row_to_user(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_tenant_ids() -> list[str]:
    """All tenants that can own data: registered users + default + shared."""
    ids = [settings.mem0_user_id, settings.shared_tenant_id]
    try:
        ids += [u["id"] for u in list_users()]
    except Exception as e:  # DB down: the static namespaces still work
        logger.warning("list_tenant_ids: users table unavailable: %s", e)
    return sorted(set(ids))


def get_by_token(token: str) -> Optional[dict]:
    """Look up the user an API token belongs to (token is high-entropy; the
    lookup is an exact hash match in the index)."""
    if not token:
        return None
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE token_hash = %s AND NOT disabled",
                (hash_token(token),),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))
            return _row_to_user(row)
    finally:
        conn.close()


def get_by_oidc_sub(sub: str) -> Optional[dict]:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {USER_COLUMNS} FROM users WHERE oidc_sub = %s", (sub,))
            row = cur.fetchone()
            return _row_to_user(row) if row else None
    finally:
        conn.close()


def touch_login(user_id: str) -> None:
    try:
        conn = _db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
        finally:
            conn.close()
    except Exception as e:
        logger.warning("touch_login(%s) failed: %s", user_id, e)


def upsert_oidc_user(sub: str, username: str, email: str, is_admin: bool) -> dict:
    """Resolve a verified Keycloak token to a user row. Existing rows are
    matched by `sub` (identity is the Keycloak subject, never the name); the
    role is re-asserted from the token's realm roles on every login."""
    by_sub = get_by_oidc_sub(sub)
    if by_sub:
        if by_sub["disabled"]:
            raise ValueError("account disabled")
        role = "admin" if is_admin else "user"
        conn = _db_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"UPDATE users SET role = %s, email = %s, last_login_at = now() "
                        f"WHERE id = %s RETURNING {USER_COLUMNS}",
                        (role, email, by_sub["id"]),
                    )
                    return _row_to_user(cur.fetchone())
        finally:
            conn.close()
    if not settings.oidc_auto_create:
        raise ValueError("no local account is linked to this Keycloak user")
    user_id = sanitize_id(username or sub)
    if not valid_id(user_id):
        user_id = "oidc-" + hashlib.sha256(sub.encode()).hexdigest()[:12]
    try:
        user, _token = create_user(
            user_id, name=username or email or user_id,
            role="admin" if is_admin else "user", oidc_sub=sub, email=email,
        )
        return user
    except ValueError as e:
        # Username already taken locally (e.g. the tenant was created with an
        # API token first): link this Keycloak subject to it instead.
        existing = get_user(user_id)
        if existing and existing.get("oidc_sub") is None:
            conn = _db_conn()
            try:
                with conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(
                            f"UPDATE users SET oidc_sub = %s, email = %s, last_login_at = now(), "
                            f"role = %s WHERE id = %s AND oidc_sub IS NULL RETURNING {USER_COLUMNS}",
                            (sub, email, "admin" if is_admin else "user", user_id),
                        )
                        row = cur.fetchone()
                        if not row:
                            raise ValueError("account already linked to another identity")
                        return _row_to_user(row)
            finally:
                conn.close()
        raise


def update_user(user_id: str, name: Optional[str] = None, role: Optional[str] = None,
                disabled: Optional[bool] = None) -> Optional[dict]:
    sets, params = [], []
    if name is not None:
        sets.append("name = %s"); params.append(name.strip())
    if role is not None:
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        if role != "admin" and enabled_admin_count() <= 1 and (get_user(user_id) or {}).get("role") == "admin":
            raise ValueError("cannot demote the last enabled admin")
        sets.append("role = %s"); params.append(role)
    if disabled is not None:
        if disabled and (get_user(user_id) or {}).get("role") == "admin" and enabled_admin_count() <= 1:
            raise ValueError("cannot disable the last enabled admin")
        sets.append("disabled = %s"); params.append(bool(disabled))
    if not sets:
        return get_user(user_id)
    params.append(user_id)
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE id = %s RETURNING {USER_COLUMNS}",
                    params,
                )
                row = cur.fetchone()
                return _row_to_user(row) if row else None
    finally:
        conn.close()


def rotate_token(user_id: str) -> Optional[str]:
    """Replace the API token. Returns the new plaintext token (shown once)."""
    token = generate_token()
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET token_hash = %s WHERE id = %s",
                    (hash_token(token), user_id),
                )
                if cur.rowcount == 0:
                    return None
                return token
    finally:
        conn.close()


def delete_user(user_id: str) -> bool:
    """Remove the account (its memories stay in its mem0 namespace; deleting
    the data itself is a separate, explicit operation)."""
    if (get_user(user_id) or {}).get("role") == "admin" and enabled_admin_count() <= 1:
        raise ValueError("cannot delete the last enabled admin")
    ensure_table()
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                return cur.rowcount > 0
    finally:
        conn.close()


def enabled_admin_count() -> int:
    ensure_table()
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND NOT disabled")
            return int(cur.fetchone()[0])
    finally:
        conn.close()
