# Decision: opt-in passive capture per tenant (spike, bean rkky)

Status: **NO-GO for 0.2.0 - revisit gated on two prerequisites** (see
Recommendation). Decision doc only; nothing was built.

## The idea (from claude-mem)

Every harness tool call (`PostToolUse`) ships to the server, an "observer" LLM
turns each event into a typed observation (`fact | decision | dead-end |
reference`), and observations flow into the normal memory pipeline. The agent
never has to remember to store anything - capture is passive.

## Design sketch (if it were built)

```
harness PostToolUse hook (thin, already exists as adapter base)
  -> POST /api/hooks/capture  {session, tool, event_digest}   (async, 202)
  -> server redaction pass (regex + entropy filter, BEFORE any LLM sees it)
  -> per-tenant opt-in check (users.passive_capture flag, default off)
  -> queue (same Redis/thread pool as infer adds)
  -> observer LLM prompt (event -> JSON observations, max 1, [] allowed)
  -> store with metadata {capture: "passive"} so they are filterable,
     demoted in recall ranking, and swept by the existing dedup/promotion job
```

Guardrails that would be mandatory: off-by-default per tenant; redaction
before the observer; passive memories tagged and down-ranked; hard rate limit
per tenant; raw tool output never persisted (only the observation).

## Measurements (our hardware, 2026-11-21)

NAS Ollama `llama3.2` (2 GB), warm model, `/api/generate`, temperature 0.1.
Note: `/api/chat` on that build returned empty content for this model - the
implementation would need `/api/generate` or the `/v1` OpenAI-compat route.

| Probe | Result | Wall time |
|---|---|---|
| 1 substantive tool result | good typed observation (`fact`, correct content) | 5.5 s (112 gen tok) |
| 1 connection-refused error | good `dead-end` observation | 3.9 s |
| 2 trivial reads (`package.json`) | **2/2 invented a useless `fact`** instead of `[]` | ~1.1 s each |
| 1 login response containing a token | **leaked the token into the observation text** | 1.4 s |

Cost per 100 tool calls: ~35-45k prompt tokens + ~10-25k generated tokens,
~2-8 minutes of sequential GPU on the NAS - the same box that serves
embeddings for every write and every recall; the observer queue would
contend with exactly the latency-sensitive path.

## Noise and privacy

- Noise: the sampled trivial-event false-positive rate is 2/2. A small sample,
  but the failure mode is structural: a 3B model asked to find something
  durable will find something. Expected effect without aggressive filtering:
  the corpus fills with version strings and file reads - the opposite of the
  "every line is worth its tokens" property the index modes just won.
- Privacy: the secret probe shows the observer path is a **secret
  exfiltration pipeline into long-term memory** unless the redaction pass is
  built first and provably runs before any LLM call. Tool output routinely
  contains tokens, connection strings, customer data.

## Recommendation

**No-go now.** The existing session-level path (explicit `checkpoint`,
`PreCompact` turn capture, nightly dedup/promotion) already delivers most of
the value at ~0.1x the LLM cost and without a per-tool-call privacy surface.

Revisit when both prerequisites exist:
1. a redaction pipeline that is tested against a secret corpus and runs
   pre-LLM (this is useful for the existing capture paths anyway), and
2. a per-tenant settings surface (UI/API flag), so "opt-in" is real and not
   an env-var edit by an admin.

If revisited, start with `PostToolUse` only for *failed* commands (the
`dead-end` probe was the strongest sample - errors are rare, durable, and
cheap to filter), not all tool calls.
