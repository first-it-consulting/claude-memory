# Keycloak client runbook: claude-memory-ui

The claude-memory UI signs users in via Keycloak OIDC (realm `example-realm` on
`https://auth.example.internal`). The client and its role were created
manually in the admin console — there is no client IaC for this realm yet
(tracked and scrapped as out of scope; this runbook is the recovery path).
Server-side settings live in example-project
(`kubernetes/apps/selfhosted/claude-memory/app/helmrelease.yaml`):
`OIDC_ISSUER`, `OIDC_CLIENT_ID=claude-memory-ui`, `OIDC_ADMIN_ROLES=claude-memory-admin`.

## Client `claude-memory-ui` (realm example-realm)

| Setting | Value |
| --- | --- |
| Client authentication | **Off** (public SPA client — no secret) |
| Standard flow | On |
| PKCE | **S256** (Proof Key for Code Exchange Code Challenge Method) |
| Valid redirect URIs | `https://app.example.internal/*` |
| Valid post logout redirect URIs | `https://app.example.internal/*` |
| Web origins | `https://app.example.internal` |

The UI (keycloak-js) redirects to `window.location.origin + pathname`, so the
wildcard on the app origin is what matters.

## Realm role `claude-memory-admin`

Assign the realm role `claude-memory-admin` to users who should see all
workspaces. The server maps it via `OIDC_ADMIN_ROLES`; a JWT whose `realm_access.roles`
contains it makes `whoami` return `role=admin`.

## Audience mapper (the 26.3.3 trap)

The server validates the access token with `audience=<client_id>`, so the
access token's `aud` must contain `claude-memory-ui`. Add an
**oidc-audience-mapper** to the client with ONLY this config:

```json
{ "included.custom.audience": "claude-memory-ui", "access.token.claim": "true" }
```

**Trap:** the mapper's `included.client.audience` key injects its *literal
string value* into `aud` (it does not resolve client IDs the way you would
expect). Setting `included.client.audience=claude-memory-ui` puts the raw text
`"claude-memory-ui"` in as an unvalidated entry while dropping the proper
audience, and login then loops (token rejected server-side, UI bounces to
Keycloak again). Use `included.custom.audience` only. With the correct config
`aud` becomes `["claude-memory-ui", "account"]`.

## Verify `aud` without touching the real client

Public clients cannot be token-granted directly. Create a throwaway
**confidential twin** client (same mappers, copy the generated secret), then:

1. Password-grant a token: `POST /realms/example-realm/protocol/openid-connect/token`
   with `client_id`/`client_secret` of the twin, `grant_type=password`, a test user.
2. Decode the `access_token` payload (JWT) and check `aud` contains `claude-memory-ui`.
3. Delete the twin client.

Note: admin-cli password-grant tokens expire after ~1 minute — script the whole
flow in one go. Never paste tokens into chat, logs, or beans.

## Server-side reference

`src/auth.py::verify_jwt` decodes with JWKS from `OIDC_ISSUER`, requiring
`iss`, `exp`, and `audience = OIDC_AUDIENCE or OIDC_CLIENT_ID`.
