# Keycloak client setup (OIDC login for the UI)

The browser login of claude-memory-ui uses a **public** OpenID Connect client
(Authorization Code + PKCE, S256). This page documents the exact shape the
server expects; operators are free to keep it under Terraform/OpenTofu
management (e.g. with the `mrparkers/keycloak` provider).

## Client

| Setting | Value |
|---|---|
| `clientId` | `claude-memory-ui` (must equal `OIDC_CLIENT_ID` / `KEYCLOAK_CLIENT_ID`) |
| Access type | Public (no client secret), PKCE `S256` |
| Standard flow | enabled; direct access grants **off** |
| Valid redirect URIs | `https://<ui-host>/*` |
| Web origins | `https://<ui-host>` |

## Audience mapper

Exactly one protocol mapper, `oidc-audience-mapper`, named
`client-id-audience`:

- config key `included.custom.audience` = `claude-memory-ui` (claims: id,
  access, introspection, userinfo)
- **never set `included.client.audience`** - on Keycloak 26.x it injects its
  raw string value into `aud` and suppresses the custom audience. The server
  verifies `aud` against `OIDC_CLIENT_ID`, so the client id must land in
  `aud` via the custom audience only.

## Admin role

A realm role (default name `claude-memory-admin`, matched by the server's
`OIDC_ADMIN_ROLES`) grants the UI admin view. Assign it to the users that
should administer all workspaces; users themselves are managed outside this
project.

## Server/UI environment

Server (claude-memory) - token verification and admin gating:

```
OIDC_ISSUER=https://<keycloak>/realms/<realm>
OIDC_CLIENT_ID=claude-memory-ui
OIDC_ADMIN_ROLES=claude-memory-admin
```

UI proxy (claude-memory-ui) - enables the browser login:

```
KEYCLOAK_URL=https://<keycloak>
KEYCLOAK_REALM=<realm>
KEYCLOAK_CLIENT_ID=claude-memory-ui
```
