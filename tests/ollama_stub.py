"""Deterministic Ollama API stub for the compose smoke test (tests/ only -
never part of the image or the production compose).

Fake: embedding vectors and the LLM extraction. Real: postgres+pgvector,
the server image, HTTP/MCP/auth paths.

Wire format matches mem0 2.2.1 with the ollama python client 0.6.3:
  GET  /api/tags   -> model list (embedder registers without a pull)
  POST /api/pull   -> success (fallback if a pull is ever attempted)
  POST /api/embed  -> {"embeddings": [[...]]} 768 floats per input item
  POST /api/chat   -> {"message": {"content": "<json>"}} where the JSON is
                      mem0's extraction contract {"memory":[{"text":...}]}
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 768  # must match _EMBEDDING_DIMS in src/memory.py
VEC = [0.0] * DIM
VEC[0] = 1.0  # fixed unit vector: every text is maximally similar
MODEL = "nomic-embed-text:latest"
EXTRACTION = json.dumps({"memory": [{"text": "rest-inferred smoke fact"}]})


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": MODEL, "model": MODEL}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            req = {}
        if self.path.startswith("/api/embed"):
            inp = req.get("input", "")
            items = inp if isinstance(inp, list) else [inp]
            self._send({"embeddings": [VEC for _ in items]})
        elif self.path.startswith("/api/chat"):
            self._send({"message": {"role": "assistant", "content": EXTRACTION}, "done": True})
        elif self.path.startswith("/api/generate"):
            self._send({"response": EXTRACTION, "done": True})
        elif self.path.startswith("/api/pull"):
            self._send({"status": "success"})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
