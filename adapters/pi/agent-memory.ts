/**
 * claude-memory adapter for the Pi coding agent.
 *
 * Install: copy to ~/.pi/agent/extensions/agent-memory.ts (then /reload).
 * Config:  AGENT_MEMORY_URL / AGENT_MEMORY_TOKEN (env or ~/.config/agent-memory/env),
 *          optional AGENT_MEMORY_PROJECT, AGENT_MEMORY_RECALL=0.
 *
 * What it does (same behaviour as the Claude/Qwen/Codex hooks):
 *   - first prompt of a session  -> inject memory briefing (session id + last checkpoint)
 *   - after a compaction         -> inject briefing with the current checkpoint
 *   - every prompt               -> inject relevant memories above the score threshold
 *   - before compaction          -> auto-capture recent user turns as a safety net
 *   - tools memory_checkpoint / memory_get_checkpoint (work without MCP)
 *
 * Written against the pi extension API (>= 0.79, package @earendil-works/pi-coding-agent).
 * Older pi versions (@mariozechner/pi-coding-agent) may need `@sinclair/typebox`
 * instead of `typebox` in the import below.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";

const HARNESS = "pi";
const TOOL = "memory_checkpoint";

function loadConfig(): Record<string, string> {
  const cfg: Record<string, string> = {};
  const file = join(homedir(), ".config", "agent-memory", "env");
  if (existsSync(file)) {
    for (const raw of readFileSync(file, "utf8").split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const i = line.indexOf("=");
      cfg[line.slice(0, i).replace(/^export\s+/, "").trim()] = line.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  }
  for (const k of ["AGENT_MEMORY_URL", "AGENT_MEMORY_TOKEN", "AGENT_MEMORY_PROJECT", "AGENT_MEMORY_RECALL"]) {
    if (process.env[k]) cfg[k] = process.env[k] as string;
  }
  return cfg;
}

function projectName(cwd: string, cfg: Record<string, string>): string {
  if (cfg.AGENT_MEMORY_PROJECT) return cfg.AGENT_MEMORY_PROJECT;
  try {
    const top = execFileSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], {
      encoding: "utf8", timeout: 2000, stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (top) return basename(top);
  } catch { /* not a git repo */ }
  return basename(cwd);
}

async function call(
  cfg: Record<string, string>, method: string, path: string,
  opts: { params?: Record<string, string>; body?: unknown; timeoutMs?: number } = {},
): Promise<any> {
  const url = new URL(cfg.AGENT_MEMORY_URL.replace(/\/$/, "") + path);
  for (const [k, v] of Object.entries(opts.params ?? {})) url.searchParams.set(k, v);
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${cfg.AGENT_MEMORY_TOKEN}`,
      "Content-Type": "application/json",
      "X-Agent-ID": HARNESS,
    },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: AbortSignal.timeout(opts.timeoutMs ?? 8000),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
  return data;
}

// Generic {role, content} walker — same idea as memory_hook.py, so we don't
// depend on the exact shape of pi's session entries.
function recentTurns(entries: unknown): string {
  const msgs: Array<[string, string]> = [];
  const textOf = (c: unknown): string =>
    typeof c === "string" ? c
      : Array.isArray(c) ? c.filter((p: any) => p && typeof p.text === "string" && (!p.type || p.type === "text"))
          .map((p: any) => p.text).join("\n")
      : "";
  const walk = (o: any): void => {
    if (Array.isArray(o)) return o.forEach(walk);
    if (!o || typeof o !== "object") return;
    if ((o.role === "user" || o.role === "assistant") && "content" in o) {
      const t = textOf(o.content).trim();
      if (t) msgs.push([o.role, t]);
      return;
    }
    Object.values(o).forEach((v) => typeof v === "object" && walk(v));
  };
  walk(entries);
  const users = msgs.filter(([r, t]) => r === "user" && !t.trimStart().startsWith("<")).map(([, t]) => t).slice(-8);
  const assistants = msgs.filter(([r]) => r === "assistant").map(([, t]) => t).slice(-2);
  if (!users.length && !assistants.length) return "";
  const flat = (s: string, n: number) => s.slice(0, n).replace(/\n/g, " ");
  return [
    "Recent user requests (oldest first):", ...users.map((u) => `- ${flat(u, 300)}`),
    ...(assistants.length ? ["Last assistant output:", ...assistants.map((a) => `> ${flat(a, 600)}`)] : []),
  ].join("\n");
}

export default function (pi: ExtensionAPI) {
  const cfg = loadConfig();
  const enabled = Boolean(cfg.AGENT_MEMORY_URL && cfg.AGENT_MEMORY_TOKEN);
  const warn = (msg: string) => console.error(`[agent-memory] ${msg}`);
  if (!enabled) {
    warn("AGENT_MEMORY_URL / AGENT_MEMORY_TOKEN not set — extension inactive");
    return;
  }

  let pendingBriefing: "startup" | "resume" | "compact" | null = null;

  pi.on("session_start", async (event: any) => {
    if (event.reason === "reload") return;
    pendingBriefing = event.reason === "resume" || event.reason === "fork" ? "resume" : "startup";
  });

  pi.on("session_before_compact", async (event: any, ctx: any) => {
    try {
      const sessionId = ctx.sessionManager?.getSessionId?.();
      const notes = recentTurns(event.branchEntries ?? ctx.sessionManager?.getBranch?.());
      if (sessionId && notes) {
        await call(cfg, "POST", "/api/hooks/precompact", {
          body: { session_id: sessionId, project: projectName(ctx.cwd, cfg), harness: HARNESS, notes },
        });
      }
    } catch (e) {
      warn(`pre-compact capture failed: ${e}`);
    }
    return undefined; // never interfere with pi's own compaction
  });

  pi.on("session_compact", async () => {
    pendingBriefing = "compact";
  });

  pi.on("before_agent_start", async (event: any, ctx: any) => {
    const parts: string[] = [];
    const sessionId: string = ctx.sessionManager?.getSessionId?.() ?? "";
    const project = projectName(ctx.cwd, cfg);
    if (pendingBriefing) {
      try {
        const res = await call(cfg, "GET", "/api/hooks/briefing", {
          params: { session_id: sessionId, project, source: pendingBriefing, harness: HARNESS, tool: TOOL },
          timeoutMs: 10000,
        });
        if (res.context) parts.push(res.context);
        pendingBriefing = null;
      } catch (e) {
        warn(`briefing failed: ${e}`);
      }
    }
    if (cfg.AGENT_MEMORY_RECALL !== "0" && event.prompt) {
      try {
        const res = await call(cfg, "GET", "/api/hooks/recall", {
          params: { q: String(event.prompt).slice(0, 1000) }, timeoutMs: 6000,
        });
        if (res.context) parts.push(res.context);
      } catch (e) {
        warn(`recall failed: ${e}`);
      }
    }
    if (!parts.length) return undefined;
    return { message: { customType: "agent-memory", content: parts.join("\n\n"), display: false } };
  });

  pi.registerTool({
    name: TOOL,
    label: "Memory checkpoint",
    description:
      "Save your working state for this session (stored instantly, re-injected after context compaction). " +
      "Call after each milestone, decision or failed attempt with the FULL current state, condensed: " +
      "## Goal / ## Done / ## Decisions (+why) / ## Dead ends / ## Key files / ## Next. Overwrites the previous checkpoint.",
    parameters: Type.Object({
      state: Type.String({ description: "Full current working state in the sections above (max ~6000 chars)" }),
    }),
    async execute(_id: string, params: { state: string }, _signal: unknown, _onUpdate: unknown, ctx: any) {
      const sessionId = ctx.sessionManager?.getSessionId?.();
      if (!sessionId) throw new Error("no session id (ephemeral session?)");
      const res = await call(cfg, "PUT", `/api/sessions/${encodeURIComponent(sessionId)}`, {
        body: { state: params.state, project: projectName(ctx.cwd, cfg), harness: HARNESS },
      });
      return { content: [{ type: "text", text: `Checkpoint saved (${res.updated_at}).` }], details: {} };
    },
  });

  pi.registerTool({
    name: "memory_get_checkpoint",
    label: "Read memory checkpoint",
    description: "Read the saved checkpoint of this session, or with `project` the latest checkpoint of that project.",
    parameters: Type.Object({
      project: Type.Optional(Type.String({ description: "Project name to load the latest checkpoint from" })),
    }),
    async execute(_id: string, params: { project?: string }, _signal: unknown, _onUpdate: unknown, ctx: any) {
      let row: any = null;
      if (params.project) {
        const rows = await call(cfg, "GET", "/api/sessions", { params: { project: params.project, limit: "1" } });
        row = rows[0] ?? null;
      } else {
        const sessionId = ctx.sessionManager?.getSessionId?.();
        row = sessionId ? await call(cfg, "GET", `/api/sessions/${encodeURIComponent(sessionId)}`).catch(() => null) : null;
      }
      const text = row
        ? [`Checkpoint (session ${row.session_id}, updated ${row.updated_at})`, row.state, row.auto_notes && `Auto-captured:\n${row.auto_notes}`]
            .filter(Boolean).join("\n")
        : "No checkpoint found.";
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
