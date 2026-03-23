from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.regulatory import verify_regulatory_package


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python tests/phase6/verify_package.py <package.json>")
        return 2

    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    package = payload.get("package", payload)
    verification = verify_regulatory_package(package)
    print(
        json.dumps(
            {
                "package_hash_valid": verification.package_hash_valid,
                "audit_chain_valid": verification.audit_chain_valid,
                "latest_integrity_hash_matches": verification.latest_integrity_hash_matches,
                "tamper_detected": verification.tamper_detected,
                "ok": verification.ok,
            },
            indent=2,
        )
    )
    return 0 if verification.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
