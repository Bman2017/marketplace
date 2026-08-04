#!/usr/bin/env python3
"""Inventory UT System commercialization sources and harvest authorized public feeds.

This is intentionally fail-closed. Technology Publisher public RSS feeds are
harvested and retained with raw receipts. Wellspring/Flintbox portals are
registered but not bulk-crawled because their terms restrict spiders. Custom
campus sites are recorded for a later source-specific connector.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Arns-Innovations-UT-System-Public-Catalog-Harvest/1.0 "
    "(public metadata; source URLs and receipts retained)"
)

INSTITUTIONS = [
    {
        "code": "UTA",
        "institution": "The University of Texas at Arlington",
        "office_url": "https://www.uta.edu/research/innovation-and-commercialization",
        "candidate_portals": [],
    },
    {
        "code": "UTAUS",
        "institution": "The University of Texas at Austin",
        "office_url": "https://discoverytoimpact.utexas.edu/",
        "candidate_portals": ["https://utotc.technologypublisher.com/"],
    },
    {
        "code": "UTD",
        "institution": "The University of Texas at Dallas",
        "office_url": "https://research.utdallas.edu/innovation/industry",
        "candidate_portals": [
            "https://utdallas.technologypublisher.com/",
            "https://texas.technologypublisher.com/",
        ],
    },
    {
        "code": "UTEP",
        "institution": "The University of Texas at El Paso",
        "office_url": "https://www.utep.edu/research/otc/",
        "candidate_portals": [],
    },
    {
        "code": "UTPB",
        "institution": "The University of Texas Permian Basin",
        "office_url": "https://www.utpb.edu/university-offices/research-and-sponsored-programs/",
        "candidate_portals": [],
    },
    {
        "code": "UTRGV",
        "institution": "The University of Texas Rio Grande Valley",
        "office_url": "https://www.utrgv.edu/research/departments/research-operations/otc/licensing/index.htm",
        "candidate_portals": [],
    },
    {
        "code": "UTSA",
        "institution": "The University of Texas at San Antonio",
        "office_url": "https://research.utsa.edu/",
        "candidate_portals": ["https://utsa.flintbox.com/"],
    },
    {
        "code": "SFA",
        "institution": "Stephen F. Austin State University",
        "office_url": "https://www.sfasu.edu/academics/orgs/research-graduate-studies",
        "candidate_portals": [],
    },
    {
        "code": "UTTYLER",
        "institution": "The University of Texas at Tyler",
        "office_url": "https://www.uttyler.edu/research/technology-transfer/",
        "candidate_portals": [],
    },
    {
        "code": "UTSW",
        "institution": "The University of Texas Southwestern Medical Center",
        "office_url": "https://www.utsouthwestern.edu/about-us/administrative-offices/technology-development/",
        "candidate_portals": [],
    },
    {
        "code": "UTMB",
        "institution": "The University of Texas Medical Branch at Galveston",
        "office_url": "https://www.utmb.edu/innovations/home",
        "candidate_portals": [],
    },
    {
        "code": "UTH",
        "institution": "The University of Texas Health Science Center at Houston",
        "office_url": "https://www.uth.edu/otm/",
        "candidate_portals": [
            "https://uthealth.technologypublisher.com/",
            "https://texas.technologypublisher.com/",
        ],
    },
    {
        "code": "MDACC",
        "institution": "The University of Texas MD Anderson Cancer Center",
        "office_url": "https://www.mdanderson.org/about-md-anderson/innovation/strategic-industry-ventures/office-of-technology-and-commercialization.html",
        "candidate_portals": [],
    },
]


@dataclass
class Record:
    institution_code: str
    institution: str
    platform: str
    source_record_id: str
    title: str
    summary: str
    published: str
    updated: str
    inventors: str
    categories: str
    keywords: str
    official_record_url: str
    source_feed_url: str
    retrieved_at_utc: str
    dedup_key: str


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def fetch(session: requests.Session, url: str, attempts: int = 3, timeout: float = 60.0) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)
    assert last is not None
    raise last


def text_from_html(value: str | None) -> str:
    return clean(BeautifulSoup(value or "", "lxml").get_text(" ", strip=True))


def discover_external_links(session: requests.Session, office_url: str) -> list[str]:
    try:
        response = fetch(session, office_url, attempts=2, timeout=45)
    except Exception:
        return []
    soup = BeautifulSoup(response.text, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, anchor["href"])
        label = clean(anchor.get_text(" ", strip=True)).lower()
        host = urlparse(href).netloc.lower()
        if any(token in host for token in ["technologypublisher.com", "flintbox.com"]):
            links.append(href)
        elif any(token in label for token in ["available technologies", "explore technologies", "browse technologies", "licensing opportunities"]):
            links.append(href)
    return sorted(set(links))


def technology_publisher_feed_candidates(base: str) -> list[str]:
    root = base.rstrip("/")
    return [
        f"{root}/rss",
        f"{root}/rss.aspx",
        f"{root}/rss.xml",
        f"{root}/feed",
    ]


def parse_rss(xml_bytes: bytes, institution: dict, feed_url: str, retrieved_at: str) -> list[Record]:
    root = ET.fromstring(xml_bytes)
    records: list[Record] = []
    for item in root.findall(".//item"):
        def find_text(names: Iterable[str]) -> str:
            for name in names:
                node = item.find(name)
                if node is not None and node.text:
                    return clean(node.text)
            for child in item:
                local = child.tag.split("}")[-1].lower()
                if local in {n.lower() for n in names} and child.text:
                    return clean(child.text)
            return ""

        title = text_from_html(find_text(["title"]))
        link = find_text(["link", "guid"])
        description = text_from_html(find_text(["description", "summary", "content:encoded"]))
        published = find_text(["pubDate", "published", "dc:date"])
        updated = find_text(["updated", "lastBuildDate"])
        categories = " / ".join(
            clean(child.text)
            for child in item
            if child.tag.split("}")[-1].lower() == "category" and child.text
        )
        guid = find_text(["guid"])
        source_id = ""
        for value in [guid, link]:
            match = re.search(r"/(?:technology|tech)/(\d+|[^/?#]+)", value or "", flags=re.I)
            if match:
                source_id = match.group(1)
                break
        if not source_id:
            source_id = hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:20]
        if not title or not link:
            continue
        records.append(
            Record(
                institution_code=institution["code"],
                institution=institution["institution"],
                platform="Inteum Technology Publisher public RSS",
                source_record_id=source_id,
                title=title,
                summary=description,
                published=published,
                updated=updated,
                inventors="",
                categories=categories,
                keywords="",
                official_record_url=link,
                source_feed_url=feed_url,
                retrieved_at_utc=retrieved_at,
                dedup_key=f"{institution['code'].lower()}|{link.lower()}",
            )
        )
    return records


def infer_tp_owner(base_url: str, institution: dict) -> bool:
    host = urlparse(base_url).netloc.lower()
    code = institution["code"]
    if code == "UTAUS":
        return "utotc." in host
    if code == "UTD":
        return "utdallas." in host or "texas." in host
    if code == "UTH":
        return "uthealth." in host or "texas." in host
    return False


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    output = Path("ut-system-output")
    raw = output / "raw"
    output.mkdir(exist_ok=True)
    raw.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
    })
    retrieved_at = datetime.now(timezone.utc).isoformat()

    all_records: dict[str, Record] = {}
    source_registry: list[dict] = []
    receipts: list[dict] = []
    errors: list[dict] = []

    for institution in INSTITUTIONS:
        discovered = discover_external_links(session, institution["office_url"])
        candidates = list(institution["candidate_portals"])
        candidates.extend(
            link for link in discovered
            if any(token in urlparse(link).netloc.lower() for token in ["technologypublisher.com", "flintbox.com"])
        )
        # Normalize to portal roots.
        normalized: list[str] = []
        for candidate in candidates:
            parsed = urlparse(candidate)
            normalized.append(f"{parsed.scheme or 'https'}://{parsed.netloc}/")
        normalized = sorted(set(normalized))

        if not normalized:
            source_registry.append({
                "institution_code": institution["code"],
                "institution": institution["institution"],
                "office_url": institution["office_url"],
                "portal_url": "",
                "platform": "Custom / no public catalog discovered",
                "records_harvested": 0,
                "source_status": "REQUIRES SOURCE-SPECIFIC CONNECTOR",
                "completion_status": "INCOMPLETE",
                "notes": "Official commercialization office identified; no machine-readable public catalog confirmed in this pass.",
            })
            continue

        for portal in normalized:
            host = urlparse(portal).netloc.lower()
            if "flintbox.com" in host:
                source_registry.append({
                    "institution_code": institution["code"],
                    "institution": institution["institution"],
                    "office_url": institution["office_url"],
                    "portal_url": portal,
                    "platform": "Wellspring Flintbox",
                    "records_harvested": 0,
                    "source_status": "REGISTERED — BULK CRAWL NOT EXECUTED",
                    "completion_status": "INCOMPLETE",
                    "notes": "Public portal confirmed. Automated bulk crawl withheld because Wellspring terms restrict spiders; use authorized export or source-specific permitted API.",
                })
                continue

            if "technologypublisher.com" not in host or not infer_tp_owner(portal, institution):
                continue

            feed_found = False
            best_records: list[Record] = []
            best_feed = ""
            for feed_url in technology_publisher_feed_candidates(portal):
                try:
                    response = fetch(session, feed_url, attempts=2, timeout=60)
                    content_type = response.headers.get("content-type", "")
                    if b"<rss" not in response.content[:1000].lower() and b"<feed" not in response.content[:1000].lower():
                        continue
                    parsed_records = parse_rss(response.content, institution, response.url, retrieved_at)
                    receipt_path = raw / f"{institution['code'].lower()}_{host.replace('.', '_')}_rss.xml"
                    receipt_path.write_bytes(response.content)
                    receipts.append({
                        "institution_code": institution["code"],
                        "institution": institution["institution"],
                        "source_url": response.url,
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "raw_bytes": len(response.content),
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                        "parsed_records": len(parsed_records),
                        "retrieved_at_utc": retrieved_at,
                    })
                    if len(parsed_records) > len(best_records):
                        best_records = parsed_records
                        best_feed = response.url
                    feed_found = True
                except Exception as exc:
                    errors.append({
                        "institution_code": institution["code"],
                        "institution": institution["institution"],
                        "url": feed_url,
                        "error": repr(exc),
                    })

            unique_feed_records: dict[str, Record] = {}
            for record in best_records:
                unique_feed_records[record.dedup_key] = record
                all_records[record.dedup_key] = record

            source_registry.append({
                "institution_code": institution["code"],
                "institution": institution["institution"],
                "office_url": institution["office_url"],
                "portal_url": portal,
                "platform": "Inteum Technology Publisher",
                "records_harvested": len(unique_feed_records),
                "source_status": "PUBLIC RSS HARVESTED" if feed_found else "RSS NOT FOUND",
                "completion_status": "SOURCE-COMPLETE PUBLIC RSS" if feed_found and unique_feed_records else "INCOMPLETE",
                "notes": f"Feed used: {best_feed}" if best_feed else "No valid public RSS response found.",
            })

    record_rows = [asdict(record) for record in sorted(all_records.values(), key=lambda r: (r.institution_code, r.title, r.official_record_url))]
    registry_rows = sorted(source_registry, key=lambda row: (row["institution_code"], row["portal_url"]))

    # Joint-URL duplicates are prevented at source. Cross-institution title matches are reported, not removed.
    title_groups: dict[str, list[dict]] = {}
    for row in record_rows:
        key = re.sub(r"[^a-z0-9]+", " ", row["title"].lower()).strip()
        title_groups.setdefault(key, []).append(row)
    cross_source_title_matches = [
        {
            "normalized_title": title,
            "record_count": len(group),
            "institutions": " / ".join(sorted({row["institution"] for row in group})),
            "record_urls": " | ".join(row["official_record_url"] for row in group),
        }
        for title, group in title_groups.items()
        if len({row["institution_code"] for row in group}) > 1
    ]

    record_fields = list(asdict(Record("", "", "", "", "", "", "", "", "", "", "", "", "", "", "")).keys())
    registry_fields = [
        "institution_code", "institution", "office_url", "portal_url", "platform",
        "records_harvested", "source_status", "completion_status", "notes",
    ]
    receipt_fields = [
        "institution_code", "institution", "source_url", "http_status", "content_type",
        "raw_bytes", "sha256", "parsed_records", "retrieved_at_utc",
    ]
    write_csv(output / "ut_system_records.csv", record_rows, record_fields)
    write_csv(output / "ut_system_source_registry.csv", registry_rows, registry_fields)
    write_csv(output / "ut_system_receipts.csv", receipts, receipt_fields)
    write_csv(output / "ut_system_errors.csv", errors, ["institution_code", "institution", "url", "error"])
    write_csv(output / "ut_system_cross_source_title_matches.csv", cross_source_title_matches, ["normalized_title", "record_count", "institutions", "record_urls"])
    (output / "ut_system_records.json").write_text(json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    completed_sources = [row for row in registry_rows if row["completion_status"] == "SOURCE-COMPLETE PUBLIC RSS"]
    institution_counts: dict[str, int] = {}
    for row in record_rows:
        institution_counts[row["institution"]] = institution_counts.get(row["institution"], 0) + 1

    manifest = {
        "program": "University of Texas System public technology harvest",
        "retrieved_at_utc": retrieved_at,
        "official_current_institution_count": len(INSTITUTIONS),
        "institutions_registered": len({row["institution_code"] for row in registry_rows}),
        "source_complete_public_rss_sources": len(completed_sources),
        "records_harvested": len(record_rows),
        "records_by_institution": institution_counts,
        "registered_flintbox_sources_not_bulk_crawled": sum(1 for row in registry_rows if row["platform"] == "Wellspring Flintbox"),
        "custom_sources_requiring_connectors": sum(1 for row in registry_rows if row["platform"].startswith("Custom")),
        "cross_source_title_match_groups": len(cross_source_title_matches),
        "system_source_complete": all(row["completion_status"] == "SOURCE-COMPLETE PUBLIC RSS" for row in registry_rows),
        "admission_rule": "Only institution sources explicitly marked source-complete may enter the finished aggregate.",
    }
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    (output / "ut_system_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    # This run is expected to produce a governed checkpoint, not falsely certify the full system.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
