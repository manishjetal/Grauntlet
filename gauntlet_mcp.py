#!/usr/bin/env python3
"""
gauntlet_mcp.py — MCP server wrapping the live GAUNTLET dashboard API
+ Fyers TOTP auto-login/refresh, pushed to Render env vars directly.
"""
import os, json, hashlib, urllib.request, urllib.error
import requests, pyotp
from fastmcp import FastMCP

BASE_URL = os.environ.get("GAUNTLET_BASE_URL", "https://gauntlet-6eh9.onrender.com")

FY_ID        = os.environ.get("FYERS_ID")
APP_ID       = os.environ.get("FYERS_APP_ID")
APP_SECRET   = os.environ.get("FYERS_APP_SECRET")
PIN          = os.environ.get("FYERS_PIN")
TOTP_SECRET  = os.environ.get("FYERS_TOTP_SECRET")
REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1")

RENDER_API_KEY      = os.environ.get("RENDER_API_KEY")
GAUNTLET_SERVICE_ID = os.environ.get("GAUNTLET_SERVICE_ID")

mcp = FastMCP("GAUNTLET")


def _get(path):
    req = urllib.request.Request(BASE_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')}"}


@mcp.tool()
def get_positions() -> str:
    """Get current open positions and live signal log"""
    data = _get("/signal-log")
    return json.dumps(data.get("positions", {}))


@mcp.tool()
def get_pnl() -> str:
    """Get today's daily P&L"""
    data = _get("/signal-log")
    return json.dumps({"daily_pnl": data.get("daily_pnl", 0)})


@mcp.tool()
def get_signals() -> str:
    """Get the latest strategy signals"""
    data = _get("/signal-log")
    return json.dumps(data.get("signals", []))


@mcp.tool()
def get_india_status() -> str:
    """Check whether Fyers/Upstox tokens are configured and ready"""
    return json.dumps(_get("/india/status"))


@mcp.tool()
def place_fyers_order(symbol: str, qty: int, side: str, order_type: str = "MARKET") -> str:
    """
    Place a live order via Fyers. side='BUY' or 'SELL'.
    WARNING: this executes a real order against your Fyers account.
    """
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": 1 if side.upper() == "BUY" else -1,
        "type": 2 if order_type.upper() == "MARKET" else 1,
        "productType": "INTRADAY",
        "validity": "DAY",
    }
    return json.dumps(_post("/fyers/order", payload))


@mcp.tool()
def refresh_fyers_token() -> str:
    """
    Refresh the daily Fyers access token via TOTP login and push it to the
    live GAUNTLET proxy service as an environment variable on Render.
    """
    missing = [n for n, v in {
        "FYERS_ID": FY_ID, "FYERS_APP_ID": APP_ID, "FYERS_APP_SECRET": APP_SECRET,
        "FYERS_PIN": PIN, "FYERS_TOTP_SECRET": TOTP_SECRET,
        "RENDER_API_KEY": RENDER_API_KEY, "GAUNTLET_SERVICE_ID": GAUNTLET_SERVICE_ID,
    }.items() if not v]
    if missing:
        return json.dumps({"ok": False, "error": f"missing env vars: {', '.join(missing)}"})

    try:
        s = requests.Session()
        r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp",
                     json={"fy_id": FY_ID, "app_id": "2"})
        r1.raise_for_status()
        request_key = r1.json()["request_key"]

        r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp",
                     json={"request_key": request_key, "otp": pyotp.TOTP(TOTP_SECRET).now()})
        r2.raise_for_status()
        request_key2 = r2.json()["request_key"]

        r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin",
                     json={"request_key": request_key2, "identity_type": "pin", "identifier": PIN})
        r3.raise_for_status()
        temp_token = r3.json()["data"]["access_token"]

        headers = {"authorization": f"Bearer {temp_token}"}
        payload = {
            "fyers_id": FY_ID, "app_id": APP_ID.split("-")[0],
            "redirect_uri": REDIRECT_URI, "appType": APP_ID.split("-")[1],
            "code_challenge": "", "state": "gauntlet_auto", "scope": "", "nonce": "",
            "response_type": "code", "create_cookie": True
        }
        r4 = requests.post("https://api-t1.fyers.in/api/v3/token", json=payload, headers=headers)
        r4.raise_for_status()
        auth_code = r4.json()["Url"].split("auth_code=")[1].split("&")[0]

        app_id_hash = hashlib.sha256(f"{APP_ID}:{APP_SECRET}".encode()).hexdigest()
        r5 = requests.post("https://api-t1.fyers.in/api/v3/validate-authcode", json={
            "grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code
        })
        r5.raise_for_status()
        access_token = r5.json()["access_token"]

    except Exception as e:
        return json.dumps({"ok": False, "error": f"fyers login failed: {e}"})

    try:
        render_resp = requests.put(
            f"https://api.render.com/v1/services/{GAUNTLET_SERVICE_ID}/env-vars/FYERS_ACCESS_TOKEN",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                     "Content-Type": "application/json"},
            json={"value": access_token}
        )
        render_resp.raise_for_status()
    except Exception as e:
        return json.dumps({"ok": False, "error": f"got token but failed to push to Render: {e}"})

    return json.dumps({"ok": True, "message": "Fyers token refreshed and pushed to Render"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)

