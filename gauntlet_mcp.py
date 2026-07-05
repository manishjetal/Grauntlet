import os
from fastmcp import FastMCP

mcp = FastMCP("gauntlet")

# ... your @mcp.tool() functions ...

if __name__ == "__main__":
    mcp.run(
        transport="http",          # streamable HTTP → serves at /mcp
        host="0.0.0.0",            # required on Render
        port=int(os.environ.get("PORT", 8000)),  # Render injects PORT
    )
