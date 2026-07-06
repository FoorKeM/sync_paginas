import asyncio
import sys

from menu import modo_automatico


if __name__ == "__main__":
    if "--auto" not in sys.argv:
        sys.argv.append("--auto")
    asyncio.run(modo_automatico())
