#!/usr/bin/env python3
from __future__ import annotations

import argparse, csv, hashlib, json, math, re, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://licensing.prf.org"
PRODUCTS = f"{BASE}/products"
SEARCH = f"{BASE}/client/products/search"
ROBOTS = f"{BASE}/robots.txt"
PAGE_SIZE = 300
UA = "Arns-Innovations-Purdue-Public-Catalog-Harvest/1.1 (public metadata; source receipts retained)"
COLUMNS = ["url", "name", "shortDescription", "licencesCount", "groups", "uid1", "imageThumbnailUrl"]


def clean(value):
    return " ".join(str(value or "").split())


def slug(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


def get(session, url, attempts=4, timeout=60):
    error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            error = exc
            if attempt < attempts:
                time.sleep(min(10, attempt * 1.5))
    raise error


def search_url(page):
    params = [("page", str(page)), ("itemsPerPage", str(PAGE_SIZE))]
    params += [("columns[]", column) for column in COLUMNS]
    return f"{SEARCH}?{urlencode(params)}"


def parse_detail(text, url):
    soup = BeautifulSoup(text, "lxml")
    main = soup.find("main") or soup.body or soup
    page_text = clean(main.get_text(" ", strip=True))
    h1 = main.find("h1")
    title = clean(h1.get_text(" ", strip=True) if h1 else "")
    number = re.search(r"Technology\s+No\.\s*([A-Za-z0-9._-]+)", page_text, re.I)
    trl = re.search(r"\bTRL\s*:?\s*([0-9]+(?:\s*[-–]\s*[0-9]+)?)", page_text, re.I)
    documents = []
    for link in main.find_all("a", href=True):
        href = urljoin(url, link.get("href", ""))
        label = clean(link.get_text(" ", strip=True))
        if re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip)(?:\?|$)", href, re.I):
            documents.append(f"{label or slug(href)} | {href}")
    return {
        "detail_title": title,
        "detail_technology_number": clean(number.group(1)) if number else "",
        "detail_text": page_text,
        "trl": clean(trl.group(1)) if trl else "",
        "supporting_documents": " | ".join(dict.fromkeys(documents)),
    }


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="purdue-prf-harvest")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()

    out = Path(args.output_dir)
    raw_api, raw_details = out / "raw_api_pages", out / "raw_detail_pages"
    raw_api.mkdir(parents=True, exist_ok=True)
    raw_details.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html,application/json", "Referer": PRODUCTS})
    robots_response = get(session, ROBOTS, args.attempts, args.timeout)
    robots = RobotFileParser()
    robots.parse(robots_response.text.splitlines())
    if not robots.can_fetch(UA, PRODUCTS) or not robots.can_fetch(UA, f"{BASE}/product/example"):
        raise RuntimeError("Purdue robots.txt does not permit this public harvest")
    (out / "robots.txt").write_bytes(robots_response.content)

    api_rows, api_receipts = [], []
    first = get(session, search_url(1), args.attempts, args.timeout)
    first_payload = first.json()
    official_total, pages = int(first_payload["total"]), int(first_payload["pages"])
    assert pages == math.ceil(official_total / PAGE_SIZE)

    row_number = 0
    for page in range(1, pages + 1):
        response = first if page == 1 else get(session, search_url(page), args.attempts, args.timeout)
        raw = response.content
        (raw_api / f"page_{page:03d}.json").write_bytes(raw)
        payload = response.json()
        items = payload["items"]
        expected = PAGE_SIZE if page < pages else official_total - PAGE_SIZE * (pages - 1)
        if len(items) != expected:
            raise ValueError(f"API page {page} has {len(items)} rows; expected {expected}")
        api_receipts.append({
            "receipt_type": "product_search_api", "source_index": page,
            "url": search_url(page), "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""), "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "parsed_records": len(items),
            "retrieved_at_utc": retrieved,
        })
        for row_on_page, item in enumerate(items, start=1):
            row_number += 1
            url = urljoin(BASE, clean(item.get("url")))
            uid = clean(item.get("uid1"))
            url_slug = slug(url)
            if "/product/" not in urlparse(url).path:
                raise ValueError(f"Invalid product URL {url}")
            listing_id = f"{uid}|{url_slug}" if uid else f"row-{row_number}|{url_slug}"
            api_rows.append({
                "api_row_number": row_number, "source_api_page": page,
                "source_api_row_on_page": row_on_page, "source_listing_id": listing_id,
                "source_record_id": uid, "product_url_slug": url_slug,
                "title": clean(item.get("name")),
                "short_description": clean(item.get("shortDescription")),
                "licences_count": int(item.get("licencesCount") or 0),
                "groups": item.get("groups") if isinstance(item.get("groups"), list) else [],
                "image_url": urljoin(BASE, clean(item.get("imageThumbnailUrl"))),
                "official_record_url": url,
            })
        print(f"api_page={page} rows={len(items)} total={len(api_rows)}", flush=True)

    if len(api_rows) != official_total:
        raise ValueError("API total did not reconcile")
    identities = Counter(row["source_listing_id"].lower() for row in api_rows)
    if any(count > 1 for count in identities.values()):
        raise ValueError("Duplicate listing identities remain")

    rows_by_url = defaultdict(list)
    for row in api_rows:
        rows_by_url[row["official_record_url"]].append(row)
    unique_urls = sorted(rows_by_url)
    shared_groups = {url: rows for url, rows in rows_by_url.items() if len(rows) > 1}

    details, errors = {}, []
    def detail_job(url):
        local = requests.Session(); local.headers.update(session.headers)
        response = get(local, url, args.attempts, args.timeout)
        return url, response, parse_detail(response.text, url)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(detail_job, url): url for url in unique_urls}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                detail_url, response, parsed = future.result()
                details[detail_url] = (response, parsed)
            except Exception as exc:
                errors.append({"stage": "detail_fetch", "url": url, "error": repr(exc)})
            if completed % 100 == 0 or completed == len(unique_urls):
                print(f"detail_completed={completed} success={len(details)} errors={len(errors)}", flush=True)

    detail_receipts = []
    for index, url in enumerate(unique_urls, start=1):
        if url not in details:
            continue
        response, _ = details[url]
        raw = response.content
        (raw_details / f"{index:04d}_{slug(url)}.html").write_bytes(raw)
        detail_receipts.append({
            "receipt_type": "product_detail_html", "source_index": index, "url": url,
            "http_status": response.status_code, "content_type": response.headers.get("content-type", ""),
            "raw_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "parsed_records": 1, "retrieved_at_utc": retrieved,
        })

    records = []
    for row in api_rows:
        if row["official_record_url"] not in details:
            continue
        response, detail = details[row["official_record_url"]]
        groups = row["groups"]
        categories = [clean(group.get("name")) for group in groups if isinstance(group, dict)]
        category_slugs = [clean(group.get("slug")) for group in groups if isinstance(group, dict)]
        group_size = len(rows_by_url[row["official_record_url"]])
        records.append({
            "institution": "Purdue University",
            "catalog": "Purdue Research Foundation Office of Technology Commercialization Online Licensing Store",
            **{key: value for key, value in row.items() if key != "groups"},
            "detail_title": detail["detail_title"],
            "detail_technology_number": detail["detail_technology_number"],
            "detail_text": detail["detail_text"], "categories": " / ".join(categories),
            "category_slugs": " / ".join(category_slugs), "trl": detail["trl"],
            "supporting_documents": detail["supporting_documents"],
            "shared_url_group_size": group_size,
            "shared_url_flag": "SHARED SOURCE URL" if group_size > 1 else "UNIQUE SOURCE URL",
            "detail_fetch_status": response.status_code, "retrieved_at_utc": retrieved,
            "dedup_key": f"purdue|{row['source_listing_id'].lower()}",
        })

    duplicate_review = []
    for url, group in sorted(shared_groups.items()):
        for row in group:
            duplicate_review.append({
                "official_record_url": url, "group_size": len(group),
                "api_row_number": row["api_row_number"], "source_listing_id": row["source_listing_id"],
                "source_record_id": row["source_record_id"], "title": row["title"],
                "short_description": row["short_description"],
                "resolution": "PRESERVE DISTINCT API LISTING; SHARED DETAIL URL DOCUMENTED",
            })

    required_missing = sum(1 for row in records if not row["source_listing_id"] or not row["title"] or not row["official_record_url"])
    source_complete = (
        len(records) == official_total and len(identities) == official_total
        and len(api_receipts) == pages and len(detail_receipts) == len(unique_urls)
        and len(details) == len(unique_urls) and not errors and required_missing == 0
    )

    record_fields = list(records[0].keys())
    write_csv(out / "purdue_prf_records.csv", records, record_fields)
    api_ledger = [{**row, "groups": json.dumps(row["groups"], ensure_ascii=False)} for row in api_rows]
    write_csv(out / "purdue_prf_api_row_ledger.csv", api_ledger, list(api_ledger[0].keys()))
    write_csv(out / "purdue_prf_receipts.csv", api_receipts + detail_receipts, list(api_receipts[0].keys()))
    write_csv(out / "purdue_prf_duplicate_url_review.csv", duplicate_review,
              ["official_record_url", "group_size", "api_row_number", "source_listing_id", "source_record_id", "title", "short_description", "resolution"])
    write_csv(out / "purdue_prf_errors.csv", errors, ["stage", "url", "error"])
    (out / "purdue_prf_records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "purdue_prf_api_row_ledger.json").write_text(json.dumps(api_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "institution": "Purdue University",
        "source_organization": "Purdue Research Foundation Office of Technology Commercialization",
        "catalog_url": PRODUCTS, "public_search_endpoint": SEARCH,
        "retrieved_at_utc": retrieved, "official_api_row_total": official_total,
        "api_page_size": PAGE_SIZE, "api_pages_expected": pages,
        "api_pages_with_receipts": len(api_receipts), "normalized_listing_records": len(records),
        "unique_listing_identities": len(identities), "unique_official_record_urls": len(unique_urls),
        "shared_url_groups": len(shared_groups),
        "shared_url_api_rows": sum(len(group) for group in shared_groups.values()),
        "detail_pages_with_receipts": len(detail_receipts), "required_fields_missing": required_missing,
        "errors": len(errors), "robots_allowed": True, "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "reconciliation_note": "API listing rows are authoritative. Distinct technology numbers sharing one URL are preserved separately, while the shared detail page is fetched once and documented.",
    }
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    (out / "purdue_prf_source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)
    if not source_complete:
        print("PURDUE SOURCE INCOMPLETE", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
