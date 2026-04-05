from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import has_app_context, current_app

logger = logging.getLogger(__name__)

US_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

ORANGE_CENTS = 680
COMBO_LARGER_CENTS = 995


def normalize_us_zip(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if US_ZIP_RE.match(s):
        return s[:5]
    return None


def zip_passes_regex(raw: str | None) -> bool:
    return bool(raw and isinstance(raw, str) and US_ZIP_RE.match(raw.strip()))


def _default_json_path() -> Path:
    if has_app_context():
        return Path(current_app.root_path).resolve().parent / "data" / "zip_to_zone.json"
    return Path(__file__).resolve().parent.parent.parent / "data" / "zip_to_zone.json"


@lru_cache(maxsize=1)
def _load_zone_data(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file():
        logger.error("zip_to_zone.json not found at %s", path)
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def zone_for_zip(normalized_zip: str) -> int | None:
    env_path = os.environ.get("ZIP_TO_ZONE_JSON")
    path = Path(env_path) if env_path else _default_json_path()
    data = _load_zone_data(str(path.resolve()))
    zones = data.get("zones") or {}
    z = zones.get(normalized_zip)
    if z is None:
        z = zones.get(normalized_zip.zfill(5))
    try:
        return int(z) if z is not None else None
    except (TypeError, ValueError):
        return None


def infer_shipping_tier_from_title(title: str | None) -> str:
    t = (title or "").lower()
    for kw in ("keychain", "cardholder", "card holder", "charmer"):
        if kw in t:
            return "orange"
    for kw in ("valet", "poet", "socialite", "athlete"):
        if kw in t:
            return "pink"
    if "lifer" in t:
        return "green"
    if "reader" in t:
        return "blue"
    return "pink"


def tier_rate_cents(tier: str, zone: int) -> int:
    zhi = zone >= 5
    if tier == "orange":
        return ORANGE_CENTS
    if tier == "pink":
        return 995 if zhi else 795
    if tier == "green":
        return 995 if zhi else 840
    if tier == "blue":
        return 995 if zhi else 922
    return 995 if zhi else 795


def shipping_cents_for_lines(lines: list[tuple[str, int]], zone: int) -> int:
    """
    lines: (tier, quantity) per cart row.
    Rules: orange-only any qty → 680; orange + larger → max(680, larger rules);
    2+ larger units → 995; 1 larger only → table by tier/zone.
    """
    orange_qty = sum(q for t, q in lines if t == "orange")
    larger = [(t, q) for t, q in lines if t in ("pink", "green", "blue")]
    total_larger_qty = sum(q for _, q in larger)

    if orange_qty > 0 and total_larger_qty == 0:
        return ORANGE_CENTS
    if orange_qty > 0 and total_larger_qty > 0:
        if total_larger_qty >= 2:
            return max(ORANGE_CENTS, COMBO_LARGER_CENTS)
        t_one = next((t for t, q in larger if q > 0), "pink")
        return max(ORANGE_CENTS, tier_rate_cents(t_one, zone))
    if total_larger_qty == 0:
        return 0
    if total_larger_qty >= 2:
        return COMBO_LARGER_CENTS
    t_one = next((t for t, q in larger if q > 0), "pink")
    return tier_rate_cents(t_one, zone)
