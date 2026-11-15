#!/usr/bin/env python3
"""Turn the reversible Harbor biscuit simulation on, off, or check its status."""
import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from storage import BASE

DEMO_FILE = BASE / ".simulated_recall.json"
DEMO_ORIGIN = "https://demo.lotwise.invalid"
DEMO_URL = DEMO_ORIGIN + "/harbor-lemon-oat-biscuits"
PRODUCT = {
    "name_raw": "Harbor Demo Lemon Oat Biscuits",
    "brand_raw": "Harbor Demo",
    "pack_size_raw": "180 g",
    "product_kind": "food",
    "identifiers": [{"type": "ean", "value_raw": "0209000000003", "role": "affected_product"}],
    "scope_basis": "specific_conditions",
    "scope_clauses": [{"all_of": [
        {"field": "lot", "operator": "equals", "values_raw": ["HB-2611"]},
        {"field": "best_before", "operator": "equals", "date_values": [{"year": 2027, "month": 4, "day": 30}]},
    ]}],
    "scope_raw": "This simulated recall covers lot HB-2611 with best-before 30 April 2027.",
    "reason": "The simulated notice says the packaging omits milk from the ingredient list.",
    "action": "For the exercise, check the lot and date printed beside the barcode.",
}


def read_simulation(path):
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))["enabled_at"]
    except FileNotFoundError:
        return None
    return {"url": DEMO_URL, "id": "simulation-" + stamp, "fetched_at": stamp, "refresh_error": None,
            "data": {"products": [PRODUCT]}, "english": {"products": [PRODUCT]}}


def enable(path):
    if path.exists():
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as tmp:
        json.dump({"enabled_at": datetime.now(timezone.utc).isoformat()}, tmp)
    os.replace(tmp.name, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("on", "off", "status"))
    parser.add_argument("--state", type=Path, default=DEMO_FILE)
    args = parser.parse_args()
    if args.command == "on":
        enable(args.state)
    if args.command == "off":
        args.state.unlink(missing_ok=True)
    print("Simulation is " + ("ON (Harbor Demo, lot HB-2611)" if args.state.exists() else "OFF"))


if __name__ == "__main__":
    main()
