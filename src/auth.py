"""Request authentication and tenant resolution.

resolve(token) tries, in order:

  1. ADMIN_TOKEN            — bootstrap admin (role admin, tenant "default")
  2. a per-user API token   — users table, SHA-256 hash lookup (agents/MCP)
  3. a Keycloak access JWT  — verified via JWKS (browser login, UI)
  4. the legacy BEARER_TOKEN— tenant "default", role user (migration path)

The winning token type decides the caller's tenant and role; everything
downstream (memory.py, session_state, hooks, queue jobs) is scoped to that
principal. MCP tools read the principal from a contextvar that the ASGI app
sets before dispatching, so tools never touch headers themselves.
"""

import hmac
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from config import settings

if TYPE_CHECKING:  # runtime imports stay lazy inside the OIDC helpers
    import jwt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    user_id: str  # tenant workspace (mem0 user_id)
    role: str  # "user" | "admin"
    via: str  # admin | token | oidc | legacy
    label: str = ""  # display name (audit log)
    # PAT context (api_tokens path only; defaults for every other caller)
    token_id: str = ""
    token_name: str = ""
    default_harness: str = ""
    token_expires_in_days: int = -1  # -1 = not a PAT or never expires

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def default_principal() -> Principal:
    """Who acts when no request context exists (scripts, tests, direct calls)."""
    return Principal(settings.mem0_user_id, "admin", "local", "local")


_principal_var: ContextVar[Principal | None] = ContextVar("principal", default=None)


def set_principal(p: Principal) -> None:
    _principal_var.set(p)


def get_principal() -> Principal:
    return _principal_var.get() or default_principal()


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def resolve(token: str) -> Principal | None:
    """Map a bearer token to a Principal, or None when unauthenticated."""
    token = (token or "").strip()
    if not token:
        return None

    if _eq(token, settings.admin_token):
        return Principal(settings.mem0_user_id, "admin", "admin", "admin-token")

    # API tokens are prefixed so a wrong one fails fast without a DB hit.
    if token.startswith("cm_"):
        p = _resolve_api_token(token)
        if p:
            return p
        logger.info("auth: unknown or disabled API token %s…", token[:10])
        return None

    # Keycloak access tokens are JWTs (three dot-separated base64url parts).
    if settings.oidc_issuer and token.count(".") == 2:
        p = _resolve_oidc(token)
        if p:
            return p
        return None

    if _eq(token, settings.bearer_token):
        # Revocation truth: the shared token is registered as the
        # 'legacy-hooks' api_tokens row (tokens.ensure_table imports it), so
        # revoking that row kills every hook fleet-wide. This makes the
        # config path DB-dependent — deliberate: a revoked shared token must
        # stay dead, and during a DB outage the hooks could not read or write
        # anyway. Fail closed on lookup errors.
        import tokens

        try:
            if tokens.is_hash_revoked(tokens._hash(token)):
                logger.info("auth: shared legacy token revoked (legacy-hooks)")
                return None
        except Exception as e:
            logger.warning("auth: shared-token revocation check failed (DB?): %s", e)
            return None
        # Keep 'last used' honest for the migration runbook (revoking is only
        # safe once nobody used it recently). Throttled, own txn, never fails.
        tokens.touch_last_used("legacy-hooks")
        return Principal(settings.mem0_user_id, "user", "legacy", "legacy-token")
    return None


def _resolve_api_token(token: str) -> Principal | None:
    # New-shape PATs (cm_<id>_<secret>) resolve through api_tokens; the
    # legacy per-user shape (cm_<random>, users.token_hash) keeps its exact
    # old path below — row-role semantics, unchanged.
    import tokens

    try:
        t = tokens.get_by_token(token)
    except Exception as e:
        logger.warning("API token lookup failed (DB?): %s", e)
        return None
    if t:
        role = "admin" if (t["user_role"] == "admin" and t["can_admin"]) else "user"
        exp = t.get("expires_at")
        days = -1
        if exp is not None and t.get("revoked_at") is None and exp.year < 9999:
            from datetime import datetime

            days = max(0, int((exp - datetime.now(UTC)).total_seconds() // 86400))
        return Principal(
            t["user_id"],
            role,
            "token",
            t["name"],
            token_id=t["id"],
            token_name=t["name"],
            default_harness=t.get("default_harness", ""),
            token_expires_in_days=days,
        )
    import users

    try:
        u = users.get_by_token(token)
    except Exception as e:
        logger.warning("API token lookup failed (DB?): %s", e)
        return None
    if not u:
        return None
    # Revocation single source of truth: if this legacy hash was imported
    # into api_tokens and revoked there, the plaintext is dead too.
    try:
        if tokens.is_hash_revoked(tokens._hash(token)):
            logger.info("auth: legacy token revoked for user %s", u["id"])
            return None
    except Exception as e:
        logger.warning("api_tokens revocation check failed (DB?): %s", e)
        return None
    return Principal(u["id"], u["role"], "token", u.get("name") or u["id"])


def _resolve_oidc(token: str) -> Principal | None:
    try:
        claims = verify_jwt(token)
    except Exception as e:
        logger.info("auth: OIDC token rejected: %s", e)
        return None
    import users

    username = claims.get("preferred_username") or claims.get("email") or ""
    email = claims.get("email") or ""
    try:
        u = users.upsert_oidc_user(
            sub=claims["sub"],
            username=username,
            email=email,
            is_admin=_has_admin_role(claims),
        )
    except ValueError as e:
        logger.info(
            "auth: OIDC user rejected (%s): %s", username or claims.get("sub"), e
        )
        return None
    except Exception as e:
        logger.warning("auth: OIDC user lookup failed (DB?): %s", e)
        return None
    return Principal(u["id"], u["role"], "oidc", username or u["id"])


def _has_admin_role(claims: dict) -> bool:
    admin_roles = {r.strip() for r in settings.oidc_admin_roles.split(",") if r.strip()}
    realm_roles = set((claims.get("realm_access") or {}).get("roles") or [])
    client_roles = set(
        ((claims.get("resource_access") or {}).get(settings.oidc_client_id) or {}).get(
            "roles"
        )
        or []
    )
    if (realm_roles | client_roles) & admin_roles:
        return True
    admin_users = {
        u.strip().lower() for u in settings.oidc_admin_usernames.split(",") if u.strip()
    }
    username = (claims.get("preferred_username") or "").lower()
    return bool(username) and username in admin_users


# ---------------------------------------------------------------------------
# Keycloak JWT verification (RS256 via JWKS; PyJWT is imported lazily so the
# server still starts without it when OIDC is disabled)
# ---------------------------------------------------------------------------

_discovery: dict = {"jwks_uri": "", "time": 0.0}
_jwks_client = None


def _jwks() -> "jwt.PyJWKClient":
    """PyJWKClient for the configured issuer, cached. Discovery is fetched
    once per process (Keycloak's jwks_uri is stable across restarts)."""
    global _jwks_client
    import time as _time

    import jwt

    now = _time.monotonic()
    if not _discovery["jwks_uri"] or now - _discovery["time"] > 3600:
        import requests

        issuer = settings.oidc_issuer.rstrip("/")
        resp = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
        resp.raise_for_status()
        _discovery["jwks_uri"] = resp.json()["jwks_uri"]
        _discovery["time"] = now
        _jwks_client = None
    if _jwks_client is None:
        # cache_jwk_set=True + default 5-minute refresh handles key rotation.
        _jwks_client = jwt.PyJWKClient(
            _discovery["jwks_uri"], cache_jwk_set=True, lifespan=300
        )
    return _jwks_client


def verify_jwt(token: str) -> dict:
    """Verify signature, issuer, audience and lifetime; return the claims.
    Raises on any failure — callers must treat that as unauthenticated."""
    import jwt

    header = jwt.get_unverified_header(token)
    if header.get("alg") not in ("RS256", "ES256"):
        raise ValueError(f"unsupported JWT algorithm {header.get('alg')!r}")
    key = _jwks().get_signing_key_from_jwt(token).key
    audience = settings.oidc_audience or settings.oidc_client_id
    claims = jwt.decode(
        token,
        key,
        algorithms=[header["alg"]],
        audience=audience,
        issuer=settings.oidc_issuer.rstrip("/"),
        options={"require": ["exp", "sub"]},
    )
    return claims


def extract_bearer(header_value: str) -> str:
    v = (header_value or "").strip()
    return v[7:].strip() if v[:7].lower() == "bearer " else v


def principal_from_asgi_headers(headers: dict) -> Principal | None:
    """headers: the raw ASGI header list as dict (bytes → bytes)."""
    raw = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
    return resolve(extract_bearer(raw))
