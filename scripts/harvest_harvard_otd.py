#!/usr/bin/env python3
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
from bs4 import BeautifulSoup

BASE = "https://otd.harvard.edu"
CATALOG = f"{BASE}/explore-innovation/technologies/results/"
OUT = Path("harvard-otd-harvest")
EXPECTED = 265
UA = "Mozilla/5.0 (compatible; ArnsInnovations-HarvardOTD/1.0)"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def get(url: str, tries: int = 6) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200 and len(r.text) > 500:
                return r
            last = RuntimeError(f"status={r.status_code} bytes={len(r.text)}")
        except Exception as e:
            last = e
        time.sleep(min(10, 1.25 * 2**i))
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def detail_url(href: str) -> str | None:
    url = urljoin(BASE, href).split("#", 1)[0]
    p = urlparse(url)
    path = p.path.rstrip("/") + "/"
    prefix = "/explore-innovation/technologies/"
    if p.netloc not in {"otd.harvard.edu", "www.otd.harvard.edu"}:
        return None
    if not path.startswith(prefix) or "/results/" in path:
        return None
    tail = path[len(prefix):].strip("/")
    if not tail or "/" in tail:
        return None
    return BASE + prefix + tail + "/"


def parse_listing_page(page: int) -> tuple[list[dict], int | None, str]:
    url = CATALOG if page == 1 else f"{CATALOG}p{page}/"
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    txt = clean(soup.get_text(" "))
    m = re.search(r"Displaying:\s*\d+\s*[-–]\s*\d+\s+of\s+(\d+)\s+Results", txt, re.I)
    total = int(m.group(1)) if m else None
    rows = []
    seen = set()
    for heading in soup.find_all(["h2", "h3", "h4", "h5"]):
        a = heading.find("a", href=True)
        if not a and heading.parent and heading.parent.name == "a":
            a = heading.parent
        if not a:
            continue
        url2 = detail_url(a.get("href", ""))
        if not url2 or url2 in seen:
            continue
        seen.add(url2)
        title = clean(heading.get_text(" ")) or clean(a.get_text(" "))
        container = heading
        for parent in heading.parents:
            classes = " ".join(parent.get("class", [])) if hasattr(parent, "get") else ""
            if parent.name in {"article", "li"} or (parent.name == "div" and re.search(r"result|technology|card|item", classes, re.I)):
                container = parent
                break
        lines = [clean(x) for x in container.get_text("\n").splitlines() if clean(x)]
        def block(label: str, stops: set[str]) -> list[str]:
            try:
                i = next(i for i, x in enumerate(lines) if x.casefold() == label.casefold()) + 1
            except StopIteration:
                return []
            vals = []
            for x in lines[i:]:
                if x.casefold() in stops:
                    break
                if len(x) > 180:
                    break
                if x and x.casefold() not in {"contact", "learn more"}:
                    vals.append(re.sub(r"^[•*\-–—]\s*", "", x))
            return list(dict.fromkeys(vals))
        try:
            start = next(i for i, x in enumerate(lines) if x.casefold() == title.casefold()) + 1
        except StopIteration:
            start = 0
        summary = []
        for x in lines[start:]:
            if x.casefold() in {"dbd", "investigators", "contact", "learn more"}:
                break
            summary.append(x)
        rows.append({
            "institution_name": "Harvard University",
            "institution_id": "org-harvard-university",
            "source_catalog_name": "Harvard Office of Technology Development — Browse Technologies",
            "source_catalog_url": CATALOG,
            "source_page_number": page,
            "source_position": len(rows) + 1,
            "source_record_id": urlparse(url2).path.rstrip("/").split("/")[-1],
            "source_identifier_type": "canonical_url_slug",
            "canonical_detail_url": url2,
            "title": title,
            "listing_summary": clean(" ".join(summary)),
            "dbd_contacts": block("DBD", {"investigators", "contact", "learn more"}),
            "investigators": block("Investigators", {"dbd", "contact", "learn more"}),
            "listing_page_url": url,
        })
    return rows, total, url


def parse_detail(row: dict) -> dict:
    r = get(row["canonical_detail_url"])
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = "\n".join(clean(x) for x in main.get_text("\n").splitlines() if clean(x))
    title_el = main.find("h1") or soup.find("h1")
    if title_el:
        row["title"] = clean(title_el.get_text(" ")) or row["title"]
    case = re.search(r"\bCase\s*(?:No\.?|Number|#)?\s*[:#-]?\s*([A-Za-z0-9.-]+)", text, re.I)
    patents = sorted(set(re.findall(r"\b(?:US|WO|EP)\s*\d{6,}[A-Z0-9]*\b", text, re.I)))
    row.update({
        "official_case_number": case.group(1) if case else "",
        "patent_identifiers": patents,
        "detail_text": text,
        "detail_sha256": hashlib.sha256(clean(text).encode()).hexdigest(),
        "http_status": r.status_code,
        "harvested_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "provenance_tier": "official_public_source",
        "corpus_tier": "discovery_pool",
        "canon_status": "not_promoted",
    })
    return row


def main() -> int:
    OUT.mkdir(exist_ok=True)
    first, total, _ = parse_listing_page(1)
    total = total or EXPECTED
    pages = math.ceil(total / 10)
    print(f"reported_total={total} pages={pages} page1={len(first)}", flush=True)
    listings = list(first)
    page_receipts = [{"page": 1, "records": len(first)}]
    for page in range(2, pages + 1):
        rows, page_total, url = parse_listing_page(page)
        if page_total and page_total != total:
            raise RuntimeError(f"catalog total changed at {url}: {page_total} != {total}")
        listings.extend(rows)
        page_receipts.append({"page": page, "records": len(rows), "url": url})
        print(f"page={page}/{pages} records={len(rows)} cumulative={len(listings)}", flush=True)

    unique = {r["canonical_detail_url"]: r for r in listings}
    rows = list(unique.values())
    failures = []
    enriched = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(parse_detail, dict(r)): r for r in rows}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                enriched.append(fut.result())
            except Exception as e:
                failures.append({"url": src["canonical_detail_url"], "error": repr(e)})
            if i % 25 == 0 or i == len(futures):
                print(f"details={i}/{len(futures)} failures={len(failures)}", flush=True)

    enriched.sort(key=lambda r: (r["source_page_number"], r["source_position"]))
    checks = {
        "reported_total_is_265": total == EXPECTED,
        "all_27_pages_retrieved": len(page_receipts) == 27 and all(x["records"] > 0 for x in page_receipts),
        "unique_listing_count_equals_reported_total": len(rows) == total,
        "detail_count_equals_listing_count": len(enriched) == len(rows),
        "no_detail_failures": not failures,
        "all_titles_present": all(r.get("title") for r in enriched),
        "all_canonical_urls_present": all(r.get("canonical_detail_url") for r in enriched),
        "all_detail_text_present": all(r.get("detail_text") for r in enriched),
        "all_http_200": all(r.get("http_status") == 200 for r in enriched),
    }
    passed = all(checks.values())

    fields = [
        "institution_name","institution_id","source_catalog_name","source_catalog_url",
        "source_page_number","source_position","source_record_id","source_identifier_type",
        "canonical_detail_url","title","listing_summary","dbd_contacts","investigators",
        "official_case_number","patent_identifiers","listing_page_url","detail_text",
        "detail_sha256","http_status","harvested_at_utc","provenance_tier","corpus_tier","canon_status"
    ]
    with (OUT / "harvard_otd_technologies.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in enriched:
            c = dict(r)
            c["dbd_contacts"] = " | ".join(c.get("dbd_contacts") or [])
            c["investigators"] = " | ".join(c.get("investigators") or [])
            c["patent_identifiers"] = " | ".join(c.get("patent_identifiers") or [])
            w.writerow({k: c.get(k, "") for k in fields})
    with (OUT / "harvard_otd_technologies.jsonl").open("w", encoding="utf-8") as f:
        for r in enriched:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    validation = {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "catalog_reported_total": total,
        "unique_listings": len(rows),
        "detail_records": len(enriched),
        "failures": failures,
        "page_receipts": page_receipts,
        "validated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    (OUT / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    manifest = {
        "institution": "Harvard University",
        "source": "Harvard Office of Technology Development",
        "catalog_url": CATALOG,
        "scope": "All publicly listed Harvard OTD technology records and detail pages",
        "record_count": len(enriched),
        "catalog_reported_total": total,
        "validation_status": validation["status"],
        "governance": {"corpus_tier": "discovery_pool", "canon_status": "not_promoted"},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(validation, indent=2), flush=True)
    return 0 if passed else 1

if __name__ == "__main__":
    sys.exit(main())
