"""Shared helpers for demo scripts: start the API on a free port, tiny HTTP client, SSE reader."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        self.log = open(ROOT / ".e2e-server.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "stormchaser.api:app", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"], cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)

    def wait(self, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {self.log.name}")
            try:
                return self.get("/health")
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        raise RuntimeError("server did not become healthy")

    def call(self, method, path, body=None, **params):
        q = {k: v for k, v in params.items() if v is not None}
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read() or b"{}")}

    def get(self, path, **params):
        return self.call("GET", path, **params)

    def post(self, path, body=None, **params):
        return self.call("POST", path, body if body is not None else {}, **params)

    def stream(self, **params):
        """Yield (event, payload) from /stream until the server closes it."""
        url = self.base + "/stream?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        with urllib.request.urlopen(url, timeout=120) as r:
            event = "message"
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    yield event, json.loads(line[5:])
                    event = "message"

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def utc(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "-"
