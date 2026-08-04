#!/usr/bin/env python3
"""Source-complete public Purdue Research Foundation licensing catalog harvest.

The script uses Purdue's public e-lucid product-search endpoint to enumerate the
live catalog, then retrieves every public product detail page. It preserves raw
API and HTML evidence, SHA-256 receipts, normalized CSV/JSON records, and fails
closed unless every live listing is represented exactly once.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://licensing.prf.org"
PRODUCTS_URL = f"{BASE}/products"
SEARCH_ENDPOINT = f"{BASE}/client/products/search"
ROBOTS_URL = f"{BASE}/robots.txt"
ITEMS_PER_PAGE = 300
USER_AGENT = (
    "Arns-Innovations-Purdue-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)
SEARCH_COLUMNS = [
    "url",
    "name",
    "shortDescription",
    "licencesCount",
    "groups",
    "uid1",
    "imageThumbnailUrl",
]


@dataclass
class PurdueRecord:
    institution: str
    catalog: str
    source_listing_id: str
    source_record_id: str
    title: str
    short_description: str
    detail_text: str
    authors: str
    categories: str
    category_slugs: str
    licences_count: int
    trl: str
    intellectual_property: str
    keywords: str
    supporting_documents: str
    image_url: str
    official_record_url: str
    source_api_page: int
    retrieved_at_utc: str
    title_source: str
    detail_fetch_status: int
    dedup_key: str


@dataclass
class Receipt:
    receipt_type: str
    source_index: int
    url: str
    http_status: int
    content_type: str
    raw_bytes: int
    sha256: str
    parsed_records: int
    retrieved_at_utc: str


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def safe_slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


def fetch(
    session: requests.Session,
    url: str,
    attempts: int,
    timeout: float,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(10.0, attempt * 1.5))
    assert last_error is not None
    raise last_error


def verify_robots(session: requests.Session, timeout: float) -> dict[str, Any]:
    response = session.get(ROBOTS_URL, timeout=timeout)
    response.raise_for_status()
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines())
    allowed_products = parser.can_fetch(USER_AGENT, PRODUCTS_URL)
    allowed_example = parser.can_fetch(USER_AGENT, f"{BASE}/product/example")
    if not allowed_products or not allowed_example:
        raise RuntimeError("robots.txt does not permit public product harvesting")
    return {
        "url": ROBOTS_URL,
        "status": response.status_code,
        "text": response.text,
        "allowed_products": allowed_products,
        "allowed_product_details": allowed_example,
        "sha256": hashlib.sha256(response.content).hexdigest(),
    }


def search_url(page: int) -> str:
    params: list[tuple[str, str]] = [
        ("page", str(page)),
        ("itemsPerPage", str(ITEMS_PER_PAGE)),
    ]
    params.extend(("columns[]", column) for column in SEARCH_COLUMNS)
    return f"{SEARCH_ENDPOINT}?{urlencode(params)}"


def section_after(text: str, label: str, stop_labels: list[str]) -> str:
    match = re.search(rf"\b{re.escape(label)}\s*:?\s*", text, flags=re.I)
    if not match:
        return ""
    tail = text[match.end():]
    ends = []
    for stop in stop_labels:
        stop_match = re.search(rf"\b{re.escape(stop)}\s*:?\s*", tail, flags=re.I)
        if stop_match:
            ends.append(stop_match.start())
    if ends:
        tail = tail[: min(ends)]
    return clean(tail)


def extract_named_list(soup: BeautifulSoup, label_pattern: str) -> list[str]:
    values: list[str] = []
    heading = soup.find(string=re.compile(label_pattern, flags=re.I))
    if not heading:
        return values
    parent = heading.parent if isinstance(heading.parent, Tag) else None
    container = parent.find_parent(["li", "section", "div"]) if parent else None
    if container is None:
        return values
    # Prefer list-like descendants but avoid UI labels and action controls.
    for element in container.find_all(["a", "p", "li", "span"]):
        value = clean(element.get_text(" ", strip=True))
        if not value:
            continue
        if re.search(label_pattern, value, flags=re.I):
            continue
        if value.lower() in {
            "download",
            "product brochure",
            "contact us",
            "license now",
            "preview terms",
            "expand_more",
            "mode_edit",
            "cloud_download",
        }:
            continue
        if len(value) <= 180:
            values.append(value)
    return list(dict.fromkeys(values))


def parse_detail(html_text: str, api_item: dict[str, Any], official_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find("main") or soup.body or soup
    page_text = clean(main.get_text(" ", strip=True))

    h1 = main.find("h1")
    detail_title = clean(h1.get_text(" ", strip=True) if h1 else "")
    api_title = clean(str(api_item.get("name") or ""))
    title = detail_title or api_title
    title_source = "Official detail page" if detail_title else "Public search API"

    technology_no_match = re.search(
        r"Technology\s+No\.\s*([^\n\r]+?)(?=\s{2,}|Researchers|Purdue|Advantages|Applications|Technology Validation|TRL|Intellectual Property|Keywords|Authors|References|Supporting documents|$)",
        page_text,
        flags=re.I,
    )
    source_record_id = clean(str(api_item.get("uid1") or ""))
    if not source_record_id and technology_no_match:
        source_record_id = clean(technology_no_match.group(1))

    short_description = clean(str(api_item.get("shortDescription") or ""))
    if not short_description and h1:
        candidate = h1.find_next(["h5", "h6", "p"])
        if candidate:
            short_description = clean(candidate.get_text(" ", strip=True))

    groups = api_item.get("groups") if isinstance(api_item.get("groups"), list) else []
    category_names = [clean(str(group.get("name") or "")) for group in groups if isinstance(group, dict)]
    category_slugs = [clean(str(group.get("slug") or "")) for group in groups if isinstance(group, dict)]

    # Author names are published in a collapsible section. This extraction is
    # intentionally permissive; completion does not depend on optional authors.
    authors = extract_named_list(soup, r"^\s*Authors?(?:\s*\(\d+\))?\s*$")
    authors = [
        value
        for value in authors
        if not value.lower().startswith("professor's website")
        and not re.search(r"\bwebsite\b", value, flags=re.I)
    ]

    supporting_documents: list[str] = []
    for link in main.find_all("a", href=True):
        href = urljoin(official_url, link.get("href", ""))
        text = clean(link.get_text(" ", strip=True))
        if re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip)(?:\?|$)", href, flags=re.I):
            supporting_documents.append(f"{text or safe_slug_from_url(href)} | {href}")
        elif text and re.search(r"product brochure|supporting document|download", text, flags=re.I):
            if urlparse(href).netloc:
                supporting_documents.append(f"{text} | {href}")
    supporting_documents = list(dict.fromkeys(supporting_documents))

    trl_match = re.search(r"\bTRL\s*:?\s*([0-9]+(?:\s*[-–]\s*[0-9]+)?)", page_text, flags=re.I)
    trl = clean(trl_match.group(1)) if trl_match else ""
    intellectual_property = section_after(
        page_text,
        "Intellectual Property",
        ["Keywords", "Authors", "References", "Supporting documents", "Questions about this technology"],
    )
    keywords = section_after(
        page_text,
        "Keywords",
        ["Authors", "References", "Supporting documents", "Questions about this technology"],
    )

    # Remove global chrome from the retained detail text where possible.
    for boilerplate in [
        "Skip to main content",
        "Account & Support",
        "Technology Transfer",
        "Questions about this technology?",
        "Contact us",
    ]:
        page_text = page_text.replace(boilerplate, " ")
    page_text = clean(page_text)

    return {
        "title": title,
        "title_source": title_source,
        "source_record_id": source_record_id,
        "short_description": short_description,
        "detail_text": page_text,
        "authors": " / ".join(authors),
        "categories": " / ".join(value for value in category_names if value),
        "category_slugs": " / ".join(value for value in category_slugs if value),
        "trl": trl,
        "intellectual_property": intellectual_property,
        "keywords": keywords,
        "supporting_documents": " | ".join(supporting_documents),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="purdue-prf-harvest")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    output = Path(args.output_dir)
    raw_api = output / "raw_api_pages"
    raw_details = output / "raw_detail_pages"
    output.mkdir(parents=True, exist_ok=True)
    raw_api.mkdir(parents=True, exist_ok=True)
    raw_details.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": PRODUCTS_URL,
    })
    robots = verify_robots(session, args.timeout)
    (output / "robots.txt").write_text(robots["text"], encoding="utf-8")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    api_receipts: list[Receipt] = []
    errors: list[dict[str, Any]] = []

    first_response = fetch(session, search_url(1), args.attempts, args.timeout)
    first_payload = first_response.json()
    official_total = int(first_payload["total"])
    expected_pages = int(first_payload["pages"])
    if expected_pages != math.ceil(official_total / ITEMS_PER_PAGE):
        raise ValueError(
            f"Purdue API page disagreement: API pages={expected_pages}, calculated={math.ceil(official_total / ITEMS_PER_PAGE)}"
        )

    api_items_by_url: dict[str, dict[str, Any]] = {}
    for page in range(1, expected_pages + 1):
        url = search_url(page)
        response = first_response if page == 1 else fetch(session, url, args.attempts, args.timeout)
        raw = response.content
        raw_path = raw_api / f"page_{page:03d}.json"
        raw_path.write_bytes(raw)
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError(f"Purdue API page {page} does not contain an items list")
        expected_count = (
            ITEMS_PER_PAGE
            if page < expected_pages
            else official_total - ITEMS_PER_PAGE * (expected_pages - 1)
        )
        if len(items) != expected_count:
            raise ValueError(
                f"Purdue API page {page} has {len(items)} items; expected {expected_count}"
            )
        api_receipts.append(
            Receipt(
                receipt_type="product_search_api",
                source_index=page,
                url=url,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                raw_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                parsed_records=len(items),
                retrieved_at_utc=retrieved_at,
            )
        )
        for item in items:
            relative_url = clean(str(item.get("url") or ""))
            official_url = urljoin(BASE, relative_url)
            if not official_url or "/product/" not in urlparse(official_url).path:
                raise ValueError(f"Invalid Purdue product URL on API page {page}: {relative_url!r}")
            if official_url in api_items_by_url:
                raise ValueError(f"Duplicate Purdue product URL in API: {official_url}")
            item_copy = dict(item)
            item_copy["source_api_page"] = page
            item_copy["official_url"] = official_url
            api_items_by_url[official_url] = item_copy
        print(f"api_page={page} items={len(items)} unique_total={len(api_items_by_url)}", flush=True)

    if len(api_items_by_url) != official_total:
        raise ValueError(
            f"Purdue API unique URL count {len(api_items_by_url)} does not equal official total {official_total}"
        )

    # Fetch all detail pages concurrently with a bounded worker pool.
    def fetch_detail_job(pair: tuple[int, tuple[str, dict[str, Any]]]) -> tuple[int, str, dict[str, Any], requests.Response]:
        index, (official_url, item) = pair
        local_session = requests.Session()
        local_session.headers.update(session.headers)
        response = fetch(local_session, official_url, args.attempts, args.timeout)
        return index, official_url, item, response

    indexed_items = list(enumerate(sorted(api_items_by_url.items()), start=1))
    detail_results: dict[int, tuple[str, dict[str, Any], requests.Response]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_detail_job, pair): pair[0]
            for pair in indexed_items
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            try:
                result_index, official_url, item, response = future.result()
                detail_results[result_index] = (official_url, item, response)
            except Exception as exc:
                errors.append({
                    "stage": "detail_fetch",
                    "source_index": index,
                    "url": indexed_items[index - 1][1][0],
                    "error": repr(exc),
                })
            if completed % 100 == 0 or completed == len(indexed_items):
                print(
                    f"detail_completed={completed} detail_success={len(detail_results)} detail_errors={len(errors)}",
                    flush=True,
                )

    records: list[PurdueRecord] = []
    detail_receipts: list[Receipt] = []
    for index, (official_url, item) in [
        (idx, detail_results[idx]) for idx in sorted(detail_results)
    ]:
        response = item[1] if False else None
        # Unpack after the intentionally explicit ordering above.
        official_url, api_item, detail_response = detail_results[index]
        raw = detail_response.content
        slug = safe_slug_from_url(official_url)
        raw_path = raw_details / f"{index:04d}_{slug}.html"
        raw_path.write_bytes(raw)
        parsed = parse_detail(detail_response.text, api_item, official_url)
        source_listing_id = slug or hashlib.sha256(official_url.encode("utf-8")).hexdigest()[:24]
        source_record_id = parsed["source_record_id"]
        dedup_key = f"purdue|{source_listing_id.lower()}"
        record = PurdueRecord(
            institution="Purdue University",
            catalog="Purdue Research Foundation Office of Technology Commercialization Online Licensing Store",
            source_listing_id=source_listing_id,
            source_record_id=source_record_id,
            title=parsed["title"],
            short_description=parsed["short_description"],
            detail_text=parsed["detail_text"],
            authors=parsed["authors"],
            categories=parsed["categories"],
            category_slugs=parsed["category_slugs"],
            licences_count=int(api_item.get("licencesCount") or 0),
            trl=parsed["trl"],
            intellectual_property=parsed["intellectual_property"],
            keywords=parsed["keywords"],
            supporting_documents=parsed["supporting_documents"],
            image_url=urljoin(BASE, clean(str(api_item.get("imageThumbnailUrl") or ""))),
            official_record_url=official_url,
            source_api_page=int(api_item["source_api_page"]),
            retrieved_at_utc=retrieved_at,
            title_source=parsed["title_source"],
            detail_fetch_status=detail_response.status_code,
            dedup_key=dedup_key,
        )
        records.append(record)
        detail_receipts.append(
            Receipt(
                receipt_type="product_detail_html",
                source_index=index,
                url=official_url,
                http_status=detail_response.status_code,
                content_type=detail_response.headers.get("content-type", ""),
                raw_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                parsed_records=1,
                retrieved_at_utc=retrieved_at,
            )
        )

    record_rows = [asdict(record) for record in records]
    receipt_rows = [asdict(receipt) for receipt in api_receipts + detail_receipts]

    unique_urls = {row["official_record_url"] for row in record_rows}
    unique_listing_ids = {row["source_listing_id"] for row in record_rows}
    nonblank_source_ids = [row["source_record_id"] for row in record_rows if row["source_record_id"]]
    duplicate_source_ids = {
        source_id: sum(1 for row in record_rows if row["source_record_id"] == source_id)
        for source_id in sorted(set(nonblank_source_ids))
        if sum(1 for row in record_rows if row["source_record_id"] == source_id) > 1
    }
    required_missing = sum(
        1
        for row in record_rows
        if not row["source_listing_id"]
        or not row["title"]
        or not row["official_record_url"]
    )
    blank_source_record_ids = sum(1 for row in record_rows if not row["source_record_id"])

    write_csv(
        output / "purdue_prf_records.csv",
        record_rows,
        list(PurdueRecord.__dataclass_fields__),
    )
    write_csv(
        output / "purdue_prf_receipts.csv",
        receipt_rows,
        list(Receipt.__dataclass_fields__),
    )
    write_csv(
        output / "purdue_prf_errors.csv",
        errors,
        ["stage", "source_index", "url", "error"],
    )
    (output / "purdue_prf_records.json").write_text(
        json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "purdue_prf_receipts.json").write_text(
        json.dumps(receipt_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "purdue_prf_duplicate_source_ids.json").write_text(
        json.dumps(duplicate_source_ids, indent=2), encoding="utf-8"
    )

    source_complete = (
        len(record_rows) == official_total
        and len(unique_urls) == official_total
        and len(unique_listing_ids) == official_total
        and len(api_receipts) == expected_pages
        and len(detail_receipts) == official_total
        and not errors
        and required_missing == 0
    )
    manifest = {
        "institution": "Purdue University",
        "source_organization": "Purdue Research Foundation Office of Technology Commercialization",
        "catalog_url": PRODUCTS_URL,
        "public_search_endpoint": SEARCH_ENDPOINT,
        "retrieved_at_utc": retrieved_at,
        "official_live_total": official_total,
        "api_page_size": ITEMS_PER_PAGE,
        "api_pages_expected": expected_pages,
        "api_pages_with_receipts": len(api_receipts),
        "detail_pages_with_receipts": len(detail_receipts),
        "unique_listing_identities": len(unique_listing_ids),
        "unique_official_record_urls": len(unique_urls),
        "nonblank_technology_numbers": len(nonblank_source_ids),
        "blank_technology_numbers": blank_source_record_ids,
        "duplicate_technology_number_groups": len(duplicate_source_ids),
        "required_fields_missing": required_missing,
        "errors": len(errors),
        "robots": robots,
        "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (output / "purdue_prf_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)

    if not source_complete:
        print("PURDUE SOURCE INCOMPLETE: aggregate admission is not authorized.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
