# Design note: a public Helm chart

Design only — **no chart exists yet and none is implied by this document.**
It captures scope, packaging, the values surface, what must stay external,
and the trigger criteria, so that whoever eventually builds it starts from a
decision instead of a blank page. The private deployment this project runs
in itself uses plain manifests through Flux (see `docs/kubernetes.md`); the
chart would wrap exactly that shape.

## Verdict up front

Not now. A chart would be ~90% templating of the five manifests in
`docs/kubernetes.md`, and those manifests are already the tested deployment
path. The maintenance cost is real (values drift, version skew against the
server, support surface for other people's clusters) and the payoff - "one
`helm install`" - only matters once someone outside the current two
deployments asks for it. The trigger criteria at the bottom decide, not
enthusiasm.

## Scope of a future chart

One chart, `claude-memory`, shipping everything the app needs and nothing
it does not:

- **Server** Deployment: probes (`/health`), the UID 1000 + read-only-rootfs
  security context, the two `emptyDir` volumes (`/tmp`, `/home/app`),
  `envFrom` a single Secret.
- **Service** for the server (ClusterIP, port 8080).
- **Ingress** (optional block): the two supported shapes from
  `docs/kubernetes.md` — plain (app bearer auth is the gate) or with a login
  proxy in front of the UI while `/mcp`, `/api/hooks` and `/api/sessions`
  route straight to the service. The chart must not invent a third shape;
  `scripts/setup.sh doctor` verifies exactly this split.
- **UI** Deployment + Service (optional block, `ui.enabled`): the UI is a
  separate image with its own release stream; env wiring
  (`MCP_INTERNAL_URL`, `BEARER_TOKEN` or the Keycloak OIDC variables per the
  UI README) comes from the same Secret machinery.
- **dedup CronJob** (optional block, `dedup.enabled`): same image, same
  embedder settings, `command` from the container entrypoint, schedule and
  threshold as values, `concurrencyPolicy: Forbid`.

Out of scope, deliberately: **no Postgres.** Bundling a database (subchart
or bitnami dependency) would put a stateful, backed-up, human-owned thing
into a disposable template — the wrong coupling (see "What stays external").

## Packaging and publishing

- **OCI on ghcr.io**, chart version == app version convention: the chart
  `0.x.y` ships server `vA.B.C` + UI `vD.E.F` as its default image tags.
  Package with `helm package` + `helm push` to
  `oci://ghcr.io/<org>/charts` in the existing publish workflow (tag-driven,
  same gate pattern as the images).
- **release-please does not manage a chart it does not own.** The honest
  options are: a `release-please` manifest entry with a bespoke bump
  strategy once the chart is a real directory in this repo, or (simpler at
  first) the chart version tracks the server release it bundles and is
  bumped in the same release PR that bumps `version.txt` — one release
  train, no second version ledger.
- **Semver discipline for the chart itself:** pre-1.0 like everything else
  here; breaking values changes are a MINOR with a migration note, never a
  silent rename.

## Values surface (the minimum honest one)

Sketch, not final — the rule is *every value maps to something the
sanitized manifests already show*, no chart-only concepts:

```yaml
image:
  repository: ghcr.io/first-it-consulting/claude-memory
  # default set to the bundled release; digest pinning wins if set
  tag: v0.2.5
  digest: ""            # sha256:… — see "Digest pinning"
server:
  replicas: 1
  resources: {}
  extraEnv: []
secret:
  # the chart references an existing Secret; it never renders secret values
  existingSecret: claude-memory-env
ingress:
  enabled: true
  className: ""
  host: memory.example
  annotations: {}
  tls: []
  # bearer paths straight to the service, everything else behind a proxy:
  proxyPrefixes: ["/", "/api/auth/whoami", "/health"]
  directPaths: ["/mcp", "/api/hooks", "/api/sessions"]
ui:
  enabled: false
  image:
    repository: ghcr.io/first-it-consulting/claude-memory-ui
    tag: v0.1.5
    digest: ""
  auth: bearer            # bearer | oidc  (oidc: UI README variables)
dedup:
  enabled: true
  schedule: "30 3 * * *"
  threshold: 0.92
```

Hard rules for the surface:

- **Secrets are referenced, never rendered.** The chart takes
  `secret.existingSecret`; `DATABASE_URL`, `ADMIN_TOKEN`, embedder settings
  live in a Secret the operator manages (sealed-secrets, SOPS, external
  secrets — the chart's business is the reference, not the custody). This
  keeps `helm get values` and `helm template` output free of credentials.
- The example values in the chart README use `memory.example` and
  `CHANGE_ME` — same sanitization discipline as `docs/kubernetes.md`.

## What stays external (and why)

- **PostgreSQL with pgvector + pg_trgm.** State, backups, and extension
  bootstrapping belong to the operator's database story (a CloudNativePG
  `Cluster` + `Database` CR like the one in `docs/kubernetes.md`, RDS,
  whatever). The chart only needs `DATABASE_URL` in the Secret. The server
  creates the `vector` extension and its schema at startup; `pg_trgm` must
  exist or be creatable — a documented precondition, not a chart hook.
- **Embeddings endpoint** (`OLLAMA_BASE_URL` / model names, or any
  OpenAI-compatible `/v1/embeddings`): a cluster may already run one
  (shared GPU node, litellm, hosted API). Templating an Ollama deployment
  would assume the answer to a question the operator already answered.
- **OIDC / oauth2-proxy.** The login layer is site policy (realm, client,
  cookie domain). The chart offers the ingress split and the UI's OIDC env
  wiring; it does not deploy a proxy or a Keycloak client.

## Migration and init handling

- Schema, `vector` extension, seed rows: all done by the **server at
  startup** (idempotent). The chart needs no init Jobs, no migrations, no
  hooks — and must not add any, or it forks logic that already has one home.
- Upgrading the chart is upgrading images plus, occasionally, new optional
  values. There is no state to migrate inside the chart's objects.
- `helm install` twice must be a no-op; `helm diff` (or the Flux
  HelmRelease diff) must stay readable — that is the practical test for
  "the templating is honest".

## Image digest pinning

Mutable tags (`:rolling`, and even `:vX.Y.Z` re-pointable by a rebuild)
defeat rollout bookkeeping — `docs/kubernetes.md` already says pin an
immutable tag. The chart should support the stronger form the same way
Flux users do: `image.digest` (or Flux's `imgpkg`/`policy` machinery) wins
over `image.tag` when set, and the chart README shows the
`digest: sha256:…` form for people who mirror images. Defaults stay
human-readable tags; pinning is opt-in per deployment, not chart law.

## Upgrade path from the Flux example

`docs/kubernetes.md` ends with a Flux `Kustomization` over plain manifests.
A HelmRelease replacing it is mechanical:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: claude-memory
spec:
  interval: 30m
  chart:
    spec:
      type: oci
      chart: oci://ghcr.io/<org>/charts/claude-memory
      version: 0.x.y
  values:
    image: { digest: "sha256:…" }   # or tag
```

Same Secret, same Ingress host split, same CronJob schedule — the values
translate one-to-one from the manifests, which is the point of keeping the
values surface boring. The doc must show this migration next to the
Kustomization it replaces.

## Maintenance cost, honestly

- **Two image streams, one chart.** Server and UI release independently
  (release-please, separate tags); every chart release that bundles a new
  app version is a bump PR that can be forgotten. Mitigation: chart bumps
  ride the existing release PRs, Renovate can watch the chart's
  `image.tag`s, and the "defaults track the bundled release" rule makes
  staleness visible.
- **Values are an API.** Every option added is forever supported (pre-1.0
  softens this, but people vendor values files). Keep the surface minimal
  and boring; refuse cleverness (no conditional feature-detection, no
  auto-creating databases, no embedded proxy).
- **Support surface.** A public chart invites issues about *other people's*
  clusters: their ingress controller, their Postgres operator, their proxy.
  Each chart release multiplies that queue.
- **CI.** Chart lint (`helm lint`, `ct lint`), template-render tests, and a
  smoke install would have to join the existing matrix or the chart rots.

## Trigger criteria — build it when (not "if")

Build the chart when **at least two** of these are true:

1. Someone outside the current maintainers runs this in production and
   asks for Helm specifically (issue/discussion, not speculation).
2. The manifest set in `docs/kubernetes.md` has grown enough variants that
   copy-paste-adapt is demonstrably error-prone (more than ~2 optional
   blocks beyond UI/Ingress/dedup).
3. A second, meaningfully different deployment shape needs to be supported
   with one knob (e.g. a hosted multi-tenant mode) — templating pays for
   itself when the same template serves two real customers.
4. The maintenance is owned: the person who builds it commits to the bump
   discipline and the chart CI above.

Until then the answer to "is there a Helm chart?" is: no, and
`docs/kubernetes.md` is the tested path — this note is the pre-work, done.
