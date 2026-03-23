from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.demo import build_api_cost_report


async def main() -> None:
    report = await build_api_cost_report()
    output_path = Path("artifacts") / "api_cost_report.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(str(output_path))


if __name__ == "__main__":
    asyncio.run(main())
