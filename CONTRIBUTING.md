# Contributing to claude-memory

Thanks for helping! Small, focused pull requests are easiest to review.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                      # unit + API tests
```

The tests need no live services (Postgres/Redis/Keycloak are stubbed). For manual testing,
`docker buildx bake -f docker-bake.hcl image-local` builds a local image; see the
configuration table in the README for the environment variables.

## Ground rules

- Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` …);
  the PR title follows the same style.
- Every PR must keep CI green (tests run on every pull request).
- Never commit secrets, tokens or private hostnames — this is a public repository.
- Schema changes are additive only; migrations must remain rollback-safe.
- The web UI lives in [claude-memory-ui](https://github.com/first-it-consulting/claude-memory-ui).

## Reporting issues

Bugs and feature requests: use the issue templates. Security issues: see [SECURITY.md](SECURITY.md).
