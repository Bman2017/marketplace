#!/usr/bin/env python3
"""Source-complete harvest of UF Innovate's public Technology Publisher catalog."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://ufinnovate.technologypublisher.com/"
SEARCH = urljoin(BASE, "searchresults.aspx")
RSS = urljoin(BASE, "rss.aspx")
USER_AGENT = (
    "Arns-Innovations-UF-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)
PAGE_SIZE = 10


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def local_tag(tag: str) -> str:
    return tag.split("}")[-1].split(":")[-1]


def fetch(session: requests.Session, url: str, attempts: int = 4, timeout: float = 90.0) -> requests.Response:
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            error = exc
            if attempt < attempts:
                time.sleep(min(10.0, attempt * 1.5))
    assert error is not None
    raise error


def parse_rss(xml_bytes: bytes) -> tuple[list[dict], dict[str, dict]]:
    root = ET.fromstring(xml_bytes)
    rows: list[dict] = []
    by_url: dict[str, dict] = {}
    for item in root.findall(".//item"):
        values: dict[str, list[str]] = {}
        for child in list(item):
            key = local_tag(child.tag)
            values.setdefault(key, []).append(clean(child.text))
        link = (values.get("link") or values.get("guid") or [""])[0].rstrip("/")
        row = {
            "source_record_id": (values.get("caseId") or [""])[0],
            "title": (values.get("title") or [""])[0],
            "summary": (values.get("description") or [""])[0],
            "published": (values.get("pubDate") or [""])[0],
            "author": (values.get("author") or [""])[0],
            "keywords": " / ".join(values.get("keyword", []) + values.get("keywords", [])),
            "categories": " / ".join(values.get("category", [])),
            "official_record_url": link,
            "raw_fields_json": json.dumps(values, ensure_ascii=False, sort_keys=True),
        }
        rows.append(row)
        if link:
            by_url[link] = row
    return rows, by_url


def parse_search_page(html_text: str, page_index: int, page_url: str) -> tuple[int, list[dict]]:
    soup = BeautifulSoup(html_text, "lxml")
    text = clean(soup.get_text(" ", strip=True))
    count_match = re.search(r"\b([0-9][0-9,]*)\s+Results?\b", text, flags=re.I)
    if not count_match:
        raise ValueError(f"Could not parse UF result count on page {page_index}")
    total = int(count_match.group(1).replace(",", ""))

    rows: list[dict] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor.get("href", ""))
        if "/tech/" not in urlparse(href).path:
            continue
        href = href.split("#", 1)[0].rstrip("/")
        if href in seen_urls:
            continue
        seen_urls.add(href)
        cell = anchor.find_parent("td")
        cell_text = clean(cell.get_text(" ", strip=True) if cell else "")
        title = clean(anchor.get_text(" ", strip=True))
        published_match = re.search(r"Published:\s*([^|]+?)(?=\s*\||\s*Inventor\(s\):|$)", cell_text, flags=re.I)
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
            keyword_match = re.search(r"Keywords\(s\):\s*(.*?)\s*Category\(s\):", cell_text, flags=re.I)
            if keyword_match:
                keywords = clean(keyword_match.group(1))
        # Remove title and metadata tail to retain the public teaser.
        teaser = cell_text
        if teaser.startswith(title):
            teaser = teaser[len(title):].strip()
        teaser = re.split(r"\s+Published:\s*", teaser, maxsplit=1, flags=re.I)[0]
        rows.append({
            "source_listing_id": urlparse(href).path.rstrip("/").split("/")[-1],
            "search_page_index": page_index,
            "search_page_url": page_url,
            "title": title,
            "search_teaser": clean(teaser),
            "published": clean(published_match.group(1)) if published_match else "",
            "inventors": " / ".join(dict.fromkeys(inventors)),
            "keywords": keywords,
            "categories": " / ".join(dict.fromkeys(categories)),
            "official_record_url": href,
        })
    return total, rows


def labeled_section(text: str, label: str, stops: list[str]) -> str:
    start = re.search(rf"\b{re.escape(label)}\s*:?\s*", text, flags=re.I)
    if not start:
        return ""
    tail = text[start.end():]
    ends: list[int] = []
    for stop in stops:
        match = re.search(rf"\b{re.escape(stop)}\s*:?\s*", tail, flags=re.I)
        if match:
            ends.append(match.start())
    if ends:
        tail = tail[: min(ends)]
    return clean(tail)


def parse_detail(html_text: str, listing: dict) -> dict:
    soup = BeautifulSoup(html_text, "lxml")
    main = soup.find("main") or soup.find("section") or soup.body or soup
    text = clean(main.get_text(" ", strip=True))
    heading = main.find("h1") or main.find("h2")
    detail_title = clean(heading.get_text(" ", strip=True) if heading else "")

    id_patterns = [
        r"(?:Case|Technology|Project|Marketing Project)\s*(?:ID|No\.?|Number)?\s*:?\s*([A-Z]{1,5}[A-Z0-9_-]{2,})",
        r"\b(MP[0-9A-Z_-]{3,})\b",
    ]
    source_record_id = ""
    for pattern in id_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            source_record_id = clean(match.group(1)).upper()
            break

    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.I)
    patent_status = labeled_section(text, "Patent Status", ["Applications", "Advantages", "Technology", "Contact", "Licensing"])
    applications = labeled_section(text, "Applications", ["Advantages", "Benefits", "Technology", "Patent Status", "Contact", "Licensing"])
    advantages = labeled_section(text, "Advantages", ["Applications", "Technology", "Patent Status", "Contact", "Licensing"])
    if not advantages:
        advantages = labeled_section(text, "Benefits", ["Applications", "Technology", "Patent Status", "Contact", "Licensing"])

    return {
        "detail_title": detail_title,
        "detail_text": text,
        "detail_source_record_id": source_record_id,
        "licensing_contact_email": email_match.group(0) if email_match else "",
        "patent_status": patent_status,
        "applications": applications,
        "advantages_benefits": advantages,
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
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    rss_response = fetch(session, RSS)
    (out / "uf_rss.xml").write_bytes(rss_response.content)
    rss_rows, rss_by_url = parse_rss(rss_response.content)

    first_response = fetch(session, SEARCH)
    first_total, first_rows = parse_search_page(first_response.text, 0, SEARCH)
    expected_pages = math.ceil(first_total / PAGE_SIZE)

    search_receipts: list[dict] = []
    listings_by_url: dict[str, dict] = {}
    for page_index in range(expected_pages):
        url = SEARCH if page_index == 0 else f"{SEARCH}?page={page_index}"
        response = first_response if page_index == 0 else fetch(session, url)
        raw = response.content
        (raw_search / f"page_{page_index:03d}.html").write_bytes(raw)
        page_total, page_rows = parse_search_page(response.text, page_index, url)
        if page_total != first_total:
            raise ValueError(f"UF count changed during harvest: {first_total} to {page_total}")
        expected = PAGE_SIZE if page_index < expected_pages - 1 else first_total - PAGE_SIZE * (expected_pages - 1)
        if len(page_rows) != expected:
            raise ValueError(f"UF page {page_index} parsed {len(page_rows)} rows; expected {expected}")
        search_receipts.append({
            "receipt_type": "search_results_html",
            "source_index": page_index,
            "url": url,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parsed_records": len(page_rows),
            "retrieved_at_utc": retrieved,
        })
        for row in page_rows:
            if row["official_record_url"] in listings_by_url:
                raise ValueError(f"Duplicate UF listing URL across search pages: {row['official_record_url']}")
            listings_by_url[row["official_record_url"]] = row
        print(f"search_page={page_index} rows={len(page_rows)} unique={len(listings_by_url)}", flush=True)

    if len(listings_by_url) != first_total:
        raise ValueError(f"UF search enumeration {len(listings_by_url)} != live total {first_total}")

    detail_results: dict[str, tuple[requests.Response, dict]] = {}
    errors: list[dict] = []

    def detail_job(url: str):
        local = requests.Session(); local.headers.update(session.headers)
        response = fetch(local, url)
        return url, response, parse_detail(response.text, listings_by_url[url])

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(detail_job, url): url for url in sorted(listings_by_url)}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                detail_url, response, parsed = future.result()
                detail_results[detail_url] = (response, parsed)
            except Exception as exc:
                errors.append({"stage": "detail_fetch", "url": url, "error": repr(exc)})
            if completed % 100 == 0 or completed == len(futures):
                print(f"detail_completed={completed} success={len(detail_results)} errors={len(errors)}", flush=True)

    detail_receipts: list[dict] = []
    records: list[dict] = []
    rss_missing: list[dict] = []
    for index, url in enumerate(sorted(listings_by_url), start=1):
        listing = listings_by_url[url]
        if url not in detail_results:
            continue
        response, detail = detail_results[url]
        raw = response.content
        slug = listing["source_listing_id"]
        (raw_details / f"{index:04d}_{slug}.html").write_bytes(raw)
        detail_receipts.append({
            "receipt_type": "technology_detail_html",
            "source_index": index,
            "url": url,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parsed_records": 1,
            "retrieved_at_utc": retrieved,
        })
        rss = rss_by_url.get(url)
        source_record_id = clean((rss or {}).get("source_record_id")) or detail["detail_source_record_id"]
        id_source = "RSS caseId" if rss and rss.get("source_record_id") else ("Detail-page extraction" if source_record_id else "URL-slug fallback")
        if not source_record_id:
            source_record_id = slug
        title = detail["detail_title"] or clean((rss or {}).get("title")) or listing["title"]
        summary = clean((rss or {}).get("summary")) or listing["search_teaser"]
        published = clean((rss or {}).get("published")) or listing["published"]
        inventors = listing["inventors"] or clean((rss or {}).get("author"))
        keywords = listing["keywords"] or clean((rss or {}).get("keywords"))
        categories = listing["categories"] or clean((rss or {}).get("categories"))
        if rss is None:
            rss_missing.append({
                "source_listing_id": slug,
                "source_record_id": source_record_id,
                "title": title,
                "official_record_url": url,
                "resolution": "PRESERVE FROM LIVE SEARCH CATALOG AND DETAIL PAGE; RSS EXPORT GAP",
            })
        records.append({
            "institution": "University of Florida",
            "source_organization": "UF Innovate | Tech Licensing",
            "catalog": "UF Innovate Technology Publisher",
            "source_listing_id": slug,
            "source_record_id": source_record_id,
            "source_record_id_method": id_source,
            "title": title,
            "summary": summary,
            "published": published,
            "inventors": inventors,
            "keywords": keywords,
            "categories": categories,
            "patent_status": detail["patent_status"],
            "applications": detail["applications"],
            "advantages_benefits": detail["advantages_benefits"],
            "licensing_contact_email": detail["licensing_contact_email"],
            "detail_text": detail["detail_text"],
            "official_record_url": url,
            "search_page_index": listing["search_page_index"],
            "search_page_url": listing["search_page_url"],
            "rss_present": rss is not None,
            "retrieved_at_utc": retrieved,
            "source_completion_status": "SOURCE COMPLETE",
            "dedup_key": f"uf|{slug.lower()}",
        })

    identities = Counter(row["source_listing_id"].lower() for row in records)
    urls = Counter(row["official_record_url"] for row in records)
    case_ids = Counter(row["source_record_id"] for row in records)
    duplicate_identities = {key: count for key, count in identities.items() if count > 1}
    duplicate_urls = {key: count for key, count in urls.items() if count > 1}
    duplicate_case_ids = {key: count for key, count in case_ids.items() if count > 1}
    required_missing = sum(1 for row in records if not row["source_listing_id"] or not row["title"] or not row["official_record_url"])

    source_complete = (
        len(records) == first_total
        and len(identities) == first_total
        and len(urls) == first_total
        and len(search_receipts) == expected_pages
        and len(detail_receipts) == first_total
        and not errors
        and not duplicate_identities
        and not duplicate_urls
        and required_missing == 0
    )

    write_csv(out / "uf_records.csv", records, list(records[0].keys()))
    write_csv(out / "uf_receipts.csv", search_receipts + detail_receipts, list(search_receipts[0].keys()))
    write_csv(out / "uf_rss_gap_review.csv", rss_missing, ["source_listing_id", "source_record_id", "title", "official_record_url", "resolution"])
    write_csv(out / "uf_errors.csv", errors, ["stage", "url", "error"])
    (out / "uf_records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "uf_duplicate_case_ids.json").write_text(json.dumps(duplicate_case_ids, indent=2), encoding="utf-8")

    manifest = {
        "institution": "University of Florida",
        "source_organization": "UF Innovate | Tech Licensing",
        "catalog_url": SEARCH,
        "rss_url": RSS,
        "retrieved_at_utc": retrieved,
        "official_live_search_total": first_total,
        "search_page_size": PAGE_SIZE,
        "search_pages_expected": expected_pages,
        "search_pages_with_receipts": len(search_receipts),
        "detail_pages_with_receipts": len(detail_receipts),
        "normalized_listing_records": len(records),
        "unique_listing_identities": len(identities),
        "unique_official_record_urls": len(urls),
        "rss_item_count": len(rss_rows),
        "rss_missing_live_listing_records": len(rss_missing),
        "duplicate_listing_identities": duplicate_identities,
        "duplicate_official_urls": duplicate_urls,
        "duplicate_source_record_id_groups": duplicate_case_ids,
        "required_fields_missing": required_missing,
        "errors": len(errors),
        "source_complete": source_complete,
        "aggregate_admission": "AUTHORIZED" if source_complete else "BLOCKED",
        "reconciliation_note": (
            "The live search catalog is the enumeration authority. It contained more listings than the RSS export during retrieval; "
            "RSS-missing listings are preserved from their live search and detail pages and isolated in the RSS gap review."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    (out / "uf_source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)
    if not source_complete:
        print("UF SOURCE INCOMPLETE", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
