# Deploying on Kubernetes

The production shape this project runs in itself: one server Deployment +
Postgres (pgvector), an optional UI Deployment, a nightly dedup CronJob, all
delivered through Flux. The manifests below are sanitized versions of that
setup — generic names, `memory.example` hostnames, no secrets — and work on
any cluster with an Ingress controller. Adjust storage classes and your
Postgres operator to taste.

## Requirements

- **PostgreSQL with `pgvector` and `pg_trgm`.** The schema and the `vector`
  extension are created by the server at startup; `pg_trgm` needs to exist or
  be creatable. With CloudNativePG a `Database` CR manages it without a
  superuser secret (the operator's internal role creates the extension):

  ```yaml
  apiVersion: postgresql.cnpg.io/v1
  kind: Database
  metadata:
    name: memory-db
  spec:
    cluster: { name: memory-pg }
    name: mem0
    owner: mem0
    databaseReclaimPolicy: retain
    extensions:
      - name: pg_trgm
        ensure: true
  ```

  On plain Postgres run `CREATE EXTENSION IF NOT EXISTS pg_trgm;` once, or
  grant the app role enough rights (it also runs `CREATE EXTENSION vector`).
- **An embeddings endpoint** (Ollama or any OpenAI-compatible
  `/v1/embeddings`): set `OLLAMA_BASE_URL` / `OLLAMA_EMBED_MODEL` (and
  `OLLAMA_MODEL` for the optional chat extraction).
- **If you put a login proxy (oauth2-proxy) in front of the UI:** keep
  `/mcp`, `/api/hooks` and `/api/sessions` reachable **with bearer tokens** —
  agents authenticate with per-user tokens, not SSO cookies. Everything else
  (`/`, `/api/auth/whoami`, `/health`, …) may stay login-gated; `GET
  /api/version` intentionally requires a bearer too.

## Secrets

Two secrets, nothing checked into Git:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: claude-memory-env
stringData:
  DATABASE_URL: postgresql://mem0:CHANGE_ME@memory-pg-rw:5432/mem0
  ADMIN_TOKEN: CHANGE_ME_LONG_RANDOM   # bootstrap only; mint personal tokens via /api/users
  OLLAMA_BASE_URL: http://ollama.your-namespace.svc:11434
  OLLAMA_MODEL: qwen2.5:7b
  OLLAMA_EMBED_MODEL: nomic-embed-text
```

## Server

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claude-memory
spec:
  replicas: 1
  selector: { matchLabels: { app: claude-memory } }
  template:
    metadata: { labels: { app: claude-memory } }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: server
          image: ghcr.io/first-it-consulting/claude-memory:v0.2.1
          ports: [{ containerPort: 8080 }]
          envFrom: [{ secretRef: { name: claude-memory-env } }]
          startupProbe:
            httpGet: { path: /health, port: 8080 }
            failureThreshold: 30
            periodSeconds: 5
          livenessProbe: { httpGet: { path: /health, port: 8080 } }
          readinessProbe: { httpGet: { path: /health, port: 8080 } }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: tmp, mountPath: /tmp }
            - { name: home, mountPath: /home/app }   # mem0 writes ~/.mem0
      volumes:
        - name: tmp
          emptyDir: {}
        - name: home
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: claude-memory
spec:
  selector: { app: claude-memory }
  ports: [{ port: 8080, targetPort: 8080 }]
```

The image runs as UID 1000 with a read-only root filesystem; the two
`emptyDir` volumes above are the only writable paths it needs.

## Ingress (with or without a login proxy)

Plain ingress — everything open, the app's own bearer auth is the gate:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: claude-memory
spec:
  rules:
    - host: memory.example
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: claude-memory, port: { number: 8080 } } }
```

With oauth2-proxy in front of the UI instead, route the three bearer paths
straight to the service (no auth annotation) and let the proxy guard the
rest — `scripts/setup.sh doctor` verifies exactly this split works.

## UI (optional)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claude-memory-ui
spec:
  replicas: 1
  selector: { matchLabels: { app: claude-memory-ui } }
  template:
    metadata: { labels: { app: claude-memory-ui } }
    spec:
      containers:
        - name: ui
          image: ghcr.io/first-it-consulting/claude-memory-ui:latest
          ports: [{ containerPort: 8081 }]
          env:
            - name: MCP_INTERNAL_URL
              value: http://claude-memory:8080
            - name: BEARER_TOKEN          # or Keycloak OIDC; see claude-memory-ui README
              valueFrom: { secretKeyRef: { name: claude-memory-ui, key: BEARER_TOKEN } }
```

## Nightly dedup + checkpoint promotion

The dedup job calls the server's own module in-process, so run the same image
and the same embedder settings:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: claude-memory-dedup
spec:
  schedule: "30 3 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: dedup
              image: ghcr.io/first-it-consulting/claude-memory:v0.2.1
              command: ["python", "src/scripts/dedup_job.py", "--threshold", "0.92"]
              envFrom: [{ secretRef: { name: claude-memory-env } }]
```

## Flux example

The simplest reliable setup: keep the manifests above in your own Git repo
and let a Flux `Kustomization` apply them —

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: claude-memory
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/memory
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  healthChecks:
    - apiVersion: apps/v1
      kind: Deployment
      metadata: { name: claude-memory, namespace: memory }
```

(We run the same manifests through a small private Helm chart instead; a
public chart is tracked separately and not published yet.)

Pin an immutable tag per release (`v0.2.1`, or a `sha-<commit>` tag your
pipeline mirrors) — `:rolling` changes on every push and defeats
rollout bookkeeping.
