#!/usr/bin/env python3
"""Minimal mock of the MedWear API for integration testing."""
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

RECEIVED_FILE = "/tmp/received_batches.json"
received = []
sessions = {}   # session_id -> registration


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if self.path == "/v1/data/auth/token":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "access_token": "test-token",
                "expires_in": 3600
            }).encode())

        elif self.path == "/v1/data/session":
            reg = json.loads(body)
            sessions[reg["session_id"]] = reg
            print(f"[MOCK API] POST /session — {reg['signal_type']} {reg['header']['schema_id']}", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"session_id": reg["session_id"], "project_id": reg["project_id"],
                                         "status": "created"}).encode())

        elif self.path == "/v1/data/batch":
            payload = json.loads(body)
            if "session_id" in payload:   # lean batch: metadata comes from its session
                if payload["session_id"] not in sessions:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"detail": {"error": "UNKNOWN_SESSION"}}')
                    return
                payload = {**payload, "signal_type": sessions[payload["session_id"]]["signal_type"]}
            received.append(payload)
            with open(RECEIVED_FILE, "w") as f:
                json.dump(received, f, indent=2)
            signal_type = payload.get("signal_type", "?")
            n_samples = len(
                payload.get("body", {}).get("signal_data")
                or payload.get("body", {}).get("ecg_lead_data")
                or []
            )
            print(f"[MOCK API] POST /batch — signal={signal_type} samples={n_samples}", flush=True)
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    if os.path.exists(RECEIVED_FILE):
        os.remove(RECEIVED_FILE)
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    print("[MOCK API] Listening on :8080", flush=True)
    server.serve_forever()