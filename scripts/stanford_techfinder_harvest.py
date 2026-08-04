#!/usr/bin/env python3
"""Fail-closed, source-complete public Stanford TechFinder catalog harvester."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://techfinder.stanford.edu/"
ROBOTS_URL = "https://techfinder.stanford.edu/robots.txt"
PAGE_SIZE = 15
USER_AGENT = (
    "Arns-Innovations-Stanford-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)
DOCKET_PATTERN = re.compile(r"\bS\d{2}-\d{3}[A-Z]?\b", flags=re.I)


@dataclass
class TechnologyRecord:
    institution: str
    catalog: str
    listing_page: int
    source_record_id: str
    source_listing_id: str
    title: str
    summary: str
    inventors: str
    official_record_url: str
    official_listing_url: str
    retrieved_at_utc: str
    dedup_key: str


@dataclass
class PageReceipt:
    page: int
    url: str
    http_status: int
    content_type: str
    raw_bytes: int
    sha256: str
    parsed_records: int
    listing_ids: str
    dockets: str
    retrieved_at_utc: str


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
                time.sleep(min(12.0, attempt * 2.0))
    assert last_error is not None
    raise last_error


def verify_robots(session: requests.Session, timeout: float) -> None:
    response = session.get(ROBOTS_URL, timeout=timeout)
    response.raise_for_status()
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines())
    if not parser.can_fetch(USER_AGENT, BASE_URL):
        raise RuntimeError(f"robots.txt does not permit fetching {BASE_URL}")


def parse_total_count(html: str) -> int:
    text = clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    matches = [
        int(value.replace(",", ""))
        for value in re.findall(
            r"Displaying\s+[0-9,]+\s*-\s*[0-9,]+\s+of\s+([0-9,]+)",
            text,
            flags=re.I,
        )
    ]
    if not matches:
        raise ValueError("Could not parse Stanford live result count")
    return max(matches)


def is_detail_href(href: str) -> bool:
    try:
        parsed = urlsplit(urljoin(BASE_URL, href))
    except Exception:
        return False
    path = parsed.path.rstrip("/")
    return path.startswith("/technology/") and path != "/technology"


def nearest_result_container(anchor: Tag) -> Tag:
    element: Tag | None = anchor
    while isinstance(element, Tag):
        text = clean(element.get_text(" ", strip=True))
        dockets = DOCKET_PATTERN.findall(text)
        detail_links = [
            link
            for link in element.find_all("a", href=True)
            if is_detail_href(link.get("href", ""))
        ]
        distinct_detail_urls = {
            urljoin(BASE_URL, link.get("href", "")) for link in detail_links
        }
        if len(set(value.upper() for value in dockets)) == 1 and len(distinct_detail_urls) == 1:
            return element
        element = element.parent if isinstance(element.parent, Tag) else None
    raise ValueError("Could not identify Stanford result-card container")


def extract_summary(container: Tag, title: str, docket: str, inventors: list[str]) -> str:
    candidates: list[str] = []
    for node in container.find_all(["p", "div"]):
        text = clean(node.get_text(" ", strip=True))
        if not text or text == title or text == docket:
            continue
        if DOCKET_PATTERN.fullmatch(text):
            continue
        if text in inventors:
            continue
        if text.lower().startswith("displaying ") or "pagination" in text.lower():
            continue
        if len(text) >= 40:
            candidates.append(text)
    if candidates:
        # Prefer a contained summary rather than a large wrapper that repeats the whole card.
        candidates.sort(key=lambda value: (value.count(title), len(value)))
        for value in candidates:
            if title not in value and len(value) < 2500:
                return value
        return min(candidates, key=len)
    return ""


def parse_records(html: str, page: int, page_url: str, retrieved_at: str) -> list[TechnologyRecord]:
    soup = BeautifulSoup(html, "lxml")
    records: dict[str, TechnologyRecord] = {}
    anchors = [
        anchor
        for anchor in soup.find_all("a", href=True)
        if is_detail_href(anchor.get("href", ""))
    ]

    for anchor in anchors:
        detail_url = urljoin(BASE_URL, anchor.get("href", ""))
        if detail_url in records:
            continue
        title = clean(anchor.get_text(" ", strip=True))
        if not title:
            continue
        container = nearest_result_container(anchor)
        container_text = clean(container.get_text(" ", strip=True))
        docket_matches = DOCKET_PATTERN.findall(container_text)
        docket_values = list(dict.fromkeys(value.upper() for value in docket_matches))
        if len(docket_values) != 1:
            raise ValueError(f"Expected one docket for Stanford listing {detail_url}; found {docket_values}")
        docket = docket_values[0]

        inventor_names: list[str] = []
        for link in container.find_all("a", href=True):
            href = link.get("href", "")
            text = clean(link.get_text(" ", strip=True))
            if not text or link is anchor or is_detail_href(href):
                continue
            absolute = urljoin(BASE_URL, href)
            if any(token in absolute for token in ["/innovator", "innovators", "people"]):
                inventor_names.append(text)
        inventor_names = list(dict.fromkeys(inventor_names))
        summary = extract_summary(container, title, docket, inventor_names)

        slug = urlsplit(detail_url).path.rstrip("/").split("/")[-1]
        listing_id = f"{docket}|{slug.lower()}"
        record = TechnologyRecord(
            institution="Stanford University",
            catalog="Stanford TechFinder",
            listing_page=page,
            source_record_id=docket,
            source_listing_id=listing_id,
            title=title,
            summary=summary,
            inventors=" / ".join(inventor_names),
            official_record_url=detail_url,
            official_listing_url=page_url,
            retrieved_at_utc=retrieved_at,
            dedup_key=f"stanford|{listing_id.lower()}",
        )
        records[detail_url] = record

    return list(records.values())


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="stanford_techfinder_harvest")
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--skip-robots-check", action="store_true")
    args = parser.parse_args()

    output = Path(args.output_dir)
    raw_dir = output / "raw_pages"
    output.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if not args.skip_robots_check:
        verify_robots(session, args.timeout)

    retrieved_at = datetime.now(timezone.utc).isoformat()
    first = fetch(session, BASE_URL, args.attempts, args.timeout)
    official_count = parse_total_count(first.text)
    expected_pages = math.ceil(official_count / PAGE_SIZE)

    listings: dict[str, TechnologyRecord] = {}
    receipts: list[PageReceipt] = []
    errors: list[dict[str, object]] = []

    for page in range(expected_pages):
        page_url = BASE_URL if page == 0 else f"{BASE_URL}?page={page}"
        try:
            response = first if page == 0 else fetch(session, page_url, args.attempts, args.timeout)
            raw = response.content
            (raw_dir / f"page_{page:03d}.html").write_bytes(raw)
            page_records = parse_records(response.text, page, page_url, retrieved_at)
            expected_page_records = (
                PAGE_SIZE if page < expected_pages - 1 else official_count - PAGE_SIZE * (expected_pages - 1)
            )
            if len(page_records) != expected_page_records:
                raise ValueError(
                    f"Page {page} parsed {len(page_records)} records; expected {expected_page_records}"
                )

            for record in page_records:
                key = record.official_record_url.lower()
                if key in listings and (
                    listings[key].source_record_id != record.source_record_id
                    or listings[key].title != record.title
                ):
                    raise ValueError(f"Conflicting Stanford listing identity {key}")
                listings[key] = record

            receipts.append(PageReceipt(
                page=page,
                url=page_url,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                raw_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                parsed_records=len(page_records),
                listing_ids="|".join(record.source_listing_id for record in page_records),
                dockets="|".join(record.source_record_id for record in page_records),
                retrieved_at_utc=retrieved_at,
            ))
            print(
                f"page={page:03d} records={len(page_records):02d} unique_listings={len(listings)}",
                flush=True,
            )
        except Exception as exc:
            errors.append({"page": page, "url": page_url, "error": repr(exc)})
            print(f"page={page:03d} ERROR {exc!r}", file=sys.stderr, flush=True)
        if page + 1 < expected_pages:
            time.sleep(max(0.0, args.delay))

    record_rows = [asdict(record) for record in sorted(listings.values(), key=lambda row: row.source_listing_id)]
    receipt_rows = [asdict(receipt) for receipt in sorted(receipts, key=lambda row: row.page)]
    required_missing = sum(
        1 for row in record_rows
        if not row["source_record_id"] or not row["source_listing_id"] or not row["title"] or not row["official_record_url"]
    )
    listing_id_counter = Counter(row["source_listing_id"].lower() for row in record_rows)
    docket_counter = Counter(row["source_record_id"] for row in record_rows)
    duplicate_listing_ids = {key: count for key, count in listing_id_counter.items() if count > 1}
    duplicate_dockets = {key: count for key, count in docket_counter.items() if count > 1}

    write_csv(output / "stanford_techfinder_records.csv", record_rows, list(TechnologyRecord.__dataclass_fields__))
    write_csv(output / "stanford_techfinder_page_receipts.csv", receipt_rows, list(PageReceipt.__dataclass_fields__))
    write_csv(output / "stanford_techfinder_errors.csv", errors, ["page", "url", "error"])
    (output / "stanford_techfinder_records.json").write_text(
        json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "stanford_techfinder_page_receipts.json").write_text(
        json.dumps(receipt_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "stanford_techfinder_duplicate_dockets.json").write_text(
        json.dumps(duplicate_dockets, indent=2), encoding="utf-8"
    )

    manifest = {
        "institution": "Stanford University",
        "catalog": "Stanford TechFinder",
        "catalog_url": BASE_URL,
        "retrieved_at_utc": retrieved_at,
        "official_live_result_count": official_count,
        "page_size": PAGE_SIZE,
        "expected_pages": expected_pages,
        "pages_with_receipts": len(receipts),
        "page_errors": len(errors),
        "unique_listings": len(record_rows),
        "unique_official_record_urls": len({row["official_record_url"] for row in record_rows}),
        "unique_dockets": len(docket_counter),
        "duplicate_docket_groups": len(duplicate_dockets),
        "duplicate_listing_identities": len(duplicate_listing_ids),
        "required_fields_missing": required_missing,
        "source_complete": (
            len(record_rows) == official_count
            and len(receipts) == expected_pages
            and not errors
            and not duplicate_listing_ids
            and required_missing == 0
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (output / "stanford_techfinder_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)

    if not manifest["source_complete"]:
        print("STANFORD SOURCE INCOMPLETE: aggregate admission is not authorized.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
