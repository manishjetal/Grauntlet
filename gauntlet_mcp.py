import os
import random
import json
import hashlib
import urllib.request
import requests
import pyotp
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

mcp = FastMCP("gauntlet")


def _get(path):
    req = urllib.request.Request(BASE_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


@mcp.tool()
def roll_dice(sides: int = 6) -> int:
    """Roll a dice with the given number of sides."""
    return random.randint(1, sides)


@mcp.tool()
def get_india_status() -> str:
    """Check whether Fyers/Upstox tokens are configured and ready"""
    return json.dumps(_get("/india/status"))


@mcp.tool()
def refresh_fyers_token() -> str:
    """
    Refresh the daily Fyers access token via TOTP login and push it to
    this service's environment variable FYERS_ACCESS_TOKEN on Render.
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
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
