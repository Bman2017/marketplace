#!/usr/bin/env python3
"""Source-complete public Oxford University Innovation licensing-catalog harvest."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://innovation.ox.ac.uk"
CATALOG = f"{BASE}/licensing-opportunities/browse-innovations"
ROBOTS = f"{BASE}/robots.txt"
USER_AGENT = (
    "Arns-Innovations-Oxford-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)
EXPECTED_DIRECTORY_ROWS = 207


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def fetch(session: requests.Session, url: str, attempts: int, timeout: float) -> requests.Response:
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


def first_image_url(node: Tag, page_url: str) -> str:
    image = node.find("img", src=True)
    return urljoin(page_url, image.get("src", "")) if image else ""


def parse_directory(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    rows = []
    for index, row in enumerate(soup.select(".views-row.oui-automated-teasers-licenses__item"), start=1):
        link = row.select_one("a.node--type-oui-license[href]")
        title_node = row.select_one("h3")
        summary_node = row.select_one(".field--name-field-oui-introductory-text")
        if link is None or title_node is None:
            continue
        url = urljoin(CATALOG, link.get("href", ""))
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        rows.append({
            "directory_position": index,
            "source_listing_id": slug,
            "directory_title": clean(title_node.get_text(" ", strip=True)),
            "directory_summary": clean(summary_node.get_text(" ", strip=True)) if summary_node else "",
            "official_record_url": url,
            "directory_image_url": first_image_url(row, CATALOG),
        })
    return rows


def text_after_heading(soup: BeautifulSoup, pattern: str) -> str:
    heading = soup.find(["h2", "h3", "h4", "strong"], string=re.compile(pattern, flags=re.I))
    if not heading:
        return ""
    values: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
            break
        if isinstance(sibling, Tag):
            text = clean(sibling.get_text(" ", strip=True))
            if text:
                values.append(text)
    return clean(" ".join(values))


def list_after_heading(soup: BeautifulSoup, pattern: str) -> list[str]:
    heading = soup.find(["h2", "h3", "h4", "strong"], string=re.compile(pattern, flags=re.I))
    if not heading:
        return []
    values: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
            break
        if isinstance(sibling, Tag):
            for item in sibling.find_all("li"):
                value = clean(item.get_text(" ", strip=True))
                if value:
                    values.append(value)
            if not values:
                text = clean(sibling.get_text(" ", strip=True))
                if text:
                    values.append(text)
    return list(dict.fromkeys(values))


def institution_signals(text: str) -> tuple[bool, list[str]]:
    oxford = bool(re.search(
        r"University of Oxford|Oxford researchers?|Oxford scientists?|Oxford academics?|Oxford team|developed at Oxford|created by Oxford",
        text,
        flags=re.I,
    ))
    candidates = []
    patterns = [
        r"University of (?!Oxford\b)[A-Z][A-Za-z& .'-]{2,80}",
        r"[A-Z][A-Za-z& .'-]{2,60} University",
        r"[A-Z][A-Za-z& .'-]{2,60} Institute of Technology",
        r"[A-Z][A-Za-z& .'-]{2,60} College London",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            value = clean(match).rstrip(".,;:")
            if value and "Oxford University Innovation" not in value:
                candidates.append(value)
    return oxford, sorted(set(candidates))


def parse_detail(html_text: str, official_url: str, directory_row: dict) -> dict:
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find("main") or soup.body or soup
    full_text = clean(main.get_text(" ", strip=True))
    h1 = main.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else directory_row["directory_title"]

    intro_node = main.select_one(".field--name-field-oui-introductory-text")
    introduction = clean(intro_node.get_text(" ", strip=True)) if intro_node else directory_row["directory_summary"]

    project_match = re.search(
        r"Project Number:\s*(.+?)(?=\s+Industry Categories|\s+External Links|\s+Request more information|\s+Building a better future|$)",
        full_text,
        flags=re.I,
    )
    project_number = clean(project_match.group(1)) if project_match else ""

    applications_match = re.search(
        r"Applications:\s*(.+?)(?=\s+Features Benefits|\s+Patented and Available|\s+Available For|\s+External Links|\s+Project Number:|$)",
        full_text,
        flags=re.I,
    )
    applications = clean(applications_match.group(1)) if applications_match else ""

    availability = list_after_heading(soup, r"Available For|Patented and Available For")
    categories_text = text_after_heading(soup, r"Industry Categories")
    categories = clean(categories_text.split("Request more information", 1)[0])

    feature_rows = []
    for table in main.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [clean(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            if any(cells):
                feature_rows.append(" | ".join(cells))

    external_links = []
    for link in main.find_all("a", href=True):
        href = urljoin(official_url, link.get("href", ""))
        label = clean(link.get_text(" ", strip=True))
        if urlparse(href).netloc and urlparse(href).netloc != urlparse(BASE).netloc:
            if label and not any(token in label.lower() for token in ["linkedin", "youtube", "website by"]):
                external_links.append(f"{label} | {href}")

    detail_image = first_image_url(main, official_url)
    explicit_oxford, partner_candidates = institution_signals(full_text)
    if explicit_oxford and partner_candidates:
        attribution = "University of Oxford — collaborative or externally referenced"
    elif explicit_oxford:
        attribution = "University of Oxford — explicit public evidence"
    elif partner_candidates:
        attribution = "Partner/unresolved — non-Oxford institution reference"
    else:
        attribution = "Oxford University Innovation managed — ownership not explicit on page"

    return {
        "title": title,
        "introduction": introduction,
        "project_number": project_number,
        "applications": applications,
        "availability": " / ".join(availability),
        "industry_categories": categories,
        "features_benefits": " || ".join(feature_rows),
        "external_links": " || ".join(dict.fromkeys(external_links)),
        "detail_image_url": detail_image,
        "detail_text": full_text,
        "attribution_status": attribution,
        "explicit_oxford_signal": explicit_oxford,
        "partner_institution_candidates": " / ".join(partner_candidates),
    }


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="oxford-oui-harvest")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()

    out = Path(args.output_dir)
    raw_details = out / "raw_detail_pages"
    out.mkdir(parents=True, exist_ok=True)
    raw_details.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    })

    robots_response = fetch(session, ROBOTS, args.attempts, args.timeout)
    robots = RobotFileParser()
    robots.parse(robots_response.text.splitlines())
    if not robots.can_fetch(USER_AGENT, CATALOG) or not robots.can_fetch(USER_AGENT, f"{BASE}/licence-details/example"):
        raise RuntimeError("Oxford robots.txt does not permit the public catalog harvest")
    (out / "robots.txt").write_bytes(robots_response.content)

    directory_response = fetch(session, CATALOG, args.attempts, args.timeout)
    directory_raw = directory_response.content
    (out / "catalog_directory.html").write_bytes(directory_raw)
    directory_rows = parse_directory(directory_response.text)
    if len(directory_rows) != EXPECTED_DIRECTORY_ROWS:
        raise ValueError(f"Oxford directory returned {len(directory_rows)} records; expected {EXPECTED_DIRECTORY_ROWS}")
    if len({row['official_record_url'] for row in directory_rows}) != EXPECTED_DIRECTORY_ROWS:
        raise ValueError("Oxford directory contains duplicate official URLs")
    if len({row['source_listing_id'] for row in directory_rows}) != EXPECTED_DIRECTORY_ROWS:
        raise ValueError("Oxford directory contains duplicate listing identities")

    directory_receipt = {
        "receipt_type": "catalog_directory",
        "source_index": 1,
        "url": CATALOG,
        "http_status": directory_response.status_code,
        "content_type": directory_response.headers.get("content-type", ""),
        "raw_bytes": len(directory_raw),
        "sha256": hashlib.sha256(directory_raw).hexdigest(),
        "parsed_records": len(directory_rows),
        "retrieved_at_utc": retrieved,
    }

    errors = []
    detail_results: dict[int, tuple[requests.Response, dict]] = {}

    def detail_job(pair):
        index, row = pair
        local = requests.Session(); local.headers.update(session.headers)
        response = fetch(local, row["official_record_url"], args.attempts, args.timeout)
        parsed = parse_detail(response.text, row["official_record_url"], row)
        return index, response, parsed

    indexed = list(enumerate(directory_rows, start=1))
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(detail_job, pair): pair for pair in indexed}
        for completed, future in enumerate(as_completed(futures), start=1):
            index, row = futures[future]
            try:
                result_index, response, parsed = future.result()
                detail_results[result_index] = (response, parsed)
            except Exception as exc:
                errors.append({
                    "stage": "detail_fetch",
                    "source_index": index,
                    "url": row["official_record_url"],
                    "error": repr(exc),
                })
            if completed % 25 == 0 or completed == len(indexed):
                print(f"detail_completed={completed} success={len(detail_results)} errors={len(errors)}", flush=True)

    records = []
    receipts = [directory_receipt]
    for index, row in indexed:
        if index not in detail_results:
            continue
        response, parsed = detail_results[index]
        raw = response.content
        raw_path = raw_details / f"{index:03d}_{row['source_listing_id']}.html"
        raw_path.write_bytes(raw)
        receipts.append({
            "receipt_type": "detail_page",
            "source_index": index,
            "url": row["official_record_url"],
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parsed_records": 1,
            "retrieved_at_utc": retrieved,
        })
        project_or_slug = parsed["project_number"] or row["source_listing_id"]
        records.append({
            "institution_requested": "University of Oxford",
            "source_organization": "Oxford University Innovation",
            "catalog": "Oxford University Innovation Browse innovations",
            **row,
            **parsed,
            "retrieved_at_utc": retrieved,
            "detail_fetch_status": response.status_code,
            "source_completion_status": "SOURCE COMPLETE",
            "dedup_key": f"oxford|{project_or_slug.lower()}|{row['source_listing_id'].lower()}",
        })

    required_missing = sum(
        1 for row in records
        if not row["source_listing_id"] or not row["title"] or not row["official_record_url"]
    )
    listing_ids = Counter(row["source_listing_id"].lower() for row in records)
    urls = Counter(row["official_record_url"] for row in records)
    project_numbers = Counter(row["project_number"] for row in records if row["project_number"])
    duplicate_projects = {key: value for key, value in project_numbers.items() if value > 1}
    attribution_counts = Counter(row["attribution_status"] for row in records)

    source_complete = (
        len(records) == EXPECTED_DIRECTORY_ROWS
        and len(detail_results) == EXPECTED_DIRECTORY_ROWS
        and len(receipts) == EXPECTED_DIRECTORY_ROWS + 1
        and not errors
        and required_missing == 0
        and all(value == 1 for value in listing_ids.values())
        and all(value == 1 for value in urls.values())
    )

    fields = list(records[0].keys())
    write_csv(out / "oxford_oui_records.csv", records, fields)
    write_csv(out / "oxford_oui_receipts.csv", receipts, list(receipts[0].keys()))
    write_csv(out / "oxford_oui_errors.csv", errors, ["stage", "source_index", "url", "error"])
    partner_review = [
        row for row in records
        if row["partner_institution_candidates"]
        or row["attribution_status"].startswith("Partner/unresolved")
    ]
    write_csv(out / "oxford_oui_attribution_review.csv", partner_review, fields)
    (out / "oxford_oui_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "oxford_oui_duplicate_project_numbers.json").write_text(
        json.dumps(duplicate_projects, indent=2), encoding="utf-8"
    )

    manifest = {
        "institution_requested": "University of Oxford",
        "source_organization": "Oxford University Innovation",
        "catalog_url": CATALOG,
        "retrieved_at_utc": retrieved,
        "official_live_directory_records": EXPECTED_DIRECTORY_ROWS,
        "normalized_listing_records": len(records),
        "unique_listing_identities": len(listing_ids),
        "unique_official_record_urls": len(urls),
        "detail_pages_with_receipts": len(detail_results),
        "receipt_count": len(receipts),
        "required_fields_missing": required_missing,
        "duplicate_project_number_groups": duplicate_projects,
        "attribution_counts": dict(attribution_counts),
        "attribution_review_records": len(partner_review),
        "errors": len(errors),
        "robots_allowed": True,
        "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "scope_note": (
            "This certifies the complete live Oxford University Innovation public licensing directory. "
            "Oxford University Innovation may market selected opportunities for partner institutions, so "
            "record-level attribution evidence is preserved separately from source enumeration."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (out / "oxford_oui_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)

    if not source_complete:
        print("OXFORD SOURCE INCOMPLETE", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
