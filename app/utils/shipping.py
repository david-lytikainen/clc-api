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

def normalize_us_zip(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if US_ZIP_RE.match(s):
        return s[:5]
    return None


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
