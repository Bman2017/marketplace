#!/usr/bin/env python3
"""Fail-closed, source-complete public MIT TLO catalog harvester."""
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

BASE_URL = "https://tlo.mit.edu/industry-entrepreneurs/available-technologies"
ROBOTS_URL = "https://tlo.mit.edu/robots.txt"
PAGE_SIZE = 20
USER_AGENT = (
    "Arns-Innovations-MIT-Public-Catalog-Harvest/1.2 "
    "(public metadata; source URLs and receipts retained)"
)


@dataclass
class TechnologyRecord:
    institution: str
    catalog: str
    listing_page: int
    source_record_id: str
    source_listing_id: str
    title: str
    license_or_invention_status: str
    inventors: str
    technology_areas: str
    impact_areas: str
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
    case_numbers: str
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


def section(text: str, start_label: str, end_label: str) -> str:
    start = text.lower().find(start_label.lower())
    if start < 0:
        raise ValueError(f"Could not find MIT facet section {start_label!r}")
    tail = text[start + len(start_label):]
    end = tail.lower().find(end_label.lower())
    return tail if end < 0 else tail[:end]


def parse_official_counts(html: str) -> tuple[int, dict[str, int], dict[str, int]]:
    text = clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))

    license_text = section(text, "License Status", "Technology Area")
    license_patterns = {
        "unlicensed": r"\bUnlicensed\s+([0-9][0-9,]*)\b",
        "exclusively_licensed": r"\bExclusively Licensed\s+([0-9][0-9,]*)\b",
        "non_exclusively_licensed": r"\bNon-Exclusively Licensed\s+([0-9][0-9,]*)\b",
    }
    license_counts: dict[str, int] = {}
    for key, pattern in license_patterns.items():
        match = re.search(pattern, license_text, flags=re.I)
        if not match:
            raise ValueError(f"Could not parse MIT live license count for {key}")
        license_counts[key] = int(match.group(1).replace(",", ""))

    invention_text = section(text, "Invention Type", "Protection Type")
    invention_patterns = {
        "software": r"\bSoftware\s+([0-9][0-9,]*)\b",
        "tangible_property": r"\bTangible Property\s+([0-9][0-9,]*)\b",
        "technology": r"\bTechnology\s+([0-9][0-9,]*)\b",
    }
    invention_counts: dict[str, int] = {}
    for key, pattern in invention_patterns.items():
        match = re.search(pattern, invention_text, flags=re.I)
        if not match:
            raise ValueError(f"Could not parse MIT live invention-type count for {key}")
        invention_counts[key] = int(match.group(1).replace(",", ""))

    official_listing_count = sum(invention_counts.values())
    if sum(license_counts.values()) != invention_counts["technology"]:
        raise ValueError(
            "MIT facet disagreement: license-status total does not equal Technology count"
        )
    return official_listing_count, invention_counts, license_counts


def text_of(node: Tag | None) -> str:
    return clean(node.get_text(" ", strip=True) if node is not None else "")


def parse_records(html: str, page: int, page_url: str, retrieved_at: str) -> list[TechnologyRecord]:
    soup = BeautifulSoup(html, "lxml")
    records: list[TechnologyRecord] = []

    for row in soup.select(".views-row"):
        card = row.select_one(".tech-brief-teaser") or row
        details = card.select_one(".tech-brief-teaser__details-text")
        heading = card.select_one(".tech-brief-teaser__heading")
        if details is None or heading is None:
            continue

        details_text = text_of(details)
        case_matches = re.findall(r"#\s*([A-Za-z0-9-]+)", details_text)
        if not case_matches:
            continue
        case_number = case_matches[-1].upper()

        link = heading.find("a", href=True)
        title = text_of(heading)
        detail_url = urljoin(page_url, link.get("href", "")) if link else ""
        listing_slug = urlsplit(detail_url).path.rstrip("/").split("/")[-1] if detail_url else ""
        if not listing_slug:
            listing_slug = hashlib.sha256(f"{case_number}|{title}".encode("utf-8")).hexdigest()[:20]
        listing_id = f"{case_number}|{listing_slug.lower()}"

        status = clean(details_text.split("/", 1)[0])
        inventors = text_of(card.select_one(".tech-brief-teaser__reseachers"))
        technology_areas = text_of(card.select_one(".tech-brief-teaser__categories--tech-areas"))
        impact_areas = text_of(card.select_one(".tech-brief-teaser__categories--impact-areas"))
        technology_areas = re.sub(r"^Technology Areas:\s*", "", technology_areas, flags=re.I)
        impact_areas = re.sub(r"^Impact Areas:\s*", "", impact_areas, flags=re.I)

        if not title:
            raise ValueError(f"MIT listing {listing_id} has no title on page {page}")

        records.append(
            TechnologyRecord(
                institution="Massachusetts Institute of Technology",
                catalog="MIT Technology Licensing Office",
                listing_page=page,
                source_record_id=case_number,
                source_listing_id=listing_id,
                title=title,
                license_or_invention_status=status,
                inventors=inventors,
                technology_areas=technology_areas,
                impact_areas=impact_areas,
                official_record_url=detail_url,
                official_listing_url=page_url,
                retrieved_at_utc=retrieved_at,
                dedup_key=f"mit|{listing_id.lower()}",
            )
        )
    return records


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="mit_tlo_harvest")
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
    official_count, invention_counts, license_counts = parse_official_counts(first.text)
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
                PAGE_SIZE
                if page < expected_pages - 1
                else official_count - PAGE_SIZE * (expected_pages - 1)
            )
            if len(page_records) != expected_page_records:
                raise ValueError(
                    f"Page {page} parsed {len(page_records)} listings; expected {expected_page_records}"
                )

            for record in page_records:
                key = record.source_listing_id.lower()
                if key in listings:
                    prior = listings[key]
                    if prior.title != record.title or prior.source_record_id != record.source_record_id:
                        raise ValueError(f"Conflicting MIT listing identity {key}")
                listings[key] = record

            receipts.append(
                PageReceipt(
                    page=page,
                    url=page_url,
                    http_status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    raw_bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    parsed_records=len(page_records),
                    listing_ids="|".join(record.source_listing_id for record in page_records),
                    case_numbers="|".join(record.source_record_id for record in page_records),
                    retrieved_at_utc=retrieved_at,
                )
            )
            print(
                f"page={page:03d} listings={len(page_records):02d} unique_listings={len(listings)}",
                flush=True,
            )
        except Exception as exc:
            errors.append({"page": page, "url": page_url, "error": repr(exc)})
            print(f"page={page:03d} ERROR {exc!r}", file=sys.stderr, flush=True)
        if page + 1 < expected_pages:
            time.sleep(max(0.0, args.delay))

    record_rows = [asdict(record) for record in sorted(listings.values(), key=lambda r: r.source_listing_id)]
    receipt_rows = [asdict(receipt) for receipt in sorted(receipts, key=lambda r: r.page)]
    required_missing = sum(
        1
        for row in record_rows
        if not row["source_record_id"] or not row["source_listing_id"] or not row["title"]
    )
    case_counter = Counter(row["source_record_id"] for row in record_rows)
    duplicate_case_numbers = {
        case_number: count for case_number, count in case_counter.items() if count > 1
    }

    write_csv(output / "mit_tlo_records.csv", record_rows, list(TechnologyRecord.__dataclass_fields__))
    write_csv(output / "mit_tlo_page_receipts.csv", receipt_rows, list(PageReceipt.__dataclass_fields__))
    write_csv(output / "mit_tlo_errors.csv", errors, ["page", "url", "error"])
    (output / "mit_tlo_records.json").write_text(
        json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "mit_tlo_page_receipts.json").write_text(
        json.dumps(receipt_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "mit_tlo_duplicate_case_numbers.json").write_text(
        json.dumps(duplicate_case_numbers, indent=2), encoding="utf-8"
    )

    manifest = {
        "institution": "Massachusetts Institute of Technology",
        "catalog_url": BASE_URL,
        "retrieved_at_utc": retrieved_at,
        "official_listing_count": official_count,
        "invention_type_counts": invention_counts,
        "license_status_counts": license_counts,
        "page_size": PAGE_SIZE,
        "expected_pages": expected_pages,
        "pages_with_receipts": len(receipts),
        "page_errors": len(errors),
        "unique_listings": len(record_rows),
        "unique_case_numbers": len(case_counter),
        "duplicate_case_number_groups": len(duplicate_case_numbers),
        "duplicate_case_number_rows": sum(duplicate_case_numbers.values()),
        "required_fields_missing": required_missing,
        "source_complete": (
            len(record_rows) == official_count
            and len(receipts) == expected_pages
            and not errors
            and required_missing == 0
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (output / "mit_tlo_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)

    if not manifest["source_complete"]:
        print("MIT SOURCE INCOMPLETE: aggregate admission is not authorized.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
