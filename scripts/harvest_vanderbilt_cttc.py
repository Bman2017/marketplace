#!/usr/bin/env python3
"""Harvest Vanderbilt CTTC's complete public technology catalog.

The harvester crawls the authoritative Vanderbilt CTTC technology browser by
following its own Drupal pager links, captures every canonical technology page,
normalizes structured commercialization fields, and emits validation receipts.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://legacy.cttc.co"
CATALOG = f"{BASE}/technologies"
EXPECTED_COUNT = 184
OUT = Path("vanderbilt-cttc-harvest")
USER_AGENT = "Mozilla/5.0 (compatible; ArnsInnovations-VanderbiltCTTC-Harvester/1.0)"
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


S = session()


def fetch(url: str, attempts: int = 6, timeout: int = 45) -> requests.Response:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            response = S.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code == 200 and len(response.text.strip()) > 500:
                return response
            errors.append(f"status={response.status_code} bytes={len(response.text)} url={response.url}")
        except requests.RequestException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        time.sleep(min(10, 0.8 * (2 ** (attempt - 1))))
    raise RuntimeError(f"Failed to fetch {url}: {'; '.join(errors[-6:])}")


def canonical_technology_url(href: str, base_url: str) -> str | None:
    if not href:
        return None
    url = urljoin(base_url, href).split("#", 1)[0]
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if parsed.netloc not in {"legacy.cttc.co", "cttc.co", "www.cttc.co"}:
        return None
    if not path.startswith("/technologies/"):
        return None
    slug = path.removeprefix("/technologies/").strip("/")
    if not slug or "/" in slug:
        return None
    return f"{BASE}/technologies/{slug}"


def nearest_result_container(heading: Tag) -> Tag:
    fallback: Tag = heading
    for parent in heading.parents:
        if not isinstance(parent, Tag):
            continue
        classes = " ".join(parent.get("class", []))
        if re.search(r"views-row|technology|node--type|search-result|card|result", classes, re.I):
            return parent
        if parent.name in {"article", "li"}:
            return parent
        if parent.name == "main":
            break
        fallback = parent
    return fallback


def split_lines(container: Tag) -> list[str]:
    return [clean(line) for line in container.get_text("\n", strip=True).splitlines() if clean(line)]


def values_after_label(lines: list[str], label: str, stop_labels: set[str]) -> list[str]:
    label_cf = label.casefold().rstrip(":")
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.casefold().rstrip(":") == label_cf
        ) + 1
    except StopIteration:
        return []
    values: list[str] = []
    stops = {item.casefold().rstrip(":") for item in stop_labels}
    for line in lines[start:]:
        normalized = line.casefold().rstrip(":")
        if normalized in stops:
            break
        if line.startswith("Displaying ") or line in {"next ›", "last »", "‹ previous", "« first"}:
            break
        if len(line) > 240:
            if values:
                break
            continue
        if re.fullmatch(r"\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}", line):
            continue
        if line and line not in values:
            values.append(line)
    return values


def listing_summary(lines: list[str], title: str) -> str:
    try:
        start = next(i for i, line in enumerate(lines) if line.casefold() == title.casefold()) + 1
    except StopIteration:
        start = 0
    chunks: list[str] = []
    for line in lines[start:]:
        if line.casefold().rstrip(":") in {
            "licensing contact", "inventors", "category", "field of use",
            "research tool type", "modality", "tech id"
        }:
            break
        if line.startswith("Displaying "):
            break
        chunks.append(line)
    return clean(" ".join(chunks))


def parse_listing_page(url: str, page_number: int) -> tuple[list[dict], int | None, str | None, str]:
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))
    total_match = re.search(r"Displaying\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)", page_text, re.I)
    reported_total = int(total_match.group(3)) if total_match else None

    records: list[dict] = []
    seen: set[str] = set()
    for heading in soup.find_all(["h2", "h3"]):
        anchor = heading.find("a", href=True)
        if anchor is None and heading.parent and getattr(heading.parent, "name", None) == "a":
            anchor = heading.parent
        if not anchor:
            continue
        detail_url = canonical_technology_url(anchor.get("href", ""), response.url)
        if not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)
        title = clean(heading.get_text(" ", strip=True)) or clean(anchor.get_text(" ", strip=True))
        container = nearest_result_container(heading)
        lines = split_lines(container)
        phone_match = re.search(r"\b(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b", " ".join(lines))
        records.append(
            {
                "institution_name": "Vanderbilt University",
                "institution_id": "org-vanderbilt-university",
                "source_catalog_name": "Vanderbilt Center for Technology Transfer & Commercialization — Browse Technologies",
                "source_catalog_url": CATALOG,
                "source_page_number": page_number,
                "source_position": len(records) + 1,
                "source_record_id": urlparse(detail_url).path.rstrip("/").split("/")[-1],
                "source_identifier_type": "canonical_url_slug",
                "canonical_detail_url": detail_url,
                "title": title,
                "listing_summary": listing_summary(lines, title),
                "licensing_contacts": values_after_label(
                    lines, "Licensing Contact", {"Inventors", "Category", "Field of Use", "Research Tool Type", "Modality", "Tech ID"}
                )[:2],
                "licensing_phone": phone_match.group(0) if phone_match else "",
                "inventors": values_after_label(
                    lines, "Inventors", {"Licensing Contact", "Category", "Field of Use", "Research Tool Type", "Modality", "Tech ID"}
                ),
                "categories": values_after_label(
                    lines, "Category", {"Licensing Contact", "Inventors", "Field of Use", "Research Tool Type", "Modality", "Tech ID"}
                ),
                "fields_of_use": values_after_label(
                    lines, "Field of Use", {"Licensing Contact", "Inventors", "Category", "Research Tool Type", "Modality", "Tech ID"}
                ),
                "research_tool_types": values_after_label(
                    lines, "Research Tool Type", {"Licensing Contact", "Inventors", "Category", "Field of Use", "Modality", "Tech ID"}
                ),
                "modalities": values_after_label(
                    lines, "Modality", {"Licensing Contact", "Inventors", "Category", "Field of Use", "Research Tool Type", "Tech ID"}
                ),
                "listing_page_url": response.url,
            }
        )

    next_url = None
    pager_next = soup.select_one("li.pager-next a[href], .pager__item--next a[href], a[rel='next'][href]")
    if pager_next:
        next_url = urljoin(response.url, pager_next.get("href", ""))
    else:
        for anchor in soup.find_all("a", href=True):
            text = clean(anchor.get_text(" ", strip=True)).casefold()
            if text in {"next ›", "next", "›"}:
                next_url = urljoin(response.url, anchor.get("href", ""))
                break
    return records, reported_total, next_url, response.url


def section_text(lines: list[str], label: str, stop_labels: set[str]) -> str:
    values = values_after_label(lines, label, stop_labels)
    return clean(" ".join(values))


def anchors_near_heading(soup: BeautifulSoup, label: str) -> list[Tag]:
    target = label.casefold().rstrip(":")
    for node in soup.find_all(string=True):
        if clean(str(node)).casefold().rstrip(":") != target:
            continue
        parent = node.parent
        container = parent.parent if parent and parent.parent else parent
        if not container:
            continue
        anchors: list[Tag] = []
        for element in container.next_elements:
            if isinstance(element, Tag) and element.name in {"h2", "h3", "h4", "h5"}:
                break
            if isinstance(element, Tag) and element.name == "a" and element.get("href"):
                anchors.append(element)
            if len(anchors) >= 40:
                break
        return anchors
    return []


def parse_detail(listing: dict) -> dict:
    response = fetch(listing["canonical_detail_url"])
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [clean(line) for line in main.get_text("\n", strip=True).splitlines() if clean(line)]
    detail_text = "\n".join(lines)
    h1 = main.find("h1") or soup.find("h1")
    if h1:
        listing["title"] = clean(h1.get_text(" ", strip=True)) or listing["title"]

    all_stops = {
        "Technology Summary", "Problems Addressed", "Unique Features", "Intellectual Property Status",
        "Publications", "Licensing Contact", "Tech ID", "Inventors", "Category", "Field of Use",
        "Research Tool Type", "Modality"
    }
    tech_ids = values_after_label(lines, "Tech ID", all_stops - {"Tech ID"})
    tech_ids = [value for value in tech_ids if re.fullmatch(r"VU[A-Za-z0-9.-]+", value, re.I)]
    if not tech_ids:
        text_window = " ".join(lines)
        tech_ids = sorted(set(re.findall(r"\bVU\d{3,}[A-Za-z0-9.-]*\b", text_window, re.I)))

    detail_contacts = values_after_label(lines, "Licensing Contact", all_stops - {"Licensing Contact"})
    detail_inventors = values_after_label(lines, "Inventors", all_stops - {"Inventors"})
    detail_categories = values_after_label(lines, "Category", all_stops - {"Category"})
    detail_fields = values_after_label(lines, "Field of Use", all_stops - {"Field of Use"})
    detail_tools = values_after_label(lines, "Research Tool Type", all_stops - {"Research Tool Type"})
    detail_modalities = values_after_label(lines, "Modality", all_stops - {"Modality"})

    overview_url = ""
    for anchor in soup.find_all("a", href=True):
        text = clean(anchor.get_text(" ", strip=True)).casefold()
        href = anchor.get("href", "")
        if "download overview" in text or href.lower().endswith(".pdf"):
            overview_url = urljoin(response.url, href)
            break

    patents: list[dict] = []
    publications: list[dict] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, anchor.get("href", ""))
        text = clean(anchor.get_text(" ", strip=True))
        if "patents.google" in href or re.search(r"\b(?:US|WO|EP)[- ]?\d{6,}", text, re.I):
            patents.append({"label": text, "url": href})
        if "doi.org" in href or re.search(r"\bdoi\s*:", text, re.I):
            publications.append({"label": text, "url": href})

    phone_match = re.search(r"\b(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b", detail_text)
    listing.update(
        {
            "tech_ids": list(dict.fromkeys(tech_ids)),
            "source_identifier_type": "vanderbilt_tech_id" if tech_ids else "canonical_url_slug",
            "technology_summary": section_text(lines, "Technology Summary", all_stops - {"Technology Summary"}),
            "problems_addressed": section_text(lines, "Problems Addressed", all_stops - {"Problems Addressed"}),
            "unique_features": section_text(lines, "Unique Features", all_stops - {"Unique Features"}),
            "intellectual_property_status": section_text(lines, "Intellectual Property Status", all_stops - {"Intellectual Property Status"}),
            "publications_text": section_text(lines, "Publications", all_stops - {"Publications"}),
            "overview_download_url": overview_url,
            "patent_links": patents,
            "publication_links": publications,
            "licensing_contacts": detail_contacts or listing.get("licensing_contacts", []),
            "licensing_phone": phone_match.group(0) if phone_match else listing.get("licensing_phone", ""),
            "inventors": detail_inventors or listing.get("inventors", []),
            "categories": detail_categories or listing.get("categories", []),
            "fields_of_use": detail_fields or listing.get("fields_of_use", []),
            "research_tool_types": detail_tools or listing.get("research_tool_types", []),
            "modalities": detail_modalities or listing.get("modalities", []),
            "detail_text": detail_text,
            "detail_sha256": hashlib.sha256(clean(detail_text).encode("utf-8")).hexdigest(),
            "http_status": response.status_code,
            "harvested_at_utc": NOW,
            "provenance_tier": "official_public_source",
            "corpus_tier": "discovery_pool",
            "canon_status": "not_promoted",
        }
    )
    return listing


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "institution_name", "institution_id", "source_catalog_name", "source_catalog_url",
        "source_page_number", "source_position", "source_record_id", "source_identifier_type",
        "tech_ids", "canonical_detail_url", "title", "listing_summary", "technology_summary",
        "problems_addressed", "unique_features", "intellectual_property_status",
        "licensing_contacts", "licensing_phone", "inventors", "categories", "fields_of_use",
        "research_tool_types", "modalities", "overview_download_url", "patent_links",
        "publication_links", "publications_text", "listing_page_url", "detail_text",
        "detail_sha256", "http_status", "harvested_at_utc", "provenance_tier",
        "corpus_tier", "canon_status"
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            cooked = dict(row)
            for field in [
                "tech_ids", "licensing_contacts", "inventors", "categories",
                "fields_of_use", "research_tool_types", "modalities"
            ]:
                cooked[field] = " | ".join(cooked.get(field) or [])
            for field in ["patent_links", "publication_links"]:
                cooked[field] = json.dumps(cooked.get(field) or [], ensure_ascii=False, sort_keys=True)
            writer.writerow({field: cooked.get(field, "") for field in fields})


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    current_url = CATALOG
    page_number = 1
    page_receipts: list[dict] = []
    listings: list[dict] = []
    visited_pages: set[str] = set()
    reported_total: int | None = None

    while current_url:
        if current_url in visited_pages:
            raise RuntimeError(f"Pager loop detected at {current_url}")
        visited_pages.add(current_url)
        page_rows, page_total, next_url, resolved_url = parse_listing_page(current_url, page_number)
        if page_total is not None:
            if reported_total is None:
                reported_total = page_total
            elif reported_total != page_total:
                raise RuntimeError(f"Catalog total changed: {reported_total} -> {page_total} on {resolved_url}")
        listings.extend(page_rows)
        page_receipts.append(
            {
                "page_number": page_number,
                "requested_url": current_url,
                "resolved_url": resolved_url,
                "records_found": len(page_rows),
                "next_url": next_url,
            }
        )
        print(f"page={page_number} records={len(page_rows)} cumulative={len(listings)}", flush=True)
        current_url = next_url
        page_number += 1
        if page_number > 100:
            raise RuntimeError("Unexpectedly exceeded 100 result pages")

    total = reported_total or EXPECTED_COUNT
    expected_pages = math.ceil(total / 10)
    duplicate_listing_urls: list[str] = []
    unique_by_url: dict[str, dict] = {}
    for row in listings:
        url = row["canonical_detail_url"]
        if url in unique_by_url:
            duplicate_listing_urls.append(url)
        else:
            unique_by_url[url] = row
    unique_listings = list(unique_by_url.values())

    enriched: list[dict] = []
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(parse_detail, dict(row)): row for row in unique_listings}
        for index, future in enumerate(as_completed(futures), start=1):
            source = futures[future]
            try:
                enriched.append(future.result())
            except Exception as exc:  # noqa: BLE001 - receipt must preserve failures
                failures.append(
                    {
                        "source_record_id": source["source_record_id"],
                        "canonical_detail_url": source["canonical_detail_url"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            if index % 25 == 0 or index == len(futures):
                print(f"details={index}/{len(futures)} failures={len(failures)}", flush=True)

    enriched.sort(key=lambda row: (int(row["source_page_number"]), int(row["source_position"])))
    ids = [row["source_record_id"] for row in enriched]
    duplicate_ids = sorted({value for value in ids if ids.count(value) > 1})
    tech_id_values = [tech_id for row in enriched for tech_id in (row.get("tech_ids") or [])]
    duplicate_tech_ids = sorted({value for value in tech_id_values if tech_id_values.count(value) > 1})
    missing_required = [
        row.get("canonical_detail_url") or row.get("source_record_id")
        for row in enriched
        if not row.get("title") or not row.get("canonical_detail_url") or not row.get("detail_text")
    ]

    checks = {
        "reported_catalog_count_equals_expected": total == EXPECTED_COUNT,
        "result_page_count_equals_expected": len(page_receipts) == expected_pages,
        "all_result_pages_nonempty": all(receipt["records_found"] > 0 for receipt in page_receipts),
        "unique_listing_count_equals_reported_catalog_count": len(unique_listings) == total,
        "detail_success_count_equals_listing_count": len(enriched) == len(unique_listings),
        "no_detail_failures": not failures,
        "no_duplicate_listing_urls": not duplicate_listing_urls,
        "no_duplicate_record_ids": not duplicate_ids,
        "no_missing_required_fields": not missing_required,
        "all_http_status_200": all(row.get("http_status") == 200 for row in enriched),
    }
    passed = all(checks.values())

    optional_coverage = {
        "records_with_tech_ids": sum(bool(row.get("tech_ids")) for row in enriched),
        "records_with_technology_summary": sum(bool(row.get("technology_summary")) for row in enriched),
        "records_with_problems_addressed": sum(bool(row.get("problems_addressed")) for row in enriched),
        "records_with_unique_features": sum(bool(row.get("unique_features")) for row in enriched),
        "records_with_ip_status": sum(bool(row.get("intellectual_property_status")) for row in enriched),
        "records_with_patent_links": sum(bool(row.get("patent_links")) for row in enriched),
        "records_with_publications": sum(bool(row.get("publications_text") or row.get("publication_links")) for row in enriched),
        "records_with_licensing_contacts": sum(bool(row.get("licensing_contacts")) for row in enriched),
        "records_with_inventors": sum(bool(row.get("inventors")) for row in enriched),
        "records_with_categories": sum(bool(row.get("categories")) for row in enriched),
        "records_with_fields_of_use": sum(bool(row.get("fields_of_use")) for row in enriched),
        "records_with_research_tool_types": sum(bool(row.get("research_tool_types")) for row in enriched),
        "records_with_modalities": sum(bool(row.get("modalities")) for row in enriched),
        "records_with_overview_downloads": sum(bool(row.get("overview_download_url")) for row in enriched),
    }

    write_csv(OUT / "vanderbilt_cttc_technologies.csv", enriched)
    write_jsonl(OUT / "vanderbilt_cttc_technologies.jsonl", enriched)
    validation = {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "catalog_reported_total": total,
        "configured_expected_total": EXPECTED_COUNT,
        "expected_result_pages": expected_pages,
        "result_pages_harvested": len(page_receipts),
        "unique_listings": len(unique_listings),
        "detail_records": len(enriched),
        "optional_field_coverage": optional_coverage,
        "duplicate_listing_urls": duplicate_listing_urls,
        "duplicate_record_ids": duplicate_ids,
        "duplicate_tech_ids": duplicate_tech_ids,
        "missing_required": missing_required,
        "failures": failures,
        "page_receipts": page_receipts,
        "validated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    (OUT / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "institution": "Vanderbilt University",
        "source": "Vanderbilt Center for Technology Transfer & Commercialization",
        "catalog_url": CATALOG,
        "harvest_scope": "All publicly exposed Vanderbilt CTTC technology listings and canonical detail pages",
        "record_count": len(enriched),
        "catalog_reported_total": total,
        "validation_status": validation["status"],
        "source_identifier_policy": "Preserve Vanderbilt Tech IDs when exposed; retain canonical URL slug as stable fallback provenance identifier.",
        "governance": {"corpus_tier": "discovery_pool", "canon_status": "not_promoted"},
        "files": [
            "vanderbilt_cttc_technologies.csv",
            "vanderbilt_cttc_technologies.jsonl",
            "validation.json",
            "manifest.json",
        ],
        "harvested_at_utc": NOW,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(validation, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
