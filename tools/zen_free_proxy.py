#!/usr/bin/env python3
"""OpenAI-compatible /v1/chat/completions shim backed by `opencode serve`.

Hindsight (or any OpenAI client) -> this shim -> opencode server session API,
so Zen free-tier models are always invoked from within OpenCode itself.
"""
import json, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OC = "http://127.0.0.1:8901"
FREE_MODEL = "nemotron-3-ultra-free"   # opencode provider free model (reasoning split into separate parts)


def oc_post(path, payload, timeout=600):
    req = urllib.request.Request(OC + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def chat_complete(messages):
    # join all message contents into one user prompt
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
        parts.append(f"[{m.get('role','user')}]\n{c}")
    prompt = "\n\n".join(parts)
    prompt += "\n\nIMPORTANT: respond with ONLY the direct answer/output. No tools, no file edits, no commentary."

    sess = oc_post("/session", {})
    resp = oc_post(f"/session/{sess['id']}/message", {
        "model": {"providerID": "opencode", "modelID": FREE_MODEL},
        "agent": "build",
        "parts": [{"type": "text", "text": prompt}],
    })
    text = "".join(p.get("text", "") for p in resp.get("parts", []) if p.get("type") == "text")
    return text


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json({"object": "list", "data": [
                {"id": FREE_MODEL, "object": "model", "owned_by": "opencode-zen-free"}]})
        else:
            self._json({"ok": True})

    def do_POST(self):
        if "/chat/completions" not in self.path:
            return self._json({"error": "not found"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = chat_complete(body.get("messages", []))
            self._json({
                "id": "chatcmpl-shim", "object": "chat.completion", "created": 0,
                "model": FREE_MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
        except Exception as e:
            self._json({"error": {"type": "server_error", "message": str(e)}}, 500)

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    print("zen-free-proxy on :8081 -> opencode :8901, model", FREE_MODEL)
    ThreadingHTTPServer(("127.0.0.1", 8081), H).serve_forever()
