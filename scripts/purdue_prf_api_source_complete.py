#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, json, math, re, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

BASE = "https://licensing.prf.org"
PRODUCTS = f"{BASE}/products"
SEARCH = f"{BASE}/client/products/search"
ROBOTS = f"{BASE}/robots.txt"
PAGE_SIZE = 300
UA = "Arns-Innovations-Purdue-Public-Catalog-Harvest/1.2 (public metadata; source receipts retained)"
COLUMNS = ["url", "name", "shortDescription", "licencesCount", "groups", "uid1", "imageThumbnailUrl"]


def clean(value):
    return " ".join(str(value or "").split())


def search_url(page):
    params = [("page", str(page)), ("itemsPerPage", str(PAGE_SIZE))]
    params += [("columns[]", column) for column in COLUMNS]
    return f"{SEARCH}?{urlencode(params)}"


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def main():
    out = Path("purdue-prf-api-harvest")
    raw = out / "raw_api_pages"
    raw.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json", "Referer": PRODUCTS})
    robots_response = session.get(ROBOTS, timeout=60); robots_response.raise_for_status()
    robots = RobotFileParser(); robots.parse(robots_response.text.splitlines())
    if not robots.can_fetch(UA, PRODUCTS) or not robots.can_fetch(UA, SEARCH):
        raise RuntimeError("Purdue robots.txt does not permit the public catalog harvest")
    (out / "robots.txt").write_bytes(robots_response.content)

    first = session.get(search_url(1), timeout=90); first.raise_for_status()
    first_payload = first.json()
    official_total = int(first_payload["total"])
    pages = int(first_payload["pages"])
    if pages != math.ceil(official_total / PAGE_SIZE):
        raise ValueError("Purdue API page count does not reconcile")

    records, receipts = [], []
    row_number = 0
    for page in range(1, pages + 1):
        response = first if page == 1 else session.get(search_url(page), timeout=90)
        response.raise_for_status()
        payload = response.json(); items = payload["items"]
        expected = PAGE_SIZE if page < pages else official_total - PAGE_SIZE * (pages - 1)
        if len(items) != expected:
            raise ValueError(f"Page {page} returned {len(items)} records; expected {expected}")
        raw_bytes = response.content
        (raw / f"page_{page:03d}.json").write_bytes(raw_bytes)
        receipts.append({
            "page": page, "api_url": search_url(page), "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""), "raw_bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(), "parsed_records": len(items),
            "retrieved_at_utc": retrieved,
        })
        for row_on_page, item in enumerate(items, start=1):
            row_number += 1
            url = urljoin(BASE, clean(item.get("url")))
            uid = clean(item.get("uid1"))
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            listing_id = f"{uid}|{slug}" if uid else f"api-row-{row_number}|{slug}"
            groups = item.get("groups") if isinstance(item.get("groups"), list) else []
            categories = [clean(group.get("name")) for group in groups if isinstance(group, dict)]
            category_slugs = [clean(group.get("slug")) for group in groups if isinstance(group, dict)]
            records.append({
                "institution": "Purdue University",
                "source_organization": "Purdue Research Foundation Office of Technology Commercialization",
                "catalog": "Purdue PRF Online Licensing Store",
                "api_row_number": row_number, "source_api_page": page,
                "source_api_row_on_page": row_on_page, "source_listing_id": listing_id,
                "source_record_id": uid, "title": clean(item.get("name")),
                "summary": clean(item.get("shortDescription")),
                "categories": " / ".join(value for value in categories if value),
                "category_slugs": " / ".join(value for value in category_slugs if value),
                "licences_count": int(item.get("licencesCount") or 0),
                "image_url": urljoin(BASE, clean(item.get("imageThumbnailUrl"))),
                "official_record_url": url, "retrieved_at_utc": retrieved,
                "dedup_key": f"purdue|{listing_id.lower()}",
            })
        print(f"page={page} records={len(items)} total={len(records)}", flush=True)

    identities = Counter(row["source_listing_id"].lower() for row in records)
    urls = defaultdict(list)
    for row in records: urls[row["official_record_url"]].append(row)
    shared = {url: rows for url, rows in urls.items() if len(rows) > 1}
    duplicate_identities = {key: count for key, count in identities.items() if count > 1}
    required_missing = sum(1 for row in records if not row["source_listing_id"] or not row["title"] or not row["official_record_url"])

    review = []
    for url, rows in sorted(shared.items()):
        for row in rows:
            review.append({
                "official_record_url": url, "group_size": len(rows),
                "api_row_number": row["api_row_number"], "source_listing_id": row["source_listing_id"],
                "source_record_id": row["source_record_id"], "title": row["title"],
                "summary": row["summary"],
                "resolution": "PRESERVE DISTINCT LISTING; SHARED OFFICIAL URL DOCUMENTED",
            })

    source_complete = (
        len(records) == official_total and len(identities) == official_total
        and len(receipts) == pages and not duplicate_identities and required_missing == 0
    )
    write_csv(out / "purdue_prf_records.csv", records, list(records[0].keys()))
    write_csv(out / "purdue_prf_page_receipts.csv", receipts, list(receipts[0].keys()))
    write_csv(out / "purdue_prf_shared_url_review.csv", review,
              ["official_record_url", "group_size", "api_row_number", "source_listing_id", "source_record_id", "title", "summary", "resolution"])
    (out / "purdue_prf_records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "institution": "Purdue University",
        "source_organization": "Purdue Research Foundation Office of Technology Commercialization",
        "catalog_url": PRODUCTS, "public_search_endpoint": SEARCH,
        "retrieved_at_utc": retrieved, "official_api_row_total": official_total,
        "api_page_size": PAGE_SIZE, "api_pages_expected": pages,
        "api_pages_with_receipts": len(receipts), "normalized_listing_records": len(records),
        "unique_listing_identities": len(identities), "unique_official_record_urls": len(urls),
        "shared_url_groups": len(shared), "shared_url_api_rows": sum(len(rows) for rows in shared.values()),
        "duplicate_listing_identities": duplicate_identities,
        "required_fields_missing": required_missing, "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "scope_note": "This certifies the complete public listing catalog from Purdue's authoritative e-lucid product-search API. Product detail pages are optional enrichment and are not required to establish listing enumeration.",
    }
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    (out / "purdue_prf_source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)
    return 0 if source_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
