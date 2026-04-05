#!/usr/bin/env python3
"""
Build zip_to_zone.json from Census ZCTA gazetteer + haversine miles from origin ZCTA.

Zone bands (miles, inclusive upper bound except last):
  1: 0-50, 2: 51-150, 3: 151-300, 4: 301-600, 5: 601-1000,
  6: 1001-1400, 7: 1401-1800, 8: 1801+

Usage (from repo root or clc-api):
  python scripts/build_zip_zone_lookup.py \\
    --gazetteer ../2025_Gaz_zcta_national.txt \\
    --origin 21401 \\
    --output data/zip_to_zone.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

EARTH_RADIUS_MI = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlamb / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(min(1.0, a)))


def miles_to_zone(miles: float) -> int:
    if miles <= 50:
        return 1
    if miles <= 150:
        return 2
    if miles <= 300:
        return 3
    if miles <= 600:
        return 4
    if miles <= 1000:
        return 5
    if miles <= 1400:
        return 6
    if miles <= 1800:
        return 7
    return 8


def load_gazetteer(path: Path) -> dict[str, tuple[float, float]]:
    """GEOID (5-digit ZCTA) -> (lat, lon)."""
    out: dict[str, tuple[float, float]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            z = (row.get("GEOID") or "").strip()
            if not z or len(z) > 5:
                continue
            z = z.zfill(5)
            try:
                lat = float(row["INTPTLAT"])
                lon = float(row["INTPTLONG"])
            except (KeyError, ValueError):
                continue
            out[z] = (lat, lon)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ZIP/ZCTA -> shipping zone JSON from Census gazetteer.")
    parser.add_argument(
        "--gazetteer",
        type=Path,
        required=True,
        help="Path to *_Gaz_zcta_national.txt (pipe-delimited, GEOID|...|INTPTLAT|INTPTLONG)",
    )
    parser.add_argument("--origin", required=True, help="Origin ZCTA, e.g. 21401")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "zip_to_zone.json",
        help="Output JSON path (default: clc-api/data/zip_to_zone.json)",
    )
    args = parser.parse_args()

    origin = args.origin.strip().zfill(5)
    coords = load_gazetteer(args.gazetteer)
    if origin not in coords:
        print(f"Origin ZCTA {origin} not found in gazetteer.", file=sys.stderr)
        return 1

    olat, olon = coords[origin]
    lookup: dict[str, int] = {}
    for z, (lat, lon) in coords.items():
        miles = haversine_miles(olat, olon, lat, lon)
        lookup[z] = miles_to_zone(miles)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(
            {"origin_zcta": origin, "zones": lookup},
            f,
            separators=(",", ":"),
        )

    print(f"Wrote {len(lookup)} ZCTAs -> zone to {args.output}")
    print(f"Origin {origin} -> zone {lookup[origin]} (distance 0 mi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
