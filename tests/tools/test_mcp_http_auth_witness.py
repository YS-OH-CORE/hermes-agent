"""A background HTTP challenge must not relabel an unrelated tool failure.

Supports kvnloo's call-attribution review on Hermes PR #114591. This case uses
the SDK's optional GET stream rather than two simultaneous tools/call RPCs.
The only simulated component is a loopback MCP server; discovery, registration,
HTTP transport, SDK, RPC lock, and error recovery are the real Hermes paths.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


pytestmark = pytest.mark.integration


@contextmanager
def _http_server(get_status: int, call_status: int):
    wire = []
    wire_lock = threading.Lock()
    get_retried = threading.Event()

    def record(method, status, message=None):
        row = {"http_method": method, "status": status}
        if message is not None:
            row.update({"rpc_method": message.get("method"), "id": message.get("id")})
            if message.get("method") == "tools/call":
                row["mode"] = message.get("params", {}).get("arguments", {}).get("mode")
        with wire_lock:
            wire.append(row)
            # A second GET proves that the SDK processed the first response,
            # including the real response hook, before retrying. No sleeps or
            # reads/writes of Hermes' private marker are needed to order the case.
            if method == "GET" and sum(r["http_method"] == "GET" for r in wire) >= 2:
                get_retried.set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def respond(self, status, payload=None, *, initialize=False):
            data = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if initialize:
                self.send_header("Mcp-Session-Id", "http-auth-witness")
            self.end_headers()
            if data:
                self.wfile.write(data)
            self.wfile.flush()

        def do_GET(self):
            record("GET", get_status)
            self.respond(get_status, {"error": "synthetic GET rejection"})

        def do_DELETE(self):
            record("DELETE", 200)
            self.respond(200, {})

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = message.get("method")
            params = message.get("params", {})
            if "id" not in message:
                record("POST", 202, message)
                self.respond(202)
                return
            if method == "initialize":
                result = {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-auth-witness", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "probe", "description": "Synthetic read-only probe",
                    "inputSchema": {"type": "object", "properties": {
                        "mode": {"type": "string", "enum": ["ok", "reject"]}},
                        "required": ["mode"]},
                    "annotations": {"readOnlyHint": True},
                }]}
            elif method == "tools/call":
                if params.get("arguments", {}).get("mode") == "reject":
                    record("POST", call_status, message)
                    # Deliberately not a JSON-RPC error: exercise the SDK's real
                    # generic non-2xx fallback, the behavior this PR addresses.
                    self.respond(call_status, {"error": "synthetic tool rejection"})
                    return
                result = {"content": [{"type": "text", "text": "witness-ok"}], "isError": False}
            else:
                result = {}
            record("POST", 200, message)
            self.respond(200, {"jsonrpc": "2.0", "id": message["id"], "result": result},
                         initialize=method == "initialize")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", get_retried, wire
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("get_status,call_status,needs_signin", [
    pytest.param(405, 500, False, id="generic-without-get-challenge"),
    pytest.param(401, 500, False, id="generic-after-get-challenge"),
    pytest.param(405, 401, True, id="call-time-challenge"),
])
def test_auth_diagnosis_belongs_to_the_failed_call(get_status, call_status, needs_signin, tmp_path):
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import registry

    name = "http_auth_witness"
    tool = f"mcp__{name}__probe"
    with _http_server(get_status, call_status) as (url, get_retried, wire):
        try:
            registered = register_mcp_servers({name: {
                "url": url, "connect_timeout": 20, "timeout": 20,
            }})
            assert tool in registered, {"registered": registered, "wire": wire}
            assert get_retried.wait(15), {"missing": "SDK GET retry", "wire": wire}

            before = json.loads(registry.dispatch(tool, {"mode": "ok"}))
            rejected = json.loads(registry.dispatch(tool, {"mode": "reject"}))
            after = json.loads(registry.dispatch(tool, {"mode": "ok"}))
            observation = {"get_status": get_status, "call_status": call_status,
                           "expected_needs_signin": needs_signin, "before": before,
                           "rejected": rejected, "after": after, "wire": list(wire)}
            (tmp_path / "observation.json").write_text(
                json.dumps(observation, indent=2) + "\n", encoding="utf-8")

            assert before.get("result") == after.get("result") == "witness-ok", observation
            calls = [r for r in wire if r.get("rpc_method") == "tools/call"]
            assert [(r["mode"], r["status"]) for r in calls] == [
                ("ok", 200), ("reject", call_status), ("ok", 200)], observation
            first_call = wire.index(calls[0])
            assert not any(r.get("rpc_method") in {"initialize", "tools/list"}
                           for r in wire[first_call + 1:]), observation
            assert "error" in rejected, observation
            assert rejected.get("needs_reauth", False) is needs_signin, observation
        finally:
            shutdown_mcp_servers(names={name})
