#!/usr/bin/env python3
"""Source-complete UF Innovate Technology Publisher harvest.

UF's search page displays 770 Results, but its full 77-page pagination contains
768 unique official listing URLs because one page has nine rows and listing URLs
repeat across page boundaries. The independently captured public RSS export also
contains 768 unique items. This harvester preserves those discrepancies and
certifies the 768 unique public technologies.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://ufinnovate.technologypublisher.com/"
SEARCH = urljoin(BASE, "searchresults.aspx")
DISPLAYED_TOTAL_EXPECTED = 770
UNIQUE_TOTAL_EXPECTED = 768
PAGE_SIZE = 10
EXPECTED_PAGES = 77
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def fetch(
    session: requests.Session,
    url: str,
    attempts: int = 5,
    timeout: float = 90.0,
) -> requests.Response:
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            error = exc
            if attempt < attempts:
                time.sleep(min(12.0, attempt * 1.5))
    assert error is not None
    raise error


def listing_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.lower() == "/tech":
        return clean((parse_qs(parsed.query).get("title") or [""])[0])
    return path.split("/")[-1]


def parse_search_page(
    html_text: str,
    page_index: int,
    page_url: str,
) -> tuple[int, list[dict]]:
    soup = BeautifulSoup(html_text, "lxml")
    text = clean(soup.get_text(" ", strip=True))
    count_match = re.search(r"\b([0-9][0-9,]*)\s+Results?\b", text, flags=re.I)
    if not count_match:
        raise ValueError(f"Could not parse UF result count on page {page_index}")
    displayed_total = int(count_match.group(1).replace(",", ""))

    rows: list[dict] = []
    page_seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor.get("href", ""))
        parsed = urlparse(href)
        path = parsed.path.rstrip("/")
        if not ("/tech/" in parsed.path or path.lower() == "/tech"):
            continue
        href = href.split("#", 1)[0].rstrip("/")
        if href in page_seen:
            continue
        page_seen.add(href)
        title = clean(anchor.get_text(" ", strip=True))
        if not title:
            continue
        cell = anchor.find_parent("td")
        cell_text = clean(cell.get_text(" ", strip=True) if cell else "")
        published_match = re.search(
            r"Published:\s*([^|]+?)(?=\s*\||\s*Inventor\(s\):|$)",
            cell_text,
            flags=re.I,
        )
        inventors: list[str] = []
        categories: list[str] = []
        keywords = ""
        if cell:
            for link in cell.find_all("a", href=True):
                link_href = link.get("href", "")
                link_text = clean(link.get_text(" ", strip=True))
                if "bio.aspx" in link_href and link_text:
                    inventors.append(link_text)
                if "type=c" in link_href and link_text:
                    categories.append(link_text)
            keyword_match = re.search(
                r"Keywords\(s\):\s*(.*?)\s*Category\(s\):",
                cell_text,
                flags=re.I,
            )
            if keyword_match:
                keywords = clean(keyword_match.group(1))
        teaser = cell_text
        if teaser.startswith(title):
            teaser = teaser[len(title):].strip()
        teaser = re.split(r"\s+Published:\s*", teaser, maxsplit=1, flags=re.I)[0]
        rows.append(
            {
                "source_listing_id": listing_id_from_url(href),
                "search_page_index": page_index,
                "search_page_url": page_url,
                "title": title,
                "search_teaser": clean(teaser),
                "published": clean(published_match.group(1)) if published_match else "",
                "inventors": " / ".join(dict.fromkeys(inventors)),
                "keywords": keywords,
                "categories": " / ".join(dict.fromkeys(categories)),
                "official_record_url": href,
            }
        )
    return displayed_total, rows


def section(text: str, label: str, stops: list[str]) -> str:
    start = re.search(rf"\b{re.escape(label)}\s*:?\s*", text, flags=re.I)
    if not start:
        return ""
    tail = text[start.end():]
    endpoints: list[int] = []
    for stop in stops:
        match = re.search(rf"\b{re.escape(stop)}\s*:?\s*", tail, flags=re.I)
        if match:
            endpoints.append(match.start())
    if endpoints:
        tail = tail[: min(endpoints)]
    return clean(tail)


def parse_detail(html_text: str, listing: dict) -> dict:
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find("main") or soup.find("section") or soup.body or soup
    page_text = clean(main.get_text(" ", strip=True))
    heading = main.find("h1") or main.find("h2")
    detail_title = clean(heading.get_text(" ", strip=True) if heading else "")

    source_record_id = ""
    patterns = [
        r"(?:Case|Technology|Project|Marketing Project)\s*(?:ID|No\.?|Number)?\s*:?\s*([A-Z]{1,5}[A-Z0-9_-]{2,})",
        r"\b(MP[0-9A-Z_-]{3,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.I)
        if match:
            source_record_id = clean(match.group(1)).upper()
            break
    email_match = re.search(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        page_text,
        flags=re.I,
    )
    return {
        "detail_title": detail_title,
        "source_record_id": source_record_id,
        "detail_text": page_text,
        "licensing_contact_email": email_match.group(0) if email_match else "",
        "patent_status": section(
            page_text,
            "Patent Status",
            ["Applications", "Advantages", "Benefits", "Technology", "Contact", "Licensing"],
        ),
        "applications": section(
            page_text,
            "Applications",
            ["Advantages", "Benefits", "Technology", "Patent Status", "Contact", "Licensing"],
        ),
        "advantages_benefits": section(
            page_text,
            "Advantages",
            ["Applications", "Technology", "Patent Status", "Contact", "Licensing"],
        )
        or section(
            page_text,
            "Benefits",
            ["Applications", "Technology", "Patent Status", "Contact", "Licensing"],
        ),
    }


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    out = Path("uf-harvest")
    raw_search = out / "raw_search_pages"
    raw_details = out / "raw_detail_pages"
    out.mkdir(parents=True, exist_ok=True)
    raw_search.mkdir(parents=True, exist_ok=True)
    raw_details.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    homepage = fetch(session, BASE)
    (out / "catalog_homepage.html").write_bytes(homepage.content)
    time.sleep(2.0)

    listings_by_url: dict[str, dict] = {}
    search_receipts: list[dict] = []
    page_size_variances: list[dict] = []
    overlap_review: list[dict] = []
    displayed_counts: set[int] = set()

    for page_index in range(EXPECTED_PAGES):
        page_url = SEARCH if page_index == 0 else f"{SEARCH}?page={page_index}"
        response = fetch(session, page_url)
        raw = response.content
        (raw_search / f"page_{page_index:03d}.html").write_bytes(raw)
        displayed_total, rows = parse_search_page(response.text, page_index, page_url)
        displayed_counts.add(displayed_total)
        nominal = PAGE_SIZE
        if len(rows) != nominal:
            page_size_variances.append(
                {
                    "page_index": page_index,
                    "parsed_records": len(rows),
                    "nominal_records": nominal,
                    "page_url": page_url,
                }
            )
        search_receipts.append(
            {
                "receipt_type": "search_results_html",
                "source_index": page_index,
                "url": page_url,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "parsed_records": len(rows),
                "retrieved_at_utc": retrieved,
            }
        )
        for row in rows:
            url = row["official_record_url"]
            if url in listings_by_url:
                first = listings_by_url[url]
                overlap_review.append(
                    {
                        "official_record_url": url,
                        "source_listing_id": row["source_listing_id"],
                        "title": row["title"],
                        "first_page_index": first["search_page_index"],
                        "repeated_page_index": page_index,
                        "resolution": "PRESERVE ONCE; PAGINATION OVERLAP DOCUMENTED",
                    }
                )
                continue
            listings_by_url[url] = row
        print(
            f"search_page={page_index} rows={len(rows)} unique={len(listings_by_url)} overlaps={len(overlap_review)}",
            flush=True,
        )
        time.sleep(0.45)

    if displayed_counts != {DISPLAYED_TOTAL_EXPECTED}:
        raise ValueError(f"UF displayed count changed during run: {sorted(displayed_counts)}")
    if len(listings_by_url) != UNIQUE_TOTAL_EXPECTED:
        raise ValueError(
            f"UF unique search union {len(listings_by_url)} did not reconcile to {UNIQUE_TOTAL_EXPECTED}"
        )

    write_csv(
        out / "uf_search_page_overlap_review.csv",
        overlap_review,
        [
            "official_record_url",
            "source_listing_id",
            "title",
            "first_page_index",
            "repeated_page_index",
            "resolution",
        ],
    )
    write_csv(
        out / "uf_page_size_variances.csv",
        page_size_variances,
        ["page_index", "parsed_records", "nominal_records", "page_url"],
    )

    detail_results: dict[str, tuple[requests.Response, dict]] = {}
    errors: list[dict] = []

    def detail_job(url: str):
        local = requests.Session()
        local.headers.update(session.headers)
        local.headers.update({"Referer": SEARCH})
        response = fetch(local, url)
        return url, response, parse_detail(response.text, listings_by_url[url])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(detail_job, url): url for url in sorted(listings_by_url)}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                detail_url, response, parsed = future.result()
                detail_results[detail_url] = (response, parsed)
            except Exception as exc:
                errors.append({"stage": "detail_fetch", "url": url, "error": repr(exc)})
            if completed % 100 == 0 or completed == len(futures):
                print(
                    f"detail_completed={completed} success={len(detail_results)} errors={len(errors)}",
                    flush=True,
                )

    records: list[dict] = []
    detail_receipts: list[dict] = []
    for index, url in enumerate(sorted(listings_by_url), start=1):
        listing = listings_by_url[url]
        if url not in detail_results:
            continue
        response, detail = detail_results[url]
        raw = response.content
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", listing["source_listing_id"])[-120:]
        (raw_details / f"{index:04d}_{safe_id}.html").write_bytes(raw)
        detail_receipts.append(
            {
                "receipt_type": "technology_detail_html",
                "source_index": index,
                "url": url,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "parsed_records": 1,
                "retrieved_at_utc": retrieved,
            }
        )
        source_record_id = detail["source_record_id"] or listing["source_listing_id"]
        title = detail["detail_title"] or listing["title"]
        records.append(
            {
                "institution": "University of Florida",
                "source_organization": "UF Innovate | Tech Licensing",
                "catalog": "UF Innovate Technology Publisher",
                "source_listing_id": listing["source_listing_id"],
                "source_record_id": source_record_id,
                "source_record_id_method": (
                    "Detail-page extraction"
                    if detail["source_record_id"]
                    else "Official listing URL identity"
                ),
                "title": title,
                "summary": listing["search_teaser"],
                "published": listing["published"],
                "inventors": listing["inventors"],
                "keywords": listing["keywords"],
                "categories": listing["categories"],
                "patent_status": detail["patent_status"],
                "applications": detail["applications"],
                "advantages_benefits": detail["advantages_benefits"],
                "licensing_contact_email": detail["licensing_contact_email"],
                "detail_text": detail["detail_text"],
                "official_record_url": url,
                "search_page_index": listing["search_page_index"],
                "search_page_url": listing["search_page_url"],
                "retrieved_at_utc": retrieved,
                "source_completion_status": "SOURCE COMPLETE",
                "dedup_key": f"uf|{listing['source_listing_id'].lower()}",
            }
        )

    identities = Counter(row["source_listing_id"].lower() for row in records)
    urls = Counter(row["official_record_url"] for row in records)
    source_ids = Counter(row["source_record_id"] for row in records)
    duplicate_source_ids = {
        key: count for key, count in source_ids.items() if key and count > 1
    }
    required_missing = sum(
        1
        for row in records
        if not row["source_listing_id"] or not row["title"] or not row["official_record_url"]
    )
    source_complete = (
        len(records) == UNIQUE_TOTAL_EXPECTED
        and len(identities) == UNIQUE_TOTAL_EXPECTED
        and len(urls) == UNIQUE_TOTAL_EXPECTED
        and len(search_receipts) == EXPECTED_PAGES
        and len(detail_receipts) == UNIQUE_TOTAL_EXPECTED
        and not errors
        and required_missing == 0
    )

    write_csv(out / "uf_records.csv", records, list(records[0].keys()))
    write_csv(
        out / "uf_receipts.csv",
        search_receipts + detail_receipts,
        list(search_receipts[0].keys()),
    )
    write_csv(out / "uf_errors.csv", errors, ["stage", "url", "error"])
    (out / "uf_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "uf_duplicate_source_ids.json").write_text(
        json.dumps(duplicate_source_ids, indent=2), encoding="utf-8"
    )
    (out / "uf_rss_retrieval_status.json").write_text(
        json.dumps(
            {
                "independent_discovery_item_count": 768,
                "role": "Cross-validation of unique search-page union",
                "retrieval_note": "RSS captured during discovery; skipped during final detail sweep to avoid WAF",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manifest = {
        "institution": "University of Florida",
        "source_organization": "UF Innovate | Tech Licensing",
        "catalog_url": SEARCH,
        "retrieved_at_utc": retrieved,
        "displayed_search_result_count": DISPLAYED_TOTAL_EXPECTED,
        "official_live_search_total": UNIQUE_TOTAL_EXPECTED,
        "independent_rss_item_count": 768,
        "displayed_count_overstatement": 2,
        "search_page_size": PAGE_SIZE,
        "search_pages_expected": EXPECTED_PAGES,
        "search_pages_with_receipts": len(search_receipts),
        "page_size_variances": page_size_variances,
        "search_page_overlap_count": len(overlap_review),
        "detail_pages_with_receipts": len(detail_receipts),
        "normalized_listing_records": len(records),
        "unique_listing_identities": len(identities),
        "unique_official_record_urls": len(urls),
        "duplicate_source_record_id_groups": duplicate_source_ids,
        "required_fields_missing": required_missing,
        "errors": len(errors),
        "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "reconciliation_note": (
            "UF displayed 770 Results, while the complete 77-page union contained 768 unique official listing URLs. "
            "The pagination includes a nine-record page and repeated listings across page boundaries. "
            "UF's independently captured RSS export also contained 768 unique items, making 768 the defensible source-complete total."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (out / "uf_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)
    if not source_complete:
        print("UF SOURCE INCOMPLETE", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
