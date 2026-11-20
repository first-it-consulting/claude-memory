"""In-process stand-in for mem0.Memory — enough of its API for main.py/memory.py.

Scores mimic mem0's pgvector provider (cosine similarity, higher is better) using
word overlap, so tests can reason about thresholds deterministically.

Tenant behaviour mirrors the real store: every entry carries a `user_id`, and
search/get_all honour the `{"user_id": ...}` filter, so multi-tenant isolation
is testable without pgvector.
"""

from __future__ import annotations

import copy
import time

CORPUS = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "memory": "prod-k8s CNPG cluster uses barman backups to MinIO",
        "user_id": "default",
        "agent_id": "claude",
        "metadata": {"metadata": {"category": "project", "tags": ["prod-k8s", "cnpg"]}},
        "created_at": "2026-11-01T10:00:00+00:00",
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "memory": "Kyverno policy require-labels blocks deployments without app label",
        "user_id": "default",
        "agent_id": "codex",
        "metadata": {"metadata": {"category": "reference", "tags": ["kyverno"]}},
        "created_at": "2026-11-10T10:00:00+00:00",
        "updated_at": "2026-11-12T08:00:00+00:00",
    },
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "memory": "Ollama runs on CPU only in the lab cluster",
        "user_id": "default",
        "agent_id": "pi",
        "metadata": {"category": "user"},
        "created_at": "2026-10-01T10:00:00+00:00",
    },
]


def _words(text: str) -> set[str]:
    return set(text.lower().replace("-", " ").replace("?", " ").split())


class FakeMemory:
    def __init__(self):
        self.reset()

    def reset(self):
        self.corpus = copy.deepcopy(CORPUS)
        self.mode = "normal"  # normal | raise | slow
        self.slow_seconds = 3.0
        self.infer_results: list[dict] = []
        self.calls: list[tuple] = []

    # --- helpers ----------------------------------------------------------
    def seed(self, memory: str, user_id: str = "default", **kw) -> str:
        """Add a corpus entry for a given tenant; returns its id."""
        rid = kw.pop("id", f"seed-{len(self.corpus) + 1}")
        entry = {
            "id": rid,
            "memory": memory,
            "user_id": user_id,
            "agent_id": kw.pop("agent_id", "claude"),
            "metadata": kw.pop("metadata", {}),
            "created_at": kw.pop("created_at", "2026-11-15T10:00:00+00:00"),
            **kw,
        }
        self.corpus.append(entry)
        return rid

    def _filtered(self, filters: dict | None) -> list[dict]:
        filters = filters or {}
        out = self.corpus
        if "user_id" in filters:
            out = [r for r in out if r.get("user_id", "default") == filters["user_id"]]
        if "agent_id" in filters:
            out = [r for r in out if r.get("agent_id") == filters["agent_id"]]
        return out

    # --- mem0 API ---------------------------------------------------------
    def search(self, query, filters=None, top_k=5):
        self.calls.append(("search", query, top_k, (filters or {}).get("user_id", "")))
        if self.mode == "raise":
            raise RuntimeError("ollama unavailable")
        if self.mode == "slow":
            time.sleep(self.slow_seconds)
        q = _words(query)
        results = []
        for r in self._filtered(filters):
            w = _words(r["memory"])
            score = min(0.99, 2 * len(q & w) / max(len(w), 1))
            results.append({**r, "score": round(score, 4)})
        results.sort(key=lambda r: r["score"], reverse=True)
        return {"results": results[:top_k]}

    def get_all(self, filters=None, top_k=100):
        self.calls.append(("get_all", (filters or {}).get("user_id", "")))
        return {"results": copy.deepcopy(self._filtered(filters))[:top_k]}

    def get(self, memory_id):
        for r in self.corpus:
            if str(r["id"]) == str(memory_id):
                return copy.deepcopy(r)
        return None

    def update(self, memory_id, content):
        self.calls.append(("update", memory_id, content))
        for r in self.corpus:
            if str(r["id"]) == str(memory_id):
                r["memory"] = content
                return {"message": "Memory updated successfully!"}
        raise ValueError(f"Memory with id {memory_id} not found")

    def delete(self, memory_id):
        self.calls.append(("delete", memory_id))
        for i, r in enumerate(self.corpus):
            if str(r["id"]) == str(memory_id):
                self.corpus.pop(i)
                return {"message": "Memory deleted successfully!"}
        raise ValueError(f"Memory with id {memory_id} not found")

    def add(self, content, **kwargs):
        """infer=True returns self.infer_results (default: nothing extracted,
        like mem0 when the LLM finds no facts); infer=False stores as-is."""
        self.calls.append(("add", content, kwargs))
        if kwargs.get("infer", True):
            return {"results": list(self.infer_results)}
        return {
            "results": [
                {"id": f"raw-{len(self.calls)}", "memory": content, "event": "ADD"}
            ]
        }
