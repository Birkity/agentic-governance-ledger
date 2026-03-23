from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.demo import generate_narr05_artifacts


async def main() -> None:
    result = await generate_narr05_artifacts()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
