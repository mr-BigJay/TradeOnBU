#!/usr/bin/env python3
"""Local SIWC OAuth helper for self-hosted TradeOnBU (desktop → VPS).

Completes loopback OAuth on 127.0.0.1:1455 and posts the authorization code to the
server public connect endpoint using a one-time connect secret from the dashboard.
No dashboard session cookie or CSRF token is required.

Required environment variables (from dashboard → تحلیل AI → Connect):
  TRADEONBU_URL, CONNECT_TOKEN, CONNECT_SECRET, OAUTH_STATE, AUTHORIZE_URL
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

REDIRECT_PATH = "/auth/callback"


def main() -> None:
    base = os.environ.get("TRADEONBU_URL", "").rstrip("/")
    connect_token = os.environ.get("CONNECT_TOKEN", "")
    connect_secret = os.environ.get("CONNECT_SECRET", "")
    oauth_state = os.environ.get("OAUTH_STATE", "")
    url = os.environ.get("AUTHORIZE_URL", "")
    missing = [k for k, v in [
        ("TRADEONBU_URL", base),
        ("CONNECT_TOKEN", connect_token),
        ("CONNECT_SECRET", connect_secret),
        ("OAUTH_STATE", oauth_state),
        ("AUTHORIZE_URL", url),
    ] if not v]
    if missing:
        print("Missing env:", ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    result: dict[str, str] = {}
    done = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if "error" in qs:
                result["error"] = qs["error"][0]
            else:
                result["code"] = qs.get("code", [""])[0]
                result["state"] = qs.get("state", [""])[0]
                result["client_id"] = qs.get("client_id", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><p>TradeOnBU: authorization received. You may close this tab.</p></body></html>")
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
    if result.get("state") != oauth_state:
        print("OAuth state mismatch — aborting (possible CSRF)", file=sys.stderr)
        sys.exit(1)

    payload = {
        "connect_token": connect_token,
        "oauth_state": oauth_state,
        "code": result["code"],
        "callback_client_id": result.get("client_id") or None,
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{base}/api/public/siwc/connect/complete",
            json=payload,
            headers={"X-Connect-Secret": connect_secret},
        )
        if resp.status_code >= 400:
            print(resp.text, file=sys.stderr)
            sys.exit(1)
        out = resp.json()
        print(json.dumps({"ok": out.get("ok"), "email": out.get("email")}, indent=2))


if __name__ == "__main__":
    main()
