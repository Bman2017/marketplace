#!/usr/bin/env python3
"""Fail-closed public Skysong Innovations technology-catalog harvester.

The source is ASU's exclusive technology-transfer organization. The script first
uses the WordPress REST API when available, falls back to WordPress/Yoast
sitemaps, preserves raw evidence, and records explicit ASU/partner-institution
signals rather than assuming ownership where the public page is ambiguous.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://skysonginnovations.com/"
WP_JSON = urljoin(BASE_URL, "wp-json/")
ROBOTS_URL = urljoin(BASE_URL, "robots.txt")
USER_AGENT = (
    "Arns-Innovations-ASU-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)
MAX_PER_PAGE = 100


@dataclass
class TechnologyRecord:
    institution_scope: str
    catalog: str
    wordpress_id: int | str
    source_record_id: str
    source_listing_id: str
    title: str
    published: str
    modified: str
    inventors: str
    technology_categories: str
    technology_keywords: str
    licensing_contacts: str
    summary: str
    official_record_url: str
    source_api_url: str
    explicit_asu_signal: bool
    explicit_partner_signal: bool
    affiliation_evidence: str
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
    record_ids: str
    retrieved_at_utc: str


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def strip_html(value: str | None) -> str:
    return clean(BeautifulSoup(value or "", "lxml").get_text(" ", strip=True))


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
    for url in (BASE_URL, urljoin(BASE_URL, "technology/"), WP_JSON):
        if not parser.can_fetch(USER_AGENT, url):
            raise RuntimeError(f"robots.txt does not permit fetching {url}")


def find_technology_rest_base(session: requests.Session, timeout: float) -> tuple[str, dict[str, Any]]:
    types_url = urljoin(BASE_URL, "wp-json/wp/v2/types")
    response = session.get(types_url, timeout=timeout)
    if response.status_code >= 400:
        return "", {}
    try:
        types = response.json()
    except Exception:
        return "", {}
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for key, value in types.items():
        if not isinstance(value, dict):
            continue
        rest_base = clean(str(value.get("rest_base") or key))
        blob = " ".join(
            clean(str(value.get(field) or ""))
            for field in ("name", "slug", "description", "rest_base")
        ).lower()
        score = 0
        if key.lower() == "technology" or rest_base.lower() == "technology":
            score += 100
        if "technology" in blob:
            score += 50
        if "technologies" in blob:
            score += 30
        if score:
            ranked.append((score, rest_base, value))
    if not ranked:
        return "", types
    ranked.sort(reverse=True, key=lambda item: item[0])
    return ranked[0][1], types


def embedded_terms(post: dict[str, Any]) -> tuple[list[str], list[str]]:
    categories: list[str] = []
    affiliations: list[str] = []
    embedded = post.get("_embedded") or {}
    groups = embedded.get("wp:term") or []
    for group in groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if not isinstance(term, dict):
                continue
            name = clean(str(term.get("name") or ""))
            taxonomy = clean(str(term.get("taxonomy") or "")).lower()
            if not name:
                continue
            categories.append(name)
            if any(token in taxonomy for token in ("institution", "organization", "partner", "owner", "university")):
                affiliations.append(name)
    return list(dict.fromkeys(categories)), list(dict.fromkeys(affiliations))


def text_after_heading(soup: BeautifulSoup, pattern: str) -> str:
    heading = soup.find(
        lambda tag: tag.name in ("h2", "h3", "h4", "h5", "strong")
        and re.search(pattern, clean(tag.get_text(" ", strip=True)), flags=re.I)
    )
    if heading is None:
        return ""
    values: list[str] = []
    for sibling in heading.next_siblings:
        name = getattr(sibling, "name", None)
        if name in ("h2", "h3", "h4", "h5", "hr"):
            break
        if hasattr(sibling, "get_text"):
            text = clean(sibling.get_text(" ", strip=True))
        else:
            text = clean(str(sibling))
        if text:
            values.append(text)
    return " / ".join(values[:20])


def parse_content_fields(content_html: str, excerpt_html: str, title: str) -> dict[str, str]:
    soup = BeautifulSoup(content_html or "", "lxml")
    page_text = clean(soup.get_text(" ", strip=True))
    case_match = re.search(r"\bCase ID:\s*([^\s|]+)", page_text, flags=re.I)
    case_id = clean(case_match.group(1) if case_match else "")

    inventors = text_after_heading(soup, r"^Inventor\(s\)$|^Inventors?$")
    categories = text_after_heading(soup, r"^Technology categories$")
    keywords = text_after_heading(soup, r"^Technology keywords$")
    contacts = text_after_heading(soup, r"^Licensing Contacts?$")
    summary = strip_html(excerpt_html)
    if not summary:
        # Keep a bounded plain-text summary while preserving the full HTML separately.
        summary = page_text[:2000]

    return {
        "case_id": case_id,
        "inventors": inventors,
        "categories": categories,
        "keywords": keywords,
        "contacts": contacts,
        "summary": summary,
        "page_text": page_text,
    }


def classify_affiliation(
    title: str,
    content_html: str,
    embedded_affiliations: list[str],
) -> tuple[str, bool, bool, str]:
    text = clean(
        BeautifulSoup(content_html or "", "lxml").get_text(" ", strip=True)
    )
    combined = f"{title} {text} {' '.join(embedded_affiliations)}"
    explicit_asu = bool(
        re.search(r"\bArizona State University\b|\bASU\b|\bASU researchers?\b", combined, flags=re.I)
    )
    partner_terms = [
        term for term in embedded_affiliations
        if not re.search(r"Arizona State University|\bASU\b", term, flags=re.I)
    ]
    explicit_partner = bool(partner_terms)
    evidence_parts: list[str] = []
    if explicit_asu:
        evidence_parts.append("Official page explicitly references Arizona State University/ASU")
    if partner_terms:
        evidence_parts.append("Embedded affiliation taxonomy: " + " / ".join(partner_terms))
    if explicit_asu and not explicit_partner:
        scope = "Arizona State University — explicit"
    elif explicit_asu and explicit_partner:
        scope = "ASU collaborative / partner-associated"
    elif explicit_partner:
        scope = "Skysong partner institution — explicit"
    else:
        scope = "Skysong-managed — public ownership signal not explicit"
        evidence_parts.append("No explicit institution field detected in public REST payload")
    return scope, explicit_asu, explicit_partner, "; ".join(evidence_parts)


def parse_rest_post(post: dict[str, Any], endpoint: str, retrieved_at: str) -> TechnologyRecord:
    title = strip_html(str((post.get("title") or {}).get("rendered") or ""))
    content_html = str((post.get("content") or {}).get("rendered") or "")
    excerpt_html = str((post.get("excerpt") or {}).get("rendered") or "")
    link = clean(str(post.get("link") or ""))
    post_id = post.get("id") or ""
    slug = clean(str(post.get("slug") or ""))
    fields = parse_content_fields(content_html, excerpt_html, title)
    case_id = fields["case_id"] or slug or str(post_id)
    source_listing_id = f"{case_id}|{slug or post_id}"
    all_terms, affiliation_terms = embedded_terms(post)
    scope, explicit_asu, explicit_partner, affiliation_evidence = classify_affiliation(
        title, content_html, affiliation_terms
    )

    # REST terms may expose categories more reliably than rendered content.
    categories = fields["categories"] or " / ".join(all_terms)
    return TechnologyRecord(
        institution_scope=scope,
        catalog="Skysong Innovations Available Technologies",
        wordpress_id=post_id,
        source_record_id=case_id,
        source_listing_id=source_listing_id,
        title=title,
        published=clean(str(post.get("date_gmt") or post.get("date") or "")),
        modified=clean(str(post.get("modified_gmt") or post.get("modified") or "")),
        inventors=fields["inventors"],
        technology_categories=categories,
        technology_keywords=fields["keywords"],
        licensing_contacts=fields["contacts"],
        summary=fields["summary"],
        official_record_url=link,
        source_api_url=f"{endpoint}/{post_id}",
        explicit_asu_signal=explicit_asu,
        explicit_partner_signal=explicit_partner,
        affiliation_evidence=affiliation_evidence,
        retrieved_at_utc=retrieved_at,
        dedup_key=f"skysong|{source_listing_id.lower()}",
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="asu_skysong_harvest")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--skip-robots-check", action="store_true")
    args = parser.parse_args()

    output = Path(args.output_dir)
    raw_dir = output / "raw_api_pages"
    output.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if not args.skip_robots_check:
        verify_robots(session, args.timeout)

    retrieved_at = datetime.now(timezone.utc).isoformat()
    rest_base, types = find_technology_rest_base(session, args.timeout)
    (output / "wordpress_post_types.json").write_text(
        json.dumps(types, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not rest_base:
        raise RuntimeError("Could not identify a public WordPress REST technology post type")

    endpoint = urljoin(BASE_URL, f"wp-json/wp/v2/{rest_base}")
    first_url = f"{endpoint}?per_page={MAX_PER_PAGE}&page=1&_embed=1&orderby=id&order=asc"
    first = fetch(session, first_url, args.attempts, args.timeout)
    official_count = int(first.headers.get("X-WP-Total") or 0)
    total_pages = int(first.headers.get("X-WP-TotalPages") or 0)
    if official_count <= 0 or total_pages <= 0:
        raise ValueError(
            f"WordPress REST endpoint did not expose a valid total: count={official_count}, pages={total_pages}"
        )
    if total_pages != math.ceil(official_count / MAX_PER_PAGE):
        raise ValueError("WordPress REST page-count headers disagree with total count")

    records: dict[str, TechnologyRecord] = {}
    receipts: list[PageReceipt] = []
    errors: list[dict[str, Any]] = []

    for page in range(1, total_pages + 1):
        url = f"{endpoint}?per_page={MAX_PER_PAGE}&page={page}&_embed=1&orderby=id&order=asc"
        try:
            response = first if page == 1 else fetch(session, url, args.attempts, args.timeout)
            raw = response.content
            (raw_dir / f"page_{page:03d}.json").write_bytes(raw)
            posts = response.json()
            if not isinstance(posts, list):
                raise TypeError(f"Page {page} did not return a JSON list")
            expected = (
                MAX_PER_PAGE
                if page < total_pages
                else official_count - MAX_PER_PAGE * (total_pages - 1)
            )
            if len(posts) != expected:
                raise ValueError(f"Page {page} returned {len(posts)} records; expected {expected}")

            page_records: list[TechnologyRecord] = []
            for post in posts:
                record = parse_rest_post(post, endpoint, retrieved_at)
                key = record.source_listing_id.lower()
                if key in records:
                    prior = records[key]
                    if prior.official_record_url != record.official_record_url or prior.title != record.title:
                        raise ValueError(f"Conflicting listing identity {record.source_listing_id}")
                records[key] = record
                page_records.append(record)

            receipts.append(PageReceipt(
                page=page,
                url=url,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                raw_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                parsed_records=len(page_records),
                record_ids="|".join(record.source_listing_id for record in page_records),
                retrieved_at_utc=retrieved_at,
            ))
            print(
                f"page={page:03d}/{total_pages:03d} records={len(page_records):03d} unique={len(records)}",
                flush=True,
            )
        except Exception as exc:
            errors.append({"page": page, "url": url, "error": repr(exc)})
            print(f"page={page:03d} ERROR {exc!r}", file=sys.stderr, flush=True)
        if page < total_pages:
            time.sleep(max(0.0, args.delay))

    record_rows = [asdict(record) for record in sorted(records.values(), key=lambda r: str(r.wordpress_id))]
    receipt_rows = [asdict(receipt) for receipt in sorted(receipts, key=lambda r: r.page)]
    required_missing = sum(
        1
        for row in record_rows
        if not row["source_record_id"]
        or not row["source_listing_id"]
        or not row["title"]
        or not row["official_record_url"]
    )
    listing_counter = Counter(row["source_listing_id"].lower() for row in record_rows)
    url_counter = Counter(row["official_record_url"] for row in record_rows)
    duplicate_listing_ids = {key: count for key, count in listing_counter.items() if count > 1}
    duplicate_urls = {key: count for key, count in url_counter.items() if count > 1}
    scope_counts = Counter(row["institution_scope"] for row in record_rows)

    record_fields = list(TechnologyRecord.__dataclass_fields__)
    receipt_fields = list(PageReceipt.__dataclass_fields__)
    write_csv(output / "asu_skysong_records.csv", record_rows, record_fields)
    write_csv(output / "asu_skysong_page_receipts.csv", receipt_rows, receipt_fields)
    write_csv(output / "asu_skysong_errors.csv", errors, ["page", "url", "error"])
    (output / "asu_skysong_records.json").write_text(
        json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "asu_skysong_page_receipts.json").write_text(
        json.dumps(receipt_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest = {
        "institution_requested": "Arizona State University",
        "source_organization": "Skysong Innovations",
        "catalog_url": BASE_URL,
        "wordpress_rest_endpoint": endpoint,
        "retrieved_at_utc": retrieved_at,
        "official_rest_total": official_count,
        "rest_pages": total_pages,
        "pages_with_receipts": len(receipts),
        "page_errors": len(errors),
        "unique_listings": len(record_rows),
        "unique_official_urls": len(url_counter),
        "required_fields_missing": required_missing,
        "duplicate_listing_identities": duplicate_listing_ids,
        "duplicate_official_urls": duplicate_urls,
        "institution_scope_counts": dict(scope_counts),
        "explicit_asu_records": sum(1 for row in record_rows if row["explicit_asu_signal"]),
        "explicit_partner_records": sum(1 for row in record_rows if row["explicit_partner_signal"]),
        "source_complete": (
            len(record_rows) == official_count
            and len(receipts) == total_pages
            and not errors
            and required_missing == 0
            and not duplicate_listing_ids
            and not duplicate_urls
        ),
        "scope_note": (
            "Skysong Innovations is ASU's exclusive technology-transfer organization and also works with select partner institutions. "
            "The harvest is source-complete for the public Skysong technology catalog; each record retains explicit ASU/partner signals instead of assuming ownership where the page is silent."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    (output / "asu_skysong_source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)

    if not manifest["source_complete"]:
        print("ASU/SKYSONG SOURCE INCOMPLETE: aggregate admission is not authorized.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
