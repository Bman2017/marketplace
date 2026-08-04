#!/usr/bin/env python3
"""Probe Purdue's public product-search endpoint and preserve exact response metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = "https://licensing.prf.org"
ENDPOINT = f"{BASE}/client/products/search"
USER_AGENT = (
    "Arns-Innovations-Purdue-Public-Catalog-Probe/1.0 "
    "(public metadata; source receipts retained)"
)
COLUMNS = [
    "url",
    "name",
    "shortDescription",
    "licencesCount",
    "groups",
    "uid1",
    "imageThumbnailUrl",
]


def main() -> int:
    out = Path("purdue-api-probe")
    out.mkdir(parents=True, exist_ok=True)

    params: list[tuple[str, str]] = [
        ("page", "1"),
        ("itemsPerPage", "300"),
    ]
    params.extend(("columns[]", column) for column in COLUMNS)
    url = f"{ENDPOINT}?{urlencode(params)}"

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": f"{BASE}/products",
    })
    response = session.get(url, timeout=90)
    raw = response.content
    (out / "page_001.json").write_bytes(raw)

    report = {
        "url": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "headers": dict(response.headers),
    }
    try:
        payload = response.json()
        report["payload_type"] = type(payload).__name__
        if isinstance(payload, dict):
            report["top_level_keys"] = sorted(payload.keys())
            report["scalar_fields"] = {
                key: value
                for key, value in payload.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
            items = payload.get("items")
            report["item_count"] = len(items) if isinstance(items, list) else None
            report["first_item"] = items[0] if isinstance(items, list) and items else None
            report["last_item"] = items[-1] if isinstance(items, list) and items else None
        else:
            report["payload_preview"] = payload[:3] if isinstance(payload, list) else payload
    except Exception as exc:
        report["json_error"] = repr(exc)
        report["text_preview"] = response.text[:5000]

    (out / "purdue_api_probe_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
