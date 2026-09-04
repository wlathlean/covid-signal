#!/usr/bin/env python3
"""Fail closed when a generated public dataset is structurally unsafe to publish."""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "public" / "data" / "tracker.json"


def main() -> None:
    data = json.loads(PAYLOAD.read_text())
    generated = datetime.fromisoformat(data["generated_at"])
    age = datetime.now(timezone.utc) - generated
    if age.total_seconds() > 6 * 60 * 60:
        raise SystemExit("Generated dataset timestamp is unexpectedly old")
    if set(data.get("states", {})) != {"WA", "TX"}:
        raise SystemExit("Expected exactly WA and TX")
    for code, state in data["states"].items():
        for required in ("wastewater", "emergency", "hospital"):
            metric = state.get("metrics", {}).get(required, {})
            if not metric.get("available"):
                raise SystemExit(f"{code} {required} is unavailable")
            if metric.get("age_days", 999) > 21:
                raise SystemExit(f"{code} {required} is too old to publish")
        if state["metrics"]["wastewater"].get("reporting_percent", 0) < 75:
            raise SystemExit(f"{code} wastewater reporting is incomplete")
    print("Public dataset is valid")


if __name__ == "__main__":
    main()
