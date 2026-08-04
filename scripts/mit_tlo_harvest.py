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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://tlo.mit.edu/industry-entrepreneurs/available-technologies"
ROBOTS_URL = "https://tlo.mit.edu/robots.txt"
PAGE_SIZE = 20
USER_AGENT = (
    "Arns-Innovations-MIT-Public-Catalog-Harvest/1.1 "
    "(public metadata; source URLs and receipts retained)"
)


@dataclass
class TechnologyRecord:
    institution: str
    catalog: str
    listing_page: int
    source_record_id: str
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


def parse_official_count(html: str) -> tuple[int, dict[str, int]]:
    text = clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    patterns = {
        "unlicensed": r"(?<!Non-)\bUnlicensed\s+([0-9][0-9,]*)\b",
        "exclusively_licensed": r"(?<!Non-)\bExclusively Licensed\s+([0-9][0-9,]*)\b",
        "non_exclusively_licensed": r"\bNon-Exclusively Licensed\s+([0-9][0-9,]*)\b",
    }
    counts: dict[str, int] = {}
    for key, pattern in patterns.items():
        matches = [int(v.replace(",", "")) for v in re.findall(pattern, text, flags=re.I)]
        if not matches:
            raise ValueError(f"Could not parse MIT live count for {key}")
        counts[key] = max(matches)
    official_count = sum(counts.values())
    technology_matches = [
        int(v.replace(",", ""))
        for v in re.findall(r"\bTechnology\s+([0-9][0-9,]*)\b", text, flags=re.I)
    ]
    if technology_matches:
        technology_total = max(technology_matches)
        if technology_total != official_count:
            raise ValueError(
                f"MIT count disagreement: status facets={official_count}, Technology facet={technology_total}"
            )
    return official_count, counts


def record_containers(soup: BeautifulSoup) -> list[Tag]:
    rows = [row for row in soup.select(".views-row") if "Case number:" in row.get_text(" ", strip=True)]
    if rows:
        return rows

    candidates: list[Tag] = []
    seen: set[int] = set()
    for node in soup.find_all(string=re.compile(r"Case number:", flags=re.I)):
        element = node.parent
        while isinstance(element, Tag):
            text = clean(element.get_text(" ", strip=True))
            cases = re.findall(r"Case number:\s*#?\s*([A-Za-z0-9-]+)", text, flags=re.I)
            heading = element.find(["h2", "h3", "h4"])
            if len(cases) == 1 and heading is not None:
                marker = id(element)
                if marker not in seen:
                    seen.add(marker)
                    candidates.append(element)
                break
            element = element.parent
    return candidates


def value_between(text: str, start_pattern: str, end_pattern: str) -> str:
    start = re.search(start_pattern, text, flags=re.I)
    if not start:
        return ""
    tail = text[start.end():]
    end = re.search(end_pattern, tail, flags=re.I)
    if end:
        tail = tail[:end.start()]
    return clean(tail.strip(" :/|-"))


def parse_records(html: str, page: int, page_url: str, retrieved_at: str) -> list[TechnologyRecord]:
    soup = BeautifulSoup(html, "lxml")
    records: dict[str, TechnologyRecord] = {}

    for container in record_containers(soup):
        text = clean(container.get_text(" ", strip=True))
        case_match = re.search(r"Case number:\s*#?\s*([A-Za-z0-9-]+)", text, flags=re.I)
        if not case_match:
            continue
        case_number = case_match.group(1).upper()

        heading = None
        for candidate in container.find_all(["h2", "h3", "h4"]):
            candidate_link = candidate.find("a", href=True)
            if candidate_link and "available-technologies/" in candidate_link.get("href", ""):
                heading = candidate
                break
        if heading is None:
            heading = container.find(["h2", "h3", "h4"])
        link = heading.find("a", href=True) if heading else None
        title = clean(heading.get_text(" ", strip=True) if heading else "")
        detail_url = urljoin(page_url, link.get("href", "")) if link else ""

        prefix = text[:case_match.start()]
        status_matches = re.findall(
            r"(?:New\s+)?(Non-Exclusively Licensed Technology|Exclusively Licensed Technology|Tangible Property|Software|Technology)",
            prefix,
            flags=re.I,
        )
        status = clean(status_matches[-1] if status_matches else "")
        inventors = value_between(
            text,
            r"Case number:\s*#?\s*[A-Za-z0-9-]+",
            r"Technology Areas:|Impact Areas:|Labels|License",
        )
        technology_areas = value_between(
            text,
            r"Technology Areas:",
            r"Impact Areas:|Labels|License",
        )
        impact_areas = value_between(
            text,
            r"Impact Areas:",
            r"Labels|License",
        )

        if not title:
            raise ValueError(f"MIT case {case_number} has no title on page {page}")
        record = TechnologyRecord(
            institution="Massachusetts Institute of Technology",
            catalog="MIT Technology Licensing Office",
            listing_page=page,
            source_record_id=case_number,
            title=title,
            license_or_invention_status=status,
            inventors=inventors,
            technology_areas=technology_areas,
            impact_areas=impact_areas,
            official_record_url=detail_url,
            official_listing_url=page_url,
            retrieved_at_utc=retrieved_at,
            dedup_key=f"mit|{case_number.lower()}",
        )
        if case_number in records and records[case_number].title != title:
            raise ValueError(f"Conflicting titles for MIT case {case_number} on page {page}")
        records[case_number] = record

    return list(records.values())


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
    official_count, license_counts = parse_official_count(first.text)
    expected_pages = math.ceil(official_count / PAGE_SIZE)

    all_records: dict[str, TechnologyRecord] = {}
    receipts: list[PageReceipt] = []
    errors: list[dict[str, object]] = []

    for page in range(expected_pages):
        page_url = BASE_URL if page == 0 else f"{BASE_URL}?page={page}"
        try:
            response = first if page == 0 else fetch(session, page_url, args.attempts, args.timeout)
            raw = response.content
            (raw_dir / f"page_{page:03d}.html").write_bytes(raw)
            page_records = parse_records(response.text, page, page_url, retrieved_at)
            expected_page_records = PAGE_SIZE if page < expected_pages - 1 else official_count - PAGE_SIZE * (expected_pages - 1)
            if len(page_records) != expected_page_records:
                raise ValueError(
                    f"Page {page} parsed {len(page_records)} records; expected {expected_page_records}"
                )
            receipts.append(PageReceipt(
                page=page,
                url=page_url,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                raw_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                parsed_records=len(page_records),
                case_numbers="|".join(record.source_record_id for record in page_records),
                retrieved_at_utc=retrieved_at,
            ))
            for record in page_records:
                key = record.source_record_id
                if key in all_records and all_records[key].title != record.title:
                    raise ValueError(
                        f"Case {key} conflicts across pages: {all_records[key].title!r} vs {record.title!r}"
                    )
                all_records[key] = record
            print(f"page={page:03d} records={len(page_records):02d} unique_total={len(all_records)}", flush=True)
        except Exception as exc:
            errors.append({"page": page, "url": page_url, "error": repr(exc)})
            print(f"page={page:03d} ERROR {exc!r}", file=sys.stderr, flush=True)
        if page + 1 < expected_pages:
            time.sleep(max(0.0, args.delay))

    record_rows = [asdict(record) for record in sorted(all_records.values(), key=lambda r: r.source_record_id)]
    receipt_rows = [asdict(receipt) for receipt in sorted(receipts, key=lambda r: r.page)]
    required_missing = sum(1 for row in record_rows if not row["source_record_id"] or not row["title"])
    duplicate_count = sum(1 for key in {row["source_record_id"] for row in record_rows} if sum(1 for r in record_rows if r["source_record_id"] == key) > 1)

    write_csv(output / "mit_tlo_records.csv", record_rows, list(TechnologyRecord.__dataclass_fields__))
    write_csv(output / "mit_tlo_page_receipts.csv", receipt_rows, list(PageReceipt.__dataclass_fields__))
    write_csv(output / "mit_tlo_errors.csv", errors, ["page", "url", "error"])
    (output / "mit_tlo_records.json").write_text(json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "mit_tlo_page_receipts.json").write_text(json.dumps(receipt_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "institution": "Massachusetts Institute of Technology",
        "catalog_url": BASE_URL,
        "retrieved_at_utc": retrieved_at,
        "official_count": official_count,
        "license_status_counts": license_counts,
        "page_size": PAGE_SIZE,
        "expected_pages": expected_pages,
        "pages_with_receipts": len(receipts),
        "page_errors": len(errors),
        "unique_case_numbers": len(record_rows),
        "required_fields_missing": required_missing,
        "duplicate_case_numbers": duplicate_count,
        "source_complete": (
            len(record_rows) == official_count
            and len(receipts) == expected_pages
            and not errors
            and required_missing == 0
            and duplicate_count == 0
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (output / "mit_tlo_source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)

    if not manifest["source_complete"]:
        print("MIT SOURCE INCOMPLETE: aggregate admission is not authorized.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
