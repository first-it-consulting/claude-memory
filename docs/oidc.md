# Using another OIDC provider (server)

The server verifies browser JWTs against **any** OIDC provider that offers
standard discovery — the code path is `src/auth.py`. It has been **tested
against Keycloak**; other providers are untested but only rely on OIDC
standards. (The bundled web UI still requires Keycloak — it signs in via
`keycloak-js`; generic OIDC login in the UI is a planned idea, not built.)

## Requirements on the provider

- Standard discovery: `GET <issuer>/.well-known/openid-configuration` must
  publish a `jwks_uri`; the server fetches it once per hour and caches the
  keys.
- **RS256 or ES256** signed **access tokens** (other algorithms are
  rejected). The token must carry `exp` and `sub`; `iss`, `aud` and the
  signature are verified on every request.

## Settings

| Variable | Meaning |
|---|---|
| `OIDC_ISSUER` | Issuer URL, e.g. `https://issuer.example/realms/team`. Enables JWT auth when set; compared exactly (trailing `/` stripped) against the token's `iss`. |
| `OIDC_CLIENT_ID` | Expected audience, default `claude-memory-ui`. |
| `OIDC_AUDIENCE` | Set only if the token's `aud` differs from the client id; otherwise `OIDC_CLIENT_ID` is used. |
| `OIDC_ADMIN_ROLES` | Comma-separated role names checked against Keycloak-style `realm_access.roles` and `resource_access.<client_id>.roles` claims. |
| `OIDC_ADMIN_USERNAMES` | Comma-separated `preferred_username` values that grant admin regardless of claims. |
| `OIDC_AUTO_CREATE` | `true` (default): first login auto-creates a `role=user` account keyed by the token's `sub`; the display name comes from `preferred_username` (fallback `email`). |

## Admin mapping, honestly

Role matching reads Keycloak-style claim shapes. A provider that does not
publish `realm_access`/`resource_access` (most non-Keycloak ones) will never
match `OIDC_ADMIN_ROLES` — grant admins via `OIDC_ADMIN_USERNAMES` instead.

## Minimal .env for a non-Keycloak provider

```bash
OIDC_ISSUER=https://issuer.example/realms/team
OIDC_CLIENT_ID=claude-memory-ui          # must appear in the token's aud
OIDC_ADMIN_USERNAMES=alice,admin@example.com
# OIDC_AUDIENCE=            # only if aud != client id
```

Agents keep using `cm_…` API tokens; OIDC only authenticates browser JWTs.
