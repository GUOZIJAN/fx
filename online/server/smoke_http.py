"""End-to-end HTTP smoke test against a running local instance.

Run with: python smoke_http.py [base URL]
Uses two fresh guest sessions on the target server; use a test instance.
"""

import sys
import time

import httpx


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
    with httpx.Client(base_url=base, timeout=8, trust_env=False) as first, httpx.Client(base_url=base, timeout=8, trust_env=False) as second:
        headers = {"X-FX-Action": "1", "Origin": "https://www.bilibili.com"}
        a = first.post("api/session", headers=headers).raise_for_status().json()
        b = second.post("api/session", headers=headers).raise_for_status().json()
        assert a["account"]["id"] != b["account"]["id"]
        before = b["markets"]["EUR/USD"]["price"]
        first.post("api/order", json={"pair": "EUR/USD", "side": 1, "margin": 500, "leverage": 20},
                   headers={**headers, "Authorization": f"Bearer {a['sessionToken']}"}).raise_for_status()
        updated = second.post("api/session", headers={**headers, "Authorization": f"Bearer {b['sessionToken']}"}).raise_for_status().json()
        assert updated["markets"]["EUR/USD"]["price"] > before
        assert updated["account"]["cash"] == 10000
        assert len(updated["leaderboard"]) >= 2
        first_account = first.post("api/session", headers={**headers, "Authorization": f"Bearer {a['sessionToken']}"}).raise_for_status().json()["account"]
        assert len(first_account["positions"]) == 1
        time.sleep(.4)
        first.post("api/close", json={"id": first_account["positions"][0]["id"]},
                   headers={**headers, "Authorization": f"Bearer {a['sessionToken']}"}).raise_for_status()
        print("PASS: two accounts share market, keep separate funds, close positions, and appear in leaderboard")
        print("Test player IDs:", a["account"]["id"], b["account"]["id"])


if __name__ == "__main__":
    main()
