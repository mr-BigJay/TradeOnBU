#!/usr/bin/env python3
"""Local OAuth helper for Sign in with ChatGPT (SIWC) on self-hosted TradeOnBU.

Run on a machine with a desktop browser. Opens loopback callback on 127.0.0.1:1455.
Requires: connect_token and dashboard URL from Settings → Connect ChatGPT.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event

import httpx

REDIRECT = "http://127.0.0.1:1455/auth/callback"


def main() -> None:
    base = os.environ.get("TRADEONBU_URL", "").rstrip("/")
    connect_token = os.environ.get("CONNECT_TOKEN", "")
    session_cookie = os.environ.get("TRADEONBU_SESSION_COOKIE", "")
    if not base or not connect_token:
        print("Set TRADEONBU_URL and CONNECT_TOKEN", file=sys.stderr)
        sys.exit(1)

    headers = {}
    if session_cookie:
        headers["Cookie"] = f"tradeonbu_session={session_cookie}"

    url = os.environ.get("AUTHORIZE_URL", "")
    if not url:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            begin = client.post(f"{base}/api/gui/chatgpt/connect/begin")
            begin.raise_for_status()
            data = begin.json()
            url = data["authorize_url"]
            connect_token = data["connect_token"]

    result: dict[str, str] = {}
    done = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if "error" in qs:
                result["error"] = qs["error"][0]
            else:
                result["code"] = qs.get("code", [""])[0]
                result["client_id"] = qs.get("client_id", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><p>TradeOnBU: you can close this tab.</p></body></html>")
            done.set()

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 1455), Handler)
    print("Opening browser for ChatGPT authorization…")
    webbrowser.open(url)
    while not done.is_set():
        server.handle_request()

    if result.get("error"):
        print("OAuth error:", result["error"], file=sys.stderr)
        sys.exit(1)
    if not result.get("code"):
        print("Missing authorization code", file=sys.stderr)
        sys.exit(1)

    payload = {
        "connect_token": connect_token,
        "code": result["code"],
        "callback_client_id": result.get("client_id") or None,
    }
    with httpx.Client(timeout=60.0, headers=headers) as client:
        resp = client.post(f"{base}/api/gui/chatgpt/connect/complete", json=payload)
        if resp.status_code >= 400:
            print(resp.text, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
