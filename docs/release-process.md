# Release process

Releases are driven by release-please (`.github/workflows/release.yml`):
conventional commits on `main` accumulate in a rolling release PR; merging it
creates the tag, the GitHub release and (via dispatch) the container publish.

Version policy while the server is pre-1.0 (`release-please-config.json`):
`bump-minor-pre-major` + `bump-patch-for-minor-pre-major` are enabled, so
feature batches in 0.x land as PATCH bumps by default. When a batch should be
a MINOR anyway (e.g. 0.1.x -> 0.2.0 for a feature epic), pin it with a
one-off commit carrying the `Release-As: X.Y.Z` footer - release-please reads
the footer and uses that version for the next release.

Note: the commit must contain an actual file change - GitHub's rebase-merge
drops empty commits, and the footer would never reach `main`. (This file is
the payload of the 0.2.0 pin.)

## Merging the release PR

The `protect-main` ruleset requires the `ci-success` status check, but
release-please's version-only PR (CHANGELOG + version files) triggers no CI -
so a plain merge, even with `--admin`, is blocked (`mergeStateStatus:
BLOCKED`). Merge it as an administrator with a method that does not wait for
checks:

```sh
gh pr merge <release-pr> --admin --squash
```

(The 0.2.1 release, 2026-11-22, behaved exactly like this: `--admin` alone
failed, `--admin --squash` merged; the tag and GitHub release appeared a
minute later and the publish workflow dispatched itself.)

## The release PR itself has no PR gate

Release-please PRs run no PR gate or show a failed run without jobs: every
push to the rolling release branch makes the PR-triggered workflows
(test/security/publish) fail on GitHub's side - "This run likely failed
because of a workflow file issue", zero jobs, no logs, not rerunnable - or
sit in `action_required` waiting for an approval that never resolves. The
workflows themselves are fine (identical, valid YAML on `main`; every
human-authored PR head gates green). PR events read workflow files from the
PR head, and release-please's own branch commits (created under its app
token) trip the approval/config rail that only applies when the workflow is
not coming from `main` (bean clpj, 2026-11-22; both repos affected).

Consequences, and why we keep it: the merge already ignores these checks
(`--admin --squash`, "no checks reported"), and **the tag publish
(workflow_dispatch on `v*`) is the real gate** - it builds on the release
commit and was green for every release so far. A red X on the release PR is
expected noise; do not chase it, and do not treat it as a reason to skip the
tag-publish verification (digest compare against ghcr before mirroring).
