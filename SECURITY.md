# Security Policy

## Supported Versions

This project is pre-1.0. Only the **latest release** (and `main`) receive security fixes.

## Reporting a Vulnerability

Please report security issues **privately** via GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability):
open the **Security** tab of this repository and choose **"Report a vulnerability"**.
If that option is not yet visible on this repository, use the contact method shown on the
maintainer's GitHub profile and mark the message as a security report — please do **not**
open a regular public issue for vulnerabilities.

Include, if you can:

- version or commit you tested against
- affected component (REST/MCP API, auth & tokens, OIDC/browser login, tenant isolation, UI proxy)
- step-by-step reproduction and the security impact

We aim to acknowledge reports within **7 days** and will keep you informed while the issue
is handled. Reporters may be credited in the advisory after the fix ships.

## Scope notes

- API tokens (`cm_*`), `ADMIN_TOKEN` and the legacy shared bearer are stored or compared as
  SHA-256 hashes; a valid report proving hash recovery or tenant isolation bypass is high severity.
- Browser login verifies Keycloak JWTs (issuer/audience/expiry); bypasses of that check are in scope.
- Attacks that require already owning the trusted database, Redis or the host are out of scope.
