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
