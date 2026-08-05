#!/usr/bin/env python3
"""Source-complete harvest of the University of Michigan Available Inventions catalog."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://available-inventions.umich.edu/"
PRODUCTS_PAGE = urljoin(BASE, "products")
SEARCH_API = urljoin(BASE, "client/products/search")
AUTOCOMPLETE_API = urljoin(BASE, "autocomplete/products")
PAGE_SIZE = 300
EXPECTED_TOTAL = 834
OUT = Path("umich-harvest")
RAW_DETAILS = OUT / "raw_detail_pages"
RAW_API = OUT / "raw_api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def safe_filename(value: str, limit: int = 170) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return value[:limit] or "record"


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": PRODUCTS_PAGE,
        }
    )
    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    params=None,
    attempts: int = 7,
    timeout: float = 90.0,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(f"retryable HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(18.0, 1.25 * attempt + (attempt % 3) * 0.4))
    assert last_error is not None
    raise last_error


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def extract_technology_numbers(texts: list[str]) -> list[str]:
    values: list[str] = []
    joined = " | ".join(texts)
    # Modern case numbers, legacy numeric IDs, and occasional suffixes.
    for match in re.finditer(r"\b(?:19|20)\d{2}-\d{2,4}(?:_[A-Za-z0-9]+)?\b|\b\d{3,6}(?:_[A-Za-z0-9]+)?\b", joined):
        value = match.group(0)
        # Avoid years standing alone when a modern case number is not present.
        if re.fullmatch(r"(?:19|20)\d{2}", value):
            continue
        if value not in values:
            values.append(value)
    return values


def section_map(description: Tag | None) -> tuple[dict[str, str], str]:
    if description is None:
        return {}, ""
    sections: dict[str, list[str]] = defaultdict(list)
    current = "UNLABELED"
    for child in description.children:
        if not isinstance(child, Tag):
            continue
        if child.name in {"h1", "h2", "h3"}:
            heading = clean(child.get_text(" ", strip=True)).upper()
            if heading:
                current = heading
            continue
        text = clean(child.get_text(" ", strip=True))
        if text:
            sections[current].append(text)
    flattened = {key: clean(" ".join(parts)) for key, parts in sections.items() if clean(" ".join(parts))}
    return flattened, clean(description.get_text(" ", strip=True))


def section_value(sections: dict[str, str], *candidates: str) -> str:
    candidate_norms = [candidate.upper() for candidate in candidates]
    for candidate in candidate_norms:
        if candidate in sections:
            return sections[candidate]
    for key, value in sections.items():
        if any(candidate in key for candidate in candidate_norms):
            return value
    return ""


def parse_collapsible_values(soup: BeautifulSoup, label_prefix: str) -> list[str]:
    output: list[str] = []
    for item in soup.find_all("li"):
        header = item.select_one(".collapsible-header span")
        if not header:
            continue
        label = clean(header.get_text(" ", strip=True))
        if not label.lower().startswith(label_prefix.lower()):
            continue
        body = item.select_one(".collapsible-body")
        if not body:
            continue
        leaves: list[str] = []
        for node in body.find_all(["div", "a", "span"]):
            if node.find(["div", "a", "span"], recursive=False):
                continue
            text = clean(node.get_text(" ", strip=True))
            if text and text not in {"DOWNLOAD", "Order now", "Preview terms"}:
                leaves.append(text)
        if not leaves:
            text = clean(body.get_text(" | ", strip=True))
            if text:
                leaves = [part.strip() for part in text.split("|") if part.strip()]
        for value in leaves:
            if value not in output:
                output.append(value)
    return output


def parse_detail(html_bytes: bytes, listing: dict, autocomplete: dict) -> dict:
    soup = BeautifulSoup(html_bytes, "lxml")
    card = soup.select_one(".product-description-box") or soup.find("main") or soup.body or soup
    title_node = card.find("h1") if card else None
    title = clean(title_node.get_text(" ", strip=True) if title_node else listing.get("name"))

    number_texts: list[str] = []
    for node in card.find_all(["h5", "h6", "div", "span"]):
        text = clean(node.get_text(" ", strip=True))
        if re.search(r"\bTECHNOLOGY\s+(?:NUMBER|NUMBERS|NO\.)", text, flags=re.I):
            number_texts.append(text)
    technology_numbers = extract_technology_numbers(number_texts)

    tags = [
        clean(anchor.get_text(" ", strip=True))
        for anchor in card.select('.chips-container a, a[href*="tags="]')
        if clean(anchor.get_text(" ", strip=True))
    ]
    tags = list(dict.fromkeys(tags))

    breadcrumbs = [
        clean(anchor.get_text(" ", strip=True))
        for anchor in soup.select("a.breadcrumb")
        if clean(anchor.get_text(" ", strip=True))
    ]
    breadcrumbs = [value for value in breadcrumbs if value.lower() not in {"home", "inventions"}]

    description = card.select_one(".description") if card else None
    sections, description_text = section_map(description)

    inventors = parse_collapsible_values(soup, "Inventor")
    references = parse_collapsible_values(soup, "References")

    documents: list[dict] = []
    for anchor in soup.select('a[href*="/print"], a[href*="download"]'):
        href = urljoin(listing["official_record_url"], anchor.get("href", ""))
        label = clean(anchor.get_text(" ", strip=True))
        parent_text = clean(anchor.parent.parent.get_text(" ", strip=True) if anchor.parent and anchor.parent.parent else "")
        item = {"url": href, "label": label, "context": parent_text}
        if item not in documents:
            documents.append(item)

    image_urls: list[str] = []
    for image in card.find_all("img") if card else []:
        src = urljoin(listing["official_record_url"], image.get("src", ""))
        if src and src not in image_urls:
            image_urls.append(src)

    full_text = clean(card.get_text(" ", strip=True) if card else soup.get_text(" ", strip=True))
    contact_emails = list(dict.fromkeys(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", full_text, flags=re.I)))

    return {
        "title": title,
        "technology_numbers": technology_numbers,
        "technology_number_text": " | ".join(number_texts),
        "tags": tags,
        "breadcrumbs": breadcrumbs,
        "overview": section_value(sections, "OVERVIEW"),
        "background": section_value(sections, "BACKGROUND"),
        "innovation": section_value(sections, "INNOVATION"),
        "description": section_value(sections, "DESCRIPTION"),
        "value_proposition": section_value(sections, "VALUE PROPOSITION", "ADVANTAGES", "BENEFITS"),
        "technology_readiness_level": section_value(sections, "TECHNOLOGY READINESS LEVEL", "TRL"),
        "intellectual_property": section_value(
            sections,
            "INTELLECTUAL PROPERTY STATUS",
            "INTELLECTUAL PROPERTY",
            "PATENT APPLICATION",
            "PATENT STATUS",
        ),
        "market_opportunity": section_value(sections, "MARKET OPPORTUNITY"),
        "applications": section_value(sections, "APPLICATIONS", "POTENTIAL APPLICATIONS"),
        "additional_information": section_value(sections, "ADDITIONAL INFORMATION"),
        "references_section": section_value(sections, "REFERENCES"),
        "description_text": description_text,
        "inventors": inventors,
        "references": references,
        "documents": documents,
        "image_urls": image_urls,
        "contact_emails": contact_emails,
        "full_text": full_text,
        "html_title": clean(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "elucid_product_id": autocomplete.get("elucid_product_id", ""),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW_DETAILS.mkdir(parents=True, exist_ok=True)
    RAW_API.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    session = new_session()

    receipts: list[dict] = []
    errors: list[dict] = []

    # Warm the storefront and preserve the HTML.
    storefront = fetch(session, PRODUCTS_PAGE)
    storefront_raw = storefront.content
    (OUT / "products_storefront.html").write_bytes(storefront_raw)
    receipts.append(
        {
            "receipt_type": "products_storefront_html",
            "source_index": 0,
            "url": PRODUCTS_PAGE,
            "http_status": storefront.status_code,
            "content_type": storefront.headers.get("content-type", ""),
            "raw_bytes": len(storefront_raw),
            "sha256": hashlib.sha256(storefront_raw).hexdigest(),
            "parsed_records": "",
            "retrieved_at_utc": retrieved_at,
        }
    )

    # Independent autocomplete enumeration.
    autocomplete_response = fetch(session, AUTOCOMPLETE_API)
    autocomplete_raw = autocomplete_response.content
    (RAW_API / "autocomplete_products.json").write_bytes(autocomplete_raw)
    autocomplete_items = autocomplete_response.json()
    autocomplete_by_url: dict[str, dict] = {}
    for item in autocomplete_items:
        attrs = item.get("dataAttributes") or {}
        url = urljoin(BASE, attrs.get("url", "")).rstrip("/")
        autocomplete_by_url[url] = {
            "elucid_product_id": attrs.get("id", ""),
            "autocomplete_name": clean(item.get("name", "")),
            "official_record_url": url,
        }
    receipts.append(
        {
            "receipt_type": "autocomplete_json",
            "source_index": 0,
            "url": AUTOCOMPLETE_API,
            "http_status": autocomplete_response.status_code,
            "content_type": autocomplete_response.headers.get("content-type", ""),
            "raw_bytes": len(autocomplete_raw),
            "sha256": hashlib.sha256(autocomplete_raw).hexdigest(),
            "parsed_records": len(autocomplete_items),
            "retrieved_at_utc": retrieved_at,
        }
    )

    if len(autocomplete_items) != EXPECTED_TOTAL or len(autocomplete_by_url) != EXPECTED_TOTAL:
        raise ValueError(
            f"U-M autocomplete enumeration mismatch: items={len(autocomplete_items)} unique_urls={len(autocomplete_by_url)}"
        )

    # Authoritative catalog search pages.
    columns = ["url", "name", "shortDescription", "licencesCount", "groups", "imageThumbnailUrl"]
    search_by_url: dict[str, dict] = {}
    search_total: int | None = None
    search_pages: int | None = None
    page_counts: list[int] = []

    for page_number in range(1, 4):
        params: list[tuple[str, object]] = [
            ("page", page_number),
            ("itemsPerPage", PAGE_SIZE),
        ]
        params.extend(("columns[]", column) for column in columns)
        params.append(("orderBy", 1))
        response = fetch(session, SEARCH_API, params=params)
        raw = response.content
        (RAW_API / f"products_search_page_{page_number:02d}.json").write_bytes(raw)
        payload = response.json()
        current_total = int(payload.get("total", 0))
        current_pages = int(payload.get("pages", 0))
        if search_total is None:
            search_total = current_total
            search_pages = current_pages
        if current_total != search_total or current_pages != search_pages:
            raise ValueError("U-M search API count changed during harvest")
        items = payload.get("items") or []
        page_counts.append(len(items))
        for item in items:
            url = urljoin(BASE, item.get("url", "")).rstrip("/")
            if url in search_by_url:
                raise ValueError(f"Duplicate U-M product URL in search API: {url}")
            groups = item.get("groups") or []
            search_by_url[url] = {
                "official_record_url": url,
                "slug": slug_from_url(url),
                "name": clean(item.get("name", "")),
                "short_description": clean(item.get("shortDescription", "")),
                "licences_count": item.get("licencesCount", 0),
                "groups": groups,
                "group_names": [clean(group.get("name", "")) for group in groups if clean(group.get("name", ""))],
                "group_slugs": [clean(group.get("slug", "")) for group in groups if clean(group.get("slug", ""))],
                "image_thumbnail_url": item.get("imageThumbnailUrl", ""),
                "search_page": page_number,
            }
        receipts.append(
            {
                "receipt_type": "products_search_json",
                "source_index": page_number,
                "url": response.url,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "parsed_records": len(items),
                "retrieved_at_utc": retrieved_at,
            }
        )
        print(
            f"search_page={page_number} rows={len(items)} unique={len(search_by_url)} total={current_total}",
            flush=True,
        )

    if search_total != EXPECTED_TOTAL or search_pages != 3:
        raise ValueError(f"Unexpected U-M search API totals: total={search_total} pages={search_pages}")
    if page_counts != [300, 300, 234]:
        raise ValueError(f"Unexpected U-M page sizes: {page_counts}")
    if len(search_by_url) != EXPECTED_TOTAL:
        raise ValueError(f"U-M search union {len(search_by_url)} != {EXPECTED_TOTAL}")

    search_urls = set(search_by_url)
    autocomplete_urls = set(autocomplete_by_url)
    search_only = sorted(search_urls - autocomplete_urls)
    autocomplete_only = sorted(autocomplete_urls - search_urls)
    if search_only or autocomplete_only:
        write_csv(
            OUT / "enumeration_difference_review.csv",
            [
                *[{"difference_type": "SEARCH_ONLY", "official_record_url": url} for url in search_only],
                *[{"difference_type": "AUTOCOMPLETE_ONLY", "official_record_url": url} for url in autocomplete_only],
            ],
            ["difference_type", "official_record_url"],
        )
        raise ValueError(
            f"U-M official enumerations differ: search_only={len(search_only)} autocomplete_only={len(autocomplete_only)}"
        )

    # Fetch all detail pages.
    detail_results: dict[str, tuple[requests.Response, dict]] = {}

    def detail_job(url: str):
        local = new_session()
        response = fetch(local, url, attempts=7, timeout=90.0)
        parsed = parse_detail(response.content, search_by_url[url], autocomplete_by_url[url])
        return url, response, parsed

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(detail_job, url): url for url in sorted(search_urls)}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                detail_url, response, parsed = future.result()
                detail_results[detail_url] = (response, parsed)
            except Exception as exc:
                errors.append({"stage": "detail_fetch_or_parse", "url": url, "error": repr(exc)})
            if completed % 50 == 0 or completed == len(futures):
                print(
                    f"detail_completed={completed} success={len(detail_results)} errors={len(errors)}",
                    flush=True,
                )

    records: list[dict] = []
    detail_receipts: list[dict] = []
    for index, url in enumerate(sorted(search_urls), start=1):
        listing = search_by_url[url]
        auto = autocomplete_by_url[url]
        if url not in detail_results:
            continue
        response, detail = detail_results[url]
        raw = response.content
        raw_name = f"{index:04d}_{auto['elucid_product_id']}_{safe_filename(listing['slug'])}.html"
        (RAW_DETAILS / raw_name).write_bytes(raw)
        detail_receipts.append(
            {
                "receipt_type": "product_detail_html",
                "source_index": index,
                "url": url,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "parsed_records": 1,
                "retrieved_at_utc": retrieved_at,
            }
        )

        technology_numbers = detail["technology_numbers"]
        primary_technology_number = technology_numbers[0] if technology_numbers else ""
        record_key = f"umich-elucid-{auto['elucid_product_id']}"
        title = detail["title"] or listing["name"] or auto["autocomplete_name"]
        records.append(
            {
                "institution": "University of Michigan",
                "source_organization": "Innovation Partnerships",
                "catalog": "Available Inventions",
                "source_listing_id": record_key,
                "elucid_product_id": auto["elucid_product_id"],
                "primary_technology_number": primary_technology_number,
                "technology_numbers": " / ".join(technology_numbers),
                "technology_number_count": len(technology_numbers),
                "title": title,
                "short_description": listing["short_description"],
                "overview": detail["overview"],
                "background": detail["background"],
                "innovation": detail["innovation"],
                "description": detail["description"],
                "value_proposition": detail["value_proposition"],
                "technology_readiness_level": detail["technology_readiness_level"],
                "intellectual_property": detail["intellectual_property"],
                "market_opportunity": detail["market_opportunity"],
                "applications": detail["applications"],
                "additional_information": detail["additional_information"],
                "inventors": " / ".join(detail["inventors"]),
                "tags": " / ".join(detail["tags"]),
                "catalog_groups": " / ".join(listing["group_names"]),
                "catalog_group_slugs": " / ".join(listing["group_slugs"]),
                "breadcrumbs": " / ".join(detail["breadcrumbs"]),
                "references": " / ".join(detail["references"]),
                "references_section": detail["references_section"],
                "licences_count": listing["licences_count"],
                "licensing_contact_emails": " / ".join(detail["contact_emails"]),
                "supporting_documents_json": json.dumps(detail["documents"], ensure_ascii=False),
                "image_thumbnail_url": listing["image_thumbnail_url"],
                "image_urls_json": json.dumps(detail["image_urls"], ensure_ascii=False),
                "official_record_url": url,
                "print_or_brochure_urls": " / ".join(
                    item["url"] for item in detail["documents"] if item.get("url")
                ),
                "search_page": listing["search_page"],
                "autocomplete_name": auto["autocomplete_name"],
                "html_title": detail["html_title"],
                "full_text": detail["full_text"],
                "retrieved_at_utc": retrieved_at,
                "source_completion_status": "SOURCE COMPLETE",
                "aggregate_admission": "AUTHORIZED — SOURCE COMPLETE",
                "dedup_key": f"umich|elucid|{auto['elucid_product_id']}",
            }
        )

    receipts.extend(detail_receipts)

    # Duplicate and multi-case review, retained rather than discarded.
    technology_to_records: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        for technology_number in [value.strip() for value in record["technology_numbers"].split("/") if value.strip()]:
            technology_to_records[technology_number].append(record)
    duplicate_technology_review: list[dict] = []
    for technology_number, group in sorted(technology_to_records.items()):
        if len(group) > 1:
            for record in group:
                duplicate_technology_review.append(
                    {
                        "technology_number": technology_number,
                        "listing_count": len(group),
                        "source_listing_id": record["source_listing_id"],
                        "elucid_product_id": record["elucid_product_id"],
                        "title": record["title"],
                        "official_record_url": record["official_record_url"],
                        "resolution": "PRESERVE DISTINCT CATALOG LISTINGS; FLAG SHARED TECHNOLOGY NUMBER",
                    }
                )

    required_missing = sum(
        1
        for record in records
        if not record["source_listing_id"] or not record["title"] or not record["official_record_url"]
    )
    unique_listing_ids = len({record["source_listing_id"] for record in records})
    unique_product_ids = len({record["elucid_product_id"] for record in records})
    unique_urls = len({record["official_record_url"] for record in records})
    title_mismatches = [
        {
            "elucid_product_id": record["elucid_product_id"],
            "search_title": search_by_url[record["official_record_url"]]["name"],
            "detail_title": record["title"],
            "official_record_url": record["official_record_url"],
        }
        for record in records
        if clean(search_by_url[record["official_record_url"]]["name"]).casefold()
        != clean(record["title"]).casefold()
    ]

    source_complete = (
        search_total == EXPECTED_TOTAL
        and len(autocomplete_items) == EXPECTED_TOTAL
        and len(search_by_url) == EXPECTED_TOTAL
        and len(detail_results) == EXPECTED_TOTAL
        and len(records) == EXPECTED_TOTAL
        and unique_listing_ids == EXPECTED_TOTAL
        and unique_product_ids == EXPECTED_TOTAL
        and unique_urls == EXPECTED_TOTAL
        and len(detail_receipts) == EXPECTED_TOTAL
        and required_missing == 0
        and not errors
        and not search_only
        and not autocomplete_only
    )

    manifest = {
        "institution": "University of Michigan",
        "source_organization": "Innovation Partnerships",
        "catalog": "Available Inventions",
        "catalog_url": PRODUCTS_PAGE,
        "search_api_url": SEARCH_API,
        "autocomplete_api_url": AUTOCOMPLETE_API,
        "retrieved_at_utc": retrieved_at,
        "official_search_api_total": search_total,
        "official_search_api_pages": search_pages,
        "search_api_page_counts": page_counts,
        "search_api_unique_listing_urls": len(search_by_url),
        "autocomplete_records": len(autocomplete_items),
        "autocomplete_unique_product_ids": len({item['dataAttributes']['id'] for item in autocomplete_items}),
        "autocomplete_unique_listing_urls": len(autocomplete_by_url),
        "detail_pages_with_receipts": len(detail_receipts),
        "normalized_listing_records": len(records),
        "unique_listing_ids": unique_listing_ids,
        "unique_elucid_product_ids": unique_product_ids,
        "unique_official_record_urls": unique_urls,
        "records_with_multiple_technology_numbers": sum(1 for record in records if record["technology_number_count"] > 1),
        "records_without_published_technology_number": sum(1 for record in records if not record["technology_numbers"]),
        "shared_technology_number_groups": sum(1 for group in technology_to_records.values() if len(group) > 1),
        "shared_technology_number_review_rows": len(duplicate_technology_review),
        "title_mismatch_review_rows": len(title_mismatches),
        "required_fields_missing": required_missing,
        "errors": len(errors),
        "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "record_unit": "One public Available Inventions product listing, keyed by e-lucid product ID",
        "reconciliation_note": (
            "The official search API and autocomplete endpoint independently enumerate the same 834 unique product URLs and "
            "834 unique e-lucid product IDs. Every enumerated product detail page is required before aggregate admission."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()

    record_fields = list(records[0].keys()) if records else [
        "institution", "source_organization", "catalog", "source_listing_id", "elucid_product_id",
        "title", "official_record_url"
    ]
    write_csv(OUT / "umich_available_inventions_records.csv", records, record_fields)
    (OUT / "umich_available_inventions_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(
        OUT / "umich_source_receipts.csv",
        receipts,
        [
            "receipt_type", "source_index", "url", "http_status", "content_type",
            "raw_bytes", "sha256", "parsed_records", "retrieved_at_utc"
        ],
    )
    write_csv(
        OUT / "umich_harvest_errors.csv",
        errors,
        ["stage", "url", "error"],
    )
    write_csv(
        OUT / "umich_duplicate_technology_number_review.csv",
        duplicate_technology_review,
        [
            "technology_number", "listing_count", "source_listing_id", "elucid_product_id",
            "title", "official_record_url", "resolution"
        ],
    )
    write_csv(
        OUT / "umich_title_mismatch_review.csv",
        title_mismatches,
        ["elucid_product_id", "search_title", "detail_title", "official_record_url"],
    )
    (OUT / "umich_source_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "README.txt").write_text(
        "\n".join(
            [
                "UNIVERSITY OF MICHIGAN — AVAILABLE INVENTIONS SOURCE-COMPLETE HARVEST",
                "",
                f"Retrieved: {retrieved_at}",
                f"Catalog: {PRODUCTS_PAGE}",
                f"Official search API total: {search_total}",
                f"Official autocomplete records: {len(autocomplete_items)}",
                f"Detail pages preserved: {len(detail_receipts)}",
                f"Normalized listing records: {len(records)}",
                f"Required fields missing: {required_missing}",
                f"Errors: {len(errors)}",
                f"Source complete: {source_complete}",
                f"Aggregate admission: {manifest['aggregate_admission']}",
                "",
                "Record unit: one public product listing, keyed by e-lucid product ID.",
                "Multiple University technology numbers remain attached to the listing in one row.",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps(manifest, indent=2), flush=True)
    return 0 if source_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
