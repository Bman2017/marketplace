#!/usr/bin/env python3
"""Source-complete Vanderbilt CTTC technology harvester, semantic parser v2."""
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
OUT = Path("vanderbilt-cttc-harvest-v2")
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ArnsInnovations-VanderbiltCTTC-Harvester/2.0)",
    "Accept-Language": "en-US,en;q=0.9",
})

LABELS = {
    "Technology Summary", "Problems Addressed", "Unique Features",
    "Intellectual Property Status", "Publications", "Licensing Contact",
    "Tech ID", "Inventors", "Category", "Field of Use",
    "Research Tool Type", "Modality",
}
FOOTER_MARKERS = {
    "Center for Technology Transfer & Commercialization",
    "2100 West End Ave, Suite 750", "Nashville, TN 37203", "Tel",
    "© 2014 -2026 Vanderbilt University", "Send message",
}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized(value: str) -> str:
    return clean(value).casefold().rstrip(":")


def fetch(url: str, attempts: int = 6) -> requests.Response:
    errors = []
    for attempt in range(attempts):
        try:
            response = S.get(url, timeout=45, allow_redirects=True)
            if response.status_code == 200 and len(response.text.strip()) > 500:
                return response
            errors.append(f"status={response.status_code} bytes={len(response.text)}")
        except requests.RequestException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        time.sleep(min(10, 0.8 * 2**attempt))
    raise RuntimeError(f"Failed {url}: {'; '.join(errors[-6:])}")


def canonical_detail_url(href: str, base_url: str) -> str | None:
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


def lines_from(tag: Tag) -> list[str]:
    return [clean(x) for x in tag.get_text("\n", strip=True).splitlines() if clean(x)]


def label_index(lines: list[str], label: str, *, last: bool = True) -> int | None:
    target = normalized(label)
    matches = [i for i, line in enumerate(lines) if normalized(line) == target]
    if not matches:
        return None
    return matches[-1] if last else matches[0]


def collect_section(
    lines: list[str],
    label: str,
    stop_labels: set[str],
    *,
    last_label: bool = True,
    skip_phone: bool = False,
    stop_at_footer: bool = False,
) -> list[str]:
    index = label_index(lines, label, last=last_label)
    if index is None:
        return []
    stops = {normalized(x) for x in stop_labels}
    footer = {normalized(x) for x in FOOTER_MARKERS}
    values: list[str] = []
    for line in lines[index + 1:]:
        norm = normalized(line)
        if norm in stops:
            break
        if stop_at_footer and norm in footer:
            break
        if line in {",", ":", ".", ";", "|"}:
            continue
        if skip_phone and re.fullmatch(r"(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}", line):
            continue
        if line not in values:
            values.append(line)
    return values


def nearest_result_container(heading: Tag) -> Tag:
    for parent in heading.parents:
        if not isinstance(parent, Tag):
            continue
        classes = " ".join(parent.get("class", []))
        if re.search(r"views-row|node--type|technology|result|card", classes, re.I):
            return parent
        if parent.name in {"article", "li"}:
            return parent
        if parent.name == "main":
            break
    return heading


def parse_listing_page(url: str, page_number: int) -> tuple[list[dict], int | None, str | None, str]:
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    match = re.search(r"Displaying\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)", text, re.I)
    total = int(match.group(3)) if match else None
    rows = []
    seen = set()
    for heading in soup.find_all(["h2", "h3"]):
        anchor = heading.find("a", href=True)
        if anchor is None and heading.parent and getattr(heading.parent, "name", None) == "a":
            anchor = heading.parent
        if not anchor:
            continue
        detail_url = canonical_detail_url(anchor.get("href", ""), response.url)
        if not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)
        title = clean(heading.get_text(" ", strip=True)) or clean(anchor.get_text(" ", strip=True))
        container = nearest_result_container(heading)
        lines = lines_from(container)
        try:
            title_i = next(i for i, line in enumerate(lines) if line.casefold() == title.casefold())
        except StopIteration:
            title_i = -1
        summary = []
        for line in lines[title_i + 1:]:
            if normalized(line) in {normalized(x) for x in LABELS}:
                break
            if line.startswith("Displaying "):
                break
            summary.append(line)
        phone = re.search(r"\b(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b", " ".join(lines))
        rows.append({
            "institution_name": "Vanderbilt University",
            "institution_id": "org-vanderbilt-university",
            "source_catalog_name": "Vanderbilt Center for Technology Transfer & Commercialization — Browse Technologies",
            "source_catalog_url": CATALOG,
            "source_page_number": page_number,
            "source_position": len(rows) + 1,
            "source_record_id": urlparse(detail_url).path.rstrip("/").split("/")[-1],
            "source_identifier_type": "canonical_url_slug",
            "canonical_detail_url": detail_url,
            "title": title,
            "listing_summary": clean(" ".join(summary)),
            "licensing_contacts": collect_section(lines, "Licensing Contact", LABELS - {"Licensing Contact"}, skip_phone=True),
            "licensing_phone": phone.group(0) if phone else "",
            "inventors": collect_section(lines, "Inventors", LABELS - {"Inventors"}),
            "categories": collect_section(lines, "Category", LABELS - {"Category"}),
            "fields_of_use": collect_section(lines, "Field of Use", LABELS - {"Field of Use"}),
            "research_tool_types": collect_section(lines, "Research Tool Type", LABELS - {"Research Tool Type"}),
            "modalities": collect_section(lines, "Modality", LABELS - {"Modality"}, stop_at_footer=True),
            "listing_page_url": response.url,
        })

    next_url = None
    next_anchor = soup.select_one("li.pager-next a[href], .pager__item--next a[href], a[rel='next'][href]")
    if next_anchor:
        next_url = urljoin(response.url, next_anchor.get("href", ""))
    else:
        for anchor in soup.find_all("a", href=True):
            if normalized(anchor.get_text(" ", strip=True)) in {"next ›", "next", "›"}:
                next_url = urljoin(response.url, anchor.get("href", ""))
                break
    return rows, total, next_url, response.url


def parse_detail(row: dict) -> dict:
    response = fetch(row["canonical_detail_url"])
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = lines_from(main)
    detail_text = "\n".join(lines)
    h1 = main.find("h1") or soup.find("h1")
    if h1:
        row["title"] = clean(h1.get_text(" ", strip=True)) or row["title"]

    tech_ids = [
        value for value in collect_section(lines, "Tech ID", LABELS - {"Tech ID"})
        if re.fullmatch(r"VU[A-Za-z0-9.-]+", value, re.I)
    ]
    if not tech_ids:
        tech_ids = sorted(set(re.findall(r"\bVU\d{3,}[A-Za-z0-9.-]*\b", detail_text, re.I)))

    contacts = collect_section(lines, "Licensing Contact", LABELS - {"Licensing Contact"}, skip_phone=True)
    inventors = collect_section(lines, "Inventors", LABELS - {"Inventors"})
    categories = collect_section(lines, "Category", LABELS - {"Category"})
    fields = collect_section(lines, "Field of Use", LABELS - {"Field of Use"})
    tools = collect_section(lines, "Research Tool Type", LABELS - {"Research Tool Type"})
    modalities = collect_section(lines, "Modality", LABELS - {"Modality"}, stop_at_footer=True)

    overview = ""
    patents = []
    publications = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, anchor.get("href", ""))
        text = clean(anchor.get_text(" ", strip=True))
        if not overview and ("download overview" in text.casefold() or href.lower().endswith(".pdf")):
            overview = href
        if "patents.google" in href or re.search(r"\b(?:US|WO|EP)[- ]?\d{6,}", text, re.I):
            patents.append({"label": text, "url": href})
        if "doi.org" in href or re.search(r"\bdoi\s*:", text, re.I):
            publications.append({"label": text, "url": href})

    technology_summary = clean(" ".join(collect_section(
        lines, "Technology Summary", LABELS - {"Technology Summary"}, last_label=False
    ))) or row.get("listing_summary", "")
    problems = clean(" ".join(collect_section(
        lines, "Problems Addressed", LABELS - {"Problems Addressed"}, last_label=False
    )))
    unique_features = clean(" ".join(collect_section(
        lines, "Unique Features", LABELS - {"Unique Features"}, last_label=False
    )))
    ip_status = clean(" ".join(collect_section(
        lines, "Intellectual Property Status", LABELS - {"Intellectual Property Status"}, last_label=False
    )))
    publications_text = clean(" ".join(collect_section(
        lines, "Publications", LABELS - {"Publications"}, last_label=False
    )))
    phone = re.search(r"\b(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b", detail_text)

    row.update({
        "tech_ids": list(dict.fromkeys(tech_ids)),
        "source_identifier_type": "vanderbilt_tech_id" if tech_ids else "canonical_url_slug",
        "technology_summary": technology_summary,
        "problems_addressed": problems,
        "unique_features": unique_features,
        "intellectual_property_status": ip_status,
        "publications_text": publications_text,
        "overview_download_url": overview,
        "patent_links": patents,
        "publication_links": publications,
        "licensing_contacts": contacts or row.get("licensing_contacts", []),
        "licensing_phone": phone.group(0) if phone else row.get("licensing_phone", ""),
        "inventors": inventors or row.get("inventors", []),
        "categories": categories or row.get("categories", []),
        "fields_of_use": fields or row.get("fields_of_use", []),
        "research_tool_types": tools or row.get("research_tool_types", []),
        "modalities": modalities or row.get("modalities", []),
        "detail_text": detail_text,
        "detail_sha256": hashlib.sha256(clean(detail_text).encode("utf-8")).hexdigest(),
        "http_status": response.status_code,
        "harvested_at_utc": NOW,
        "provenance_tier": "official_public_source",
        "corpus_tier": "discovery_pool",
        "canon_status": "not_promoted",
        "semantic_parse_status": "parsed",
    })
    return row


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
        "corpus_tier", "canon_status", "semantic_parse_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            cooked = dict(row)
            for field in ["tech_ids", "licensing_contacts", "inventors", "categories", "fields_of_use", "research_tool_types", "modalities"]:
                cooked[field] = " | ".join(cooked.get(field) or [])
            for field in ["patent_links", "publication_links"]:
                cooked[field] = json.dumps(cooked.get(field) or [], ensure_ascii=False, sort_keys=True)
            writer.writerow({field: cooked.get(field, "") for field in fields})


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    current = CATALOG
    page_number = 1
    pages = []
    listings = []
    visited = set()
    reported_total = None
    while current:
        if current in visited:
            raise RuntimeError(f"Pager loop at {current}")
        visited.add(current)
        page_rows, total, next_url, resolved = parse_listing_page(current, page_number)
        if total is not None:
            if reported_total is None:
                reported_total = total
            elif reported_total != total:
                raise RuntimeError(f"Catalog total changed {reported_total} -> {total}")
        listings.extend(page_rows)
        pages.append({"page_number": page_number, "requested_url": current, "resolved_url": resolved, "records_found": len(page_rows), "next_url": next_url})
        print(f"page={page_number} records={len(page_rows)} cumulative={len(listings)}", flush=True)
        current = next_url
        page_number += 1
        if page_number > 100:
            raise RuntimeError("Exceeded 100 pages")

    total = reported_total or EXPECTED_COUNT
    unique = {}
    duplicate_urls = []
    for row in listings:
        url = row["canonical_detail_url"]
        if url in unique:
            duplicate_urls.append(url)
        else:
            unique[url] = row
    source_rows = list(unique.values())

    enriched, failures = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(parse_detail, dict(row)): row for row in source_rows}
        for i, future in enumerate(as_completed(futures), 1):
            source = futures[future]
            try:
                enriched.append(future.result())
            except Exception as exc:
                failures.append({"canonical_detail_url": source["canonical_detail_url"], "error": f"{type(exc).__name__}: {exc}"})
            if i % 25 == 0 or i == len(futures):
                print(f"details={i}/{len(futures)} failures={len(failures)}", flush=True)
    enriched.sort(key=lambda row: (int(row["source_page_number"]), int(row["source_position"])))

    ids = [row["source_record_id"] for row in enriched]
    duplicate_ids = sorted({value for value in ids if ids.count(value) > 1})
    tech_id_values = [value for row in enriched for value in row.get("tech_ids", [])]
    duplicate_tech_ids = sorted({value for value in tech_id_values if tech_id_values.count(value) > 1})
    missing_required = [row.get("canonical_detail_url") for row in enriched if not row.get("title") or not row.get("canonical_detail_url") or not row.get("detail_text")]
    coverage = {
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
    expected_pages = math.ceil(total / 10)
    checks = {
        "reported_catalog_count_equals_expected": total == EXPECTED_COUNT,
        "result_page_count_equals_expected": len(pages) == expected_pages,
        "all_result_pages_nonempty": all(page["records_found"] > 0 for page in pages),
        "unique_listing_count_equals_reported_catalog_count": len(source_rows) == total,
        "detail_success_count_equals_listing_count": len(enriched) == len(source_rows),
        "no_detail_failures": not failures,
        "no_duplicate_listing_urls": not duplicate_urls,
        "no_duplicate_record_ids": not duplicate_ids,
        "no_missing_required_fields": not missing_required,
        "all_http_status_200": all(row.get("http_status") == 200 for row in enriched),
        "semantic_summary_coverage_at_least_95_percent": coverage["records_with_technology_summary"] >= math.ceil(total * 0.95),
        "all_inventors_parsed": coverage["records_with_inventors"] == total,
        "all_licensing_contacts_parsed": coverage["records_with_licensing_contacts"] == total,
        "category_coverage_at_least_98_percent": coverage["records_with_categories"] >= math.ceil(total * 0.98),
    }
    passed = all(checks.values())

    write_csv(OUT / "vanderbilt_cttc_technologies.csv", enriched)
    with (OUT / "vanderbilt_cttc_technologies.jsonl").open("w", encoding="utf-8") as file:
        for row in enriched:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    validation = {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "catalog_reported_total": total,
        "configured_expected_total": EXPECTED_COUNT,
        "expected_result_pages": expected_pages,
        "result_pages_harvested": len(pages),
        "unique_listings": len(source_rows),
        "detail_records": len(enriched),
        "optional_field_coverage": coverage,
        "duplicate_listing_urls": duplicate_urls,
        "duplicate_record_ids": duplicate_ids,
        "duplicate_tech_ids": duplicate_tech_ids,
        "duplicate_tech_id_note": "A Tech ID may legitimately appear on multiple public portfolio/listing pages; duplicates are preserved as source facts and are not treated as listing duplicates.",
        "missing_required": missing_required,
        "failures": failures,
        "page_receipts": pages,
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
        "semantic_parser_version": "2.0",
        "source_identifier_policy": "Preserve Vanderbilt Tech IDs when exposed; retain canonical URL slug as stable fallback provenance identifier.",
        "governance": {"corpus_tier": "discovery_pool", "canon_status": "not_promoted"},
        "files": ["vanderbilt_cttc_technologies.csv", "vanderbilt_cttc_technologies.jsonl", "validation.json", "manifest.json"],
        "harvested_at_utc": NOW,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(validation, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
