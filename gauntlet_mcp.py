import os
import random
from fastmcp import FastMCP

mcp = FastMCP("gauntlet")

@mcp.tool()
def roll_dice(sides: int = 6) -> int:
    """Roll a dice with the given number of sides."""
    return random.randint(1, sides)

if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
