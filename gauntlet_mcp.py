import os
import random
import json
import urllib.request
from fastmcp import FastMCP

BASE_URL = os.environ.get("GAUNTLET_BASE_URL", "https://gauntlet-6eh9.onrender.com")

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


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
