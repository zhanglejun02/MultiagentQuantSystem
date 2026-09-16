"""Generate deterministic fixture market data under var/fixtures/cn_a."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_system.fixtures import generate_fixture_data  # noqa: E402


def main() -> None:
    dest = Path(__file__).resolve().parents[1] / "var" / "fixtures" / "cn_a"
    path = generate_fixture_data(str(dest), seed=42)
    print(f"fixture data written to {path}")


if __name__ == "__main__":
    main()
