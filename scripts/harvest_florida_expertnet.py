#!/usr/bin/env python3
"""Source-complete harvest of Florida ExpertNet's active technology corpus."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://expertnet.org/"
HOME = BASE
SEARCH_PAGE = urljoin(BASE, "index.cfm?prefilter=false&fuseaction=search.multiSearch&view=technologies")
AJAX = urljoin(BASE, "scripts/ajaxSearchData.cfc?method=loadTabData")
DETAIL_TEMPLATE = urljoin(BASE, "index.cfm?fuseaction=lo.details&propertyID={property_id}")
OUT = Path("florida-expertnet-harvest")
OUT.mkdir(exist_ok=True)
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
USER_AGENT = "Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Harvester/1.0)"

INSTITUTIONS = {
    1: ("Florida A&M University", "FAMU", "org-florida-am-university"),
    2: ("Florida Atlantic University", "FAU", "org-florida-atlantic-university"),
    3: ("Florida Gulf Coast University", "FGCU", "org-florida-gulf-coast-university"),
    4: ("Florida International University", "FIU", "org-florida-international-university"),
    5: ("Florida State University", "FSU", "org-florida-state-university"),
    6: ("University of Central Florida", "UCF", "org-university-of-central-florida"),
    7: ("University of Florida", "UF", "org-university-of-florida"),
    8: ("University of North Florida", "UNF", "org-university-of-north-florida"),
    9: ("University of South Florida", "USF", "org-university-of-south-florida"),
    10: ("University of West Florida", "UWF", "org-university-of-west-florida"),
    11: ("University of Miami", "UM", "org-university-of-miami"),
    13: ("Florida Institute of Technology", "FIT", "org-florida-institute-of-technology"),
}

_thread_local = threading.local()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def session() -> requests.Session:
    current = getattr(_thread_local, "session", None)
    if current is None:
        current = requests.Session()
        current.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )
        _thread_local.session = current
    return current


def fetch(url: str, attempts: int = 7, timeout: int = 60) -> requests.Response:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            response = session().get(url, timeout=timeout, allow_redirects=True)
            if response.status_code == 200 and len(response.content) > 500:
                return response
            errors.append(f"status={response.status_code} bytes={len(response.content)}")
        except requests.RequestException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        time.sleep(min(12, 0.75 * (2 ** (attempt - 1))))
    raise RuntimeError(f"Failed to fetch {url}: {'; '.join(errors[-7:])}")


def load_authoritative_dataset() -> tuple[list[dict], dict, int | None, str]:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    home = s.get(HOME, timeout=90)
    home.raise_for_status()
    home_text = clean(BeautifulSoup(home.text, "html.parser").get_text(" ", strip=True))
    count_matches = [int(v.replace(",", "")) for v in re.findall(r"Technologies\s+([\d,]+)", home_text, re.I)]
    homepage_count = Counter(count_matches).most_common(1)[0][0] if count_matches else None

    landing = s.get(SEARCH_PAGE, timeout=90)
    landing.raise_for_status()
    payload = {
        "prefilter": "false",
        "fuseaction": "search.multiSearch",
        "view": "technologies",
    }
    response = s.post(
        AJAX,
        data=payload,
        timeout=300,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://expertnet.org",
            "Referer": SEARCH_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    data = json.loads(response.text)
    query = data.get("queryData")
    if not isinstance(query, list):
        raise RuntimeError("ExpertNet queryData was not a list")
    (OUT / "expertnet_authoritative_response.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return query, data, homepage_count, hashlib.sha256(response.content).hexdigest()


def parse_json_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not clean(value):
        return []
    try:
        parsed = json.loads(str(value))
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def split_semicolon(value: Any) -> list[str]:
    values = []
    for item in re.split(r";+", str(value or "")):
        item = clean(item)
        if item and item not in values:
            values.append(item)
    return values


def html_sections(fragment: str) -> dict[str, str]:
    if not clean(fragment):
        return {}
    soup = BeautifulSoup(fragment, "html.parser")
    sections: dict[str, list[str]] = defaultdict(list)
    current = "description"
    for node in soup.descendants:
        if not isinstance(node, Tag):
            continue
        if node.name in {"h1", "h2", "h3", "h4", "h5", "strong"}:
            label = clean(node.get_text(" ", strip=True)).rstrip(":").lower()
            if label in {
                "abstract", "description", "benefit", "benefits", "advantages",
                "market application", "market applications", "application", "applications",
                "technical details", "technology", "partnering opportunity",
            }:
                current = label
                continue
        if node.name in {"p", "li"}:
            value = clean(node.get_text(" ", strip=True))
            if value and value not in sections[current]:
                sections[current].append(value)
    return {key: clean(" ".join(values)) for key, values in sections.items() if values}


def collect_heading_sections(root: Tag) -> dict[str, str]:
    result: dict[str, str] = {}
    headings = root.find_all(["h1", "h2", "h3", "h4", "h5"])
    for heading in headings:
        label = clean(heading.get_text(" ", strip=True)).rstrip(":")
        if not label or len(label) > 80:
            continue
        values: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4", "h5"}:
                break
            if isinstance(sibling, Tag):
                text = clean(sibling.get_text(" ", strip=True))
                if text:
                    values.append(text)
        if values:
            result[label] = clean(" ".join(values))
    return result


def links_near_heading(root: Tag, labels: set[str]) -> list[dict]:
    matches: list[dict] = []
    normalized = {label.lower() for label in labels}
    for heading in root.find_all(["h1", "h2", "h3", "h4", "h5"]):
        label = clean(heading.get_text(" ", strip=True)).rstrip(":").lower()
        if label not in normalized:
            continue
        for element in heading.next_elements:
            if isinstance(element, Tag) and element is not heading and element.name in {"h1", "h2", "h3", "h4", "h5"}:
                break
            if isinstance(element, Tag) and element.name == "a" and element.get("href"):
                href = urljoin(BASE, element.get("href", ""))
                text = clean(element.get_text(" ", strip=True))
                item = {"label": text, "url": href}
                if item not in matches:
                    matches.append(item)
    return matches


def parse_detail(record: dict) -> dict:
    url = record["canonical_detail_url"]
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    root = soup.find("main") or soup.find("div", class_=re.compile(r"content", re.I)) or soup.body or soup
    detail_text = clean(root.get_text(" ", strip=True))
    page_title_node = root.find(["h1", "h2"]) or soup.find(["h1", "h2"])
    page_title = clean(page_title_node.get_text(" ", strip=True)) if page_title_node else ""
    sections = collect_heading_sections(root)
    brochure_links = links_near_heading(root, {"Brochure", "Technology Brochure"})
    website_links = links_near_heading(root, {"Websites", "Website", "Related Websites"})
    researcher_links = links_near_heading(root, {"Researchers", "Researcher", "Inventors", "Inventor"})
    contact_links = links_near_heading(root, {"Contact Information", "Contact", "Licensing Contact"})

    all_links: list[dict] = []
    for anchor in root.find_all("a", href=True):
        item = {
            "label": clean(anchor.get_text(" ", strip=True)),
            "url": urljoin(response.url, anchor.get("href", "")),
        }
        if item not in all_links:
            all_links.append(item)
    external_links = [
        item for item in all_links
        if item["url"].startswith("http") and "expertnet.org" not in item["url"].lower()
    ]
    email_links = sorted({
        item["url"].removeprefix("mailto:").split("?", 1)[0]
        for item in all_links if item["url"].lower().startswith("mailto:")
    })
    phone_values = sorted(set(re.findall(
        r"\b(?:\+?1[.\-\s]?)?\(?\d{3}\)?[.\-\s]\d{3}[.\-\s]\d{4}\b", detail_text
    )))

    lower_text = detail_text.lower()
    semantic_status = "passed"
    if "an error occurred while executing the application" in lower_text or len(detail_text) < 120:
        semantic_status = "failed"

    record.update(
        {
            "detail_page_title": page_title,
            "detail_sections": sections,
            "detail_description": next((v for k, v in sections.items() if k.lower() == "description"), ""),
            "detail_application": next((v for k, v in sections.items() if k.lower() in {"application", "applications", "market application", "market applications"}), ""),
            "detail_advantages": next((v for k, v in sections.items() if k.lower() in {"advantages", "benefit", "benefits"}), ""),
            "detail_technology": next((v for k, v in sections.items() if k.lower() in {"technology", "technical details"}), ""),
            "brochure_links": brochure_links,
            "website_links": website_links,
            "researcher_links": researcher_links,
            "contact_links": contact_links,
            "external_links": external_links,
            "detail_emails": email_links,
            "detail_phones": phone_values,
            "detail_text": detail_text,
            "detail_sha256": hashlib.sha256(detail_text.encode("utf-8")).hexdigest(),
            "detail_http_status": response.status_code,
            "detail_final_url": response.url,
            "semantic_parse_status": semantic_status,
            "harvested_at_utc": NOW,
        }
    )
    return record


def normalize_record(raw: dict) -> dict:
    property_id = int(raw["pkPropertyID"])
    university_id = int(raw["universityID"])
    institution = INSTITUTIONS.get(university_id)
    if institution is None:
        institution = (f"Unknown ExpertNet institution {university_id}", clean(raw.get("shortName")), "")
    institution_name, institution_short_name, institution_id = institution

    contacts = parse_json_list(raw.get("contacts"))
    normalized_contacts = []
    for item in contacts:
        contact = {"name": clean(item.get("name")), "email": clean(item.get("email"))}
        if (contact["name"] or contact["email"]) and contact not in normalized_contacts:
            normalized_contacts.append(contact)

    inventors = parse_json_list(raw.get("inventors"))
    normalized_inventors = []
    for item in inventors:
        inventor = {
            "expert_id": item.get("expertID"),
            "fullname": clean(item.get("fullname")),
        }
        if (inventor["expert_id"] is not None or inventor["fullname"]) and inventor not in normalized_inventors:
            normalized_inventors.append(inventor)

    terms = parse_json_list(raw.get("irisData"))
    research_terms = []
    for item in terms:
        term = {"term_id": item.get("termID"), "term": clean(item.get("term"))}
        if term["term"] and term not in research_terms:
            research_terms.append(term)

    description_html = str(raw.get("description") or "")
    description_sections = html_sections(description_html)
    description_text = clean(BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True))
    title = clean(raw.get("title"))
    canonical_url = DETAIL_TEMPLATE.format(property_id=property_id)
    patent_numbers = split_semicolon(raw.get("patentNum"))
    patent_values = split_semicolon(raw.get("patents"))

    return {
        "institution_name": institution_name,
        "institution_short_name": institution_short_name,
        "institution_id": institution_id,
        "university_id": university_id,
        "source_catalog_name": "Florida ExpertNet Technology Licensing Opportunities",
        "source_catalog_url": SEARCH_PAGE,
        "source_record_id": str(property_id),
        "source_identifier_type": "expertnet_property_id",
        "property_id": property_id,
        "canonical_detail_url": canonical_url,
        "title": title,
        "normalized_title": normalize_title(title),
        "source_added_date": clean(raw.get("tsAddDate")),
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": description_sections,
        "abstract": description_sections.get("abstract", description_sections.get("description", "")),
        "benefits": description_sections.get("benefit", description_sections.get("benefits", "")),
        "market_applications": description_sections.get("market application", description_sections.get("market applications", "")),
        "technical_details": description_sections.get("technical details", description_sections.get("technology", "")),
        "contacts": normalized_contacts,
        "inventors": normalized_inventors,
        "research_terms": research_terms,
        "patent_numbers": patent_numbers,
        "patent_values": patent_values,
        "raw_short_name": clean(raw.get("shortName")),
        "provenance_tier": "official_public_source",
        "corpus_tier": "discovery_pool",
        "canon_status": "not_promoted",
    }


def flat_list(values: list[Any], key: str | None = None) -> str:
    output = []
    for item in values or []:
        value = item.get(key) if key and isinstance(item, dict) else item
        value = clean(value)
        if value and value not in output:
            output.append(value)
    return " | ".join(output)


def write_outputs(records: list[dict], validation: dict, manifest: dict, duplicate_groups: list[dict]) -> None:
    fields = [
        "institution_name", "institution_short_name", "institution_id", "university_id",
        "source_record_id", "source_identifier_type", "property_id", "canonical_detail_url",
        "title", "source_added_date", "description_text", "abstract", "benefits",
        "market_applications", "technical_details", "detail_description", "detail_application",
        "detail_advantages", "detail_technology", "contact_names", "contact_emails",
        "inventor_names", "inventor_expert_ids", "research_terms", "research_term_ids",
        "patent_numbers", "patent_values", "brochure_urls", "website_urls", "external_urls",
        "detail_emails", "detail_phones", "detail_page_title", "detail_text",
        "detail_sha256", "detail_http_status", "detail_final_url", "semantic_parse_status",
        "harvested_at_utc", "provenance_tier", "corpus_tier", "canon_status",
        "duplicate_candidate_group_key",
    ]
    csv_rows = []
    for record in records:
        csv_rows.append(
            {
                "institution_name": record["institution_name"],
                "institution_short_name": record["institution_short_name"],
                "institution_id": record["institution_id"],
                "university_id": record["university_id"],
                "source_record_id": record["source_record_id"],
                "source_identifier_type": record["source_identifier_type"],
                "property_id": record["property_id"],
                "canonical_detail_url": record["canonical_detail_url"],
                "title": record["title"],
                "source_added_date": record["source_added_date"],
                "description_text": record["description_text"],
                "abstract": record["abstract"],
                "benefits": record["benefits"],
                "market_applications": record["market_applications"],
                "technical_details": record["technical_details"],
                "detail_description": record.get("detail_description", ""),
                "detail_application": record.get("detail_application", ""),
                "detail_advantages": record.get("detail_advantages", ""),
                "detail_technology": record.get("detail_technology", ""),
                "contact_names": flat_list(record["contacts"], "name"),
                "contact_emails": flat_list(record["contacts"], "email"),
                "inventor_names": flat_list(record["inventors"], "fullname") or flat_list(record.get("researcher_links", []), "label"),
                "inventor_expert_ids": flat_list(record["inventors"], "expert_id"),
                "research_terms": flat_list(record["research_terms"], "term"),
                "research_term_ids": flat_list(record["research_terms"], "term_id"),
                "patent_numbers": flat_list(record["patent_numbers"]),
                "patent_values": flat_list(record["patent_values"]),
                "brochure_urls": flat_list(record.get("brochure_links", []), "url"),
                "website_urls": flat_list(record.get("website_links", []), "url"),
                "external_urls": flat_list(record.get("external_links", []), "url"),
                "detail_emails": flat_list(record.get("detail_emails", [])),
                "detail_phones": flat_list(record.get("detail_phones", [])),
                "detail_page_title": record.get("detail_page_title", ""),
                "detail_text": record.get("detail_text", ""),
                "detail_sha256": record.get("detail_sha256", ""),
                "detail_http_status": record.get("detail_http_status", ""),
                "detail_final_url": record.get("detail_final_url", ""),
                "semantic_parse_status": record.get("semantic_parse_status", ""),
                "harvested_at_utc": record.get("harvested_at_utc", NOW),
                "provenance_tier": record["provenance_tier"],
                "corpus_tier": record["corpus_tier"],
                "canon_status": record["canon_status"],
                "duplicate_candidate_group_key": record.get("duplicate_candidate_group_key", ""),
            }
        )

    with (OUT / "florida_expertnet_technologies.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)

    with (OUT / "florida_expertnet_technologies.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    with (OUT / "duplicate_candidates.csv").open("w", newline="", encoding="utf-8") as handle:
        fields_dup = ["group_key", "institution_name", "normalized_title", "record_count", "property_ids", "titles", "urls"]
        writer = csv.DictWriter(handle, fieldnames=fields_dup)
        writer.writeheader()
        writer.writerows(duplicate_groups)

    counts = Counter(record["institution_name"] for record in records)
    with (OUT / "institution_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["institution_name", "record_count"])
        writer.writerows(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    (OUT / "validation.json").write_text(json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    raw_rows, response_data, homepage_count, ajax_sha256 = load_authoritative_dataset()
    records = [normalize_record(raw) for raw in raw_rows]
    records.sort(key=lambda record: record["property_id"])
    total = len(records)
    print(f"Authoritative ExpertNet records: {total}; homepage count: {homepage_count}")

    enriched: list[dict] = []
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {pool.submit(parse_detail, record): record for record in records}
        for index, future in enumerate(as_completed(future_map), start=1):
            record = future_map[future]
            try:
                enriched.append(future.result())
            except Exception as exc:
                record.update(
                    {
                        "detail_http_status": "",
                        "detail_text": "",
                        "detail_sha256": "",
                        "semantic_parse_status": "failed",
                        "harvested_at_utc": NOW,
                    }
                )
                failures.append({"property_id": record["property_id"], "url": record["canonical_detail_url"], "error": repr(exc)})
                enriched.append(record)
            if index % 100 == 0 or index == total:
                print(f"Detail pages processed: {index}/{total}; failures={len(failures)}", flush=True)

    records = sorted(enriched, key=lambda record: record["property_id"])

    duplicate_map: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        duplicate_map[(record["institution_name"], record["normalized_title"])].append(record)
    duplicate_groups = []
    for (institution_name, normalized_title), group in duplicate_map.items():
        if normalized_title and len(group) > 1:
            key = hashlib.sha256(f"{institution_name}|{normalized_title}".encode("utf-8")).hexdigest()[:16]
            for record in group:
                record["duplicate_candidate_group_key"] = key
            duplicate_groups.append(
                {
                    "group_key": key,
                    "institution_name": institution_name,
                    "normalized_title": normalized_title,
                    "record_count": len(group),
                    "property_ids": " | ".join(str(item["property_id"]) for item in group),
                    "titles": " | ".join(dict.fromkeys(item["title"] for item in group)),
                    "urls": " | ".join(item["canonical_detail_url"] for item in group),
                }
            )
    duplicate_groups.sort(key=lambda group: (group["institution_name"], group["normalized_title"]))

    unique_ids = len({record["property_id"] for record in records})
    unique_urls = len({record["canonical_detail_url"] for record in records})
    institution_ids_valid = all(record["university_id"] in INSTITUTIONS for record in records)
    details_200 = sum(record.get("detail_http_status") == 200 for record in records)
    detail_text_present = sum(bool(clean(record.get("detail_text"))) for record in records)
    detail_hash_present = sum(bool(record.get("detail_sha256")) for record in records)
    semantic_pass = sum(record.get("semantic_parse_status") == "passed" for record in records)

    checks = {
        "authoritative_dataset_at_least_2000_records": total >= 2000,
        "homepage_count_matches_authoritative_dataset": homepage_count == total,
        "unique_property_ids_match_record_count": unique_ids == total,
        "unique_canonical_urls_match_record_count": unique_urls == total,
        "all_titles_present": all(bool(record["title"]) for record in records),
        "all_institutions_mapped": institution_ids_valid,
        "all_detail_pages_http_200": details_200 == total,
        "all_detail_text_present": detail_text_present == total,
        "all_detail_hashes_present": detail_hash_present == total,
        "all_detail_pages_semantically_valid": semantic_pass == total,
        "no_detail_fetch_failures": not failures,
        "institution_totals_sum_to_record_count": sum(Counter(record["institution_name"] for record in records).values()) == total,
    }

    optional_coverage = {
        "records_with_api_description": sum(bool(record["description_text"]) for record in records),
        "records_with_abstract": sum(bool(record["abstract"]) for record in records),
        "records_with_benefits": sum(bool(record["benefits"]) for record in records),
        "records_with_market_applications": sum(bool(record["market_applications"]) for record in records),
        "records_with_api_contact_name": sum(bool(flat_list(record["contacts"], "name")) for record in records),
        "records_with_api_contact_email": sum(bool(flat_list(record["contacts"], "email")) for record in records),
        "records_with_inventor_name_or_researcher_link": sum(bool(flat_list(record["inventors"], "fullname") or flat_list(record.get("researcher_links", []), "label")) for record in records),
        "records_with_research_terms": sum(bool(record["research_terms"]) for record in records),
        "records_with_patent_numbers": sum(bool(record["patent_numbers"]) for record in records),
        "records_with_brochure_link": sum(bool(record.get("brochure_links")) for record in records),
        "records_with_external_link": sum(bool(record.get("external_links")) for record in records),
        "duplicate_candidate_groups": len(duplicate_groups),
        "duplicate_candidate_records": sum(group["record_count"] for group in duplicate_groups),
    }

    validation = {
        "status": "passed" if all(checks.values()) else "failed",
        "source": "Florida ExpertNet",
        "source_catalog_url": SEARCH_PAGE,
        "validated_at_utc": NOW,
        "record_count": total,
        "homepage_count": homepage_count,
        "unique_property_ids": unique_ids,
        "unique_canonical_urls": unique_urls,
        "detail_pages_http_200": details_200,
        "detail_text_present": detail_text_present,
        "semantic_pass_records": semantic_pass,
        "detail_failures": failures,
        "checks": checks,
        "optional_field_coverage": optional_coverage,
        "institution_counts": dict(sorted(Counter(record["institution_name"] for record in records).items())),
    }
    manifest = {
        "source_name": "Florida ExpertNet",
        "source_catalog_url": SEARCH_PAGE,
        "source_homepage_url": HOME,
        "source_ajax_url": AJAX,
        "harvested_at_utc": NOW,
        "record_count": total,
        "homepage_count": homepage_count,
        "ajax_response_sha256": ajax_sha256,
        "ajax_top_level_keys": sorted(response_data.keys()),
        "corpus_tier": "discovery_pool",
        "canon_status": "not_promoted",
        "publication_authorized": False,
        "relationship_assertion_authorized": False,
        "deduplication_policy": "Preserve every official ExpertNet propertyID; flag probable underlying duplicates for review rather than collapsing records.",
        "files": [
            "expertnet_authoritative_response.json",
            "florida_expertnet_technologies.csv",
            "florida_expertnet_technologies.jsonl",
            "institution_summary.csv",
            "duplicate_candidates.csv",
            "validation.json",
            "manifest.json",
        ],
    }
    write_outputs(records, validation, manifest, duplicate_groups)
    print(json.dumps(validation, indent=2, ensure_ascii=False))
    return 0 if validation["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
