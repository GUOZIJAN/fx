"""Check a public WebSocket route with a fresh test guest.

Run: python smoke_ws.py http://HOST/fx-online/
Prints the test player ID for removal from the production database.
"""

import json
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.sync.client import connect


base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787/").rstrip("/") + "/"
with httpx.Client(base_url=base, timeout=8, trust_env=False) as client:
    session = client.post("api/session", headers={"Origin": "https://www.bilibili.com", "X-FX-Action": "1"}).raise_for_status().json()
    account = session["account"]
    url = urlsplit(base)
    ws_url = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc, url.path + "ws", "", ""))
    with connect(ws_url, origin="https://www.bilibili.com", open_timeout=8) as socket:
        socket.send(json.dumps({"sessionToken": session["sessionToken"]}))
        snapshot = json.loads(socket.recv(timeout=8))
        assert snapshot["type"] == "snapshot"
        assert snapshot["account"]["id"] == account["id"]
        assert snapshot["online"] >= 1
        print("PASS: public WebSocket delivers live account and online count")
        print("Test player ID:", account["id"])
