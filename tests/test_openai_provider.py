"""OpenAI-compatible provider wiring + dimension guard (bean homelab-saec).

Uses a fake OpenAI-compatible server (real mem0 OpenAIEmbedding client
against it) - no real providers are contacted.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.db

# fake test token: short + low-entropy on purpose (gitleaks generic-api-key
# threshold is 20 chars; the repo convention for test fixtures). The fake
# server only compares it, and the guard tests only assert it never leaks.
KEY = "fake-key"
DIM = 768


class FakeOpenAI(BaseHTTPRequestHandler):
    dim = DIM

    def log_message(self, *args):  # silence
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        self.rfile.read(n)
        if self.headers.get("authorization") != f"Bearer {KEY}":
            self._json({"error": {"message": "invalid api key"}}, status=401)
            return
        if self.path.startswith("/v1/embeddings"):
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.1] * self.dim,
                        }
                    ],
                }
            )
        elif self.path.startswith("/v1/chat/completions"):
            self._json(
                {
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        else:
            self._json({"error": "not found"}, status=404)


@pytest.fixture(scope="module")
def fake_openai():
    srv = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_provider_config_mapping(settings_override):
    import importlib

    import memory

    settings_override(
        embed_provider="openai",
        embed_base_url="http://gw.local:4000/v1",
        embed_model="bge-m3",
        embed_api_key=KEY,
        llm_provider="openai",
        llm_base_url="http://gw.local:4000/v1",
        llm_model="qwen",
        llm_api_key=KEY,
    )
    emb = memory._embedder_config()
    llm = memory._llm_config()
    assert emb.provider == "openai"
    assert emb.config["model"] == "bge-m3"
    assert emb.config["openai_base_url"] == "http://gw.local:4000/v1"
    # dims deliberately NOT sent to the API (vLLM/TEI reject `dimensions`)
    assert "embedding_dims" not in emb.config
    assert llm.provider == "openai"
    assert llm.config["openai_base_url"] == "http://gw.local:4000/v1"
    del importlib


def test_api_key_file_wins(tmp_path):
    import memory

    f = tmp_path / "key"
    f.write_text("  from-file  \n")
    assert memory._api_key("from-env", str(f)) == "from-file"
    assert memory._api_key("from-env", "") == "from-env"
    assert memory._api_key("", str(tmp_path / "missing")) == ""


def test_mem0_client_against_fake_server(fake_openai):
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from mem0.embeddings.openai import OpenAIEmbedding

    emb = OpenAIEmbedding(
        config=BaseEmbedderConfig(
            model="fake-model", api_key=KEY, openai_base_url=fake_openai
        )
    )
    vec = emb.embed("hello", memory_action="add")
    assert len(vec) == DIM


def test_wrong_key_is_401(fake_openai):
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from mem0.embeddings.openai import OpenAIEmbedding

    emb = OpenAIEmbedding(
        config=BaseEmbedderConfig(
            model="fake-model", api_key="sk-wrong", openai_base_url=fake_openai
        )
    )
    with pytest.raises(Exception) as ei:
        emb.embed("hello", memory_action="add")
    assert "401" in str(ei.value) or "invalid api key" in str(ei.value)


def test_dimension_guard_blocks_writes(api, monkeypatch):
    import memory

    monkeypatch.setattr(
        memory,
        "_dim_guard",
        {"ok": False, "msg": "embed model x returns 1024 dims, column is 768"},
    )
    r = api.post("/api/memories", {"content": "should be refused"})
    assert r.status_code == 503
    assert "writes disabled" in r.text
    h = api.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "degraded"
    assert body["embedding_guard"]["ok"] is False


def test_probe_dims_detects_mismatch(monkeypatch):
    import memory

    class StubEmb:
        def embed(self, text, memory_action=None):
            return [0.0] * 8  # wrong width

    class StubMem:
        embedding_model = StubEmb()

    monkeypatch.setattr(memory.settings, "embed_model", "wide-model")
    memory._probe_dims(StubMem())
    assert memory.embedding_guard()["ok"] is False
    assert "8 dims" in memory.embedding_guard()["msg"]
    with pytest.raises(RuntimeError):
        memory._guard_for_writes()
    # restore for other tests in this module/session
    monkeypatch.setattr(memory, "_dim_guard", {"ok": True, "msg": ""})


def test_startup_log_never_contains_api_key(fake_openai, caplog, monkeypatch):
    import logging

    import memory

    monkeypatch.setattr(
        memory.settings,
        "embed_base_url",
        fake_openai,
    )
    monkeypatch.setattr(memory.settings, "embed_api_key", KEY)
    monkeypatch.setattr(memory.settings, "embed_provider", "openai")
    monkeypatch.setattr(memory.settings, "embed_model", "fake-model")
    with caplog.at_level(logging.INFO, logger="memory"):
        memory.logger.info(
            "providers: embed=%s (%s / %s) llm=%s (%s / %s)",
            memory.settings.embed_provider,
            memory._provider_host(memory.settings.embed_base_url),
            memory.settings.embed_model,
            memory.settings.llm_provider,
            memory._provider_host(memory.settings.llm_base_url),
            memory.settings.llm_model,
        )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert KEY not in joined
    assert "fake-model" in joined
