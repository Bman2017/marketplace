#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("harvard-otd-harvest")
CSV_PATH = OUT / "harvard_otd_technologies.csv"
JSONL_PATH = OUT / "harvard_otd_technologies.jsonl"
VALIDATION_PATH = OUT / "validation.json"
MANIFEST_PATH = OUT / "manifest.json"


def clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines() if re.sub(r"\s+", " ", line).strip()]


def parse_record(row: dict[str, str]) -> dict[str, str]:
    lines = clean_lines(row.get("detail_text", ""))
    try:
        browse_index = next(i for i, value in enumerate(lines) if value.casefold() == "browse technologies")
    except StopIteration:
        browse_index = -1

    title = lines[browse_index + 1] if 0 <= browse_index < len(lines) - 1 else row["source_record_id"].replace("-", " ").title()

    case_number = ""
    for index, value in enumerate(lines):
        if value.casefold() == "case number:" and index + 1 < len(lines):
            case_number = lines[index + 1]
            break

    investigators: list[str] = []
    for index, value in enumerate(lines):
        if value.casefold() == "investigators:":
            for candidate in lines[index + 1 :]:
                if candidate.casefold() in {"categories:", "back", "related inventions", "additional information"}:
                    break
                investigators.append(candidate)
            break

    categories: list[str] = []
    for index, value in enumerate(lines):
        if value.casefold() == "categories:":
            categories = lines[index + 1 :]
            break

    dbd_contact: list[str] = []
    for index, value in enumerate(lines):
        if value.casefold() == "to learn more about this opportunity, contact:":
            for candidate in lines[index + 1 :]:
                if candidate.casefold() in {"email", "more cases associated with this dbd", "investigators:"}:
                    break
                if candidate.casefold() != "back" and not re.fullmatch(r"\(\d{3}\)\s*\d{3}-\d{4}", candidate):
                    dbd_contact.append(candidate.rstrip(","))
            break

    intellectual_property_status = ""
    for value in lines:
        if value.casefold().startswith("intellectual property status:"):
            intellectual_property_status = value.split(":", 1)[1].strip()
            break

    patent_identifiers: list[str] = []
    for label in {"u.s. patent(s) issued:", "patent(s) issued:", "patent applications:"}:
        for index, value in enumerate(lines):
            if value.casefold() == label:
                for candidate in lines[index + 1 :]:
                    if candidate.casefold() in {
                        "case number:", "related inventions", "additional information", "back",
                        "investigators:", "categories:", "to learn more about this opportunity, contact:"
                    }:
                        break
                    if re.search(r"\d", candidate):
                        patent_identifiers.append(candidate)
    patent_identifiers = list(dict.fromkeys(patent_identifiers))

    description_lines = lines[browse_index + 2 :] if browse_index >= 0 else lines
    stop_index = len(description_lines)
    for index, value in enumerate(description_lines):
        folded = value.casefold()
        if (
            folded in {"case number:", "back", "to learn more about this opportunity, contact:", "investigators:", "categories:", "u.s. patent(s) issued:", "patent(s) issued:"}
            or folded.startswith("intellectual property status:")
        ):
            stop_index = index
            break
    technology_description = "\n".join(description_lines[:stop_index]).strip()
    listing_summary = re.sub(r"\s+", " ", technology_description)[:1200].strip()

    row.update(
        {
            "title": title,
            "listing_summary": listing_summary,
            "technology_description": technology_description,
            "dbd_contacts": " | ".join(dbd_contact),
            "investigators": " | ".join(investigators),
            "research_categories": " | ".join(categories),
            "intellectual_property_status": intellectual_property_status,
            "official_case_number": case_number,
            "patent_identifiers": " | ".join(patent_identifiers),
            "detail_sha256": hashlib.sha256(re.sub(r"\s+", " ", row["detail_text"]).strip().encode()).hexdigest(),
            "semantic_parse_status": "passed" if title and title != "Browse Technologies" and row.get("canonical_detail_url") and row.get("detail_text") else "failed",
        }
    )
    return row


def main() -> int:
    with CSV_PATH.open(encoding="utf-8", newline="") as handle:
        source_rows = list(csv.DictReader(handle))

    rows = [parse_record(dict(row)) for row in source_rows]
    fields = [
        "institution_name", "institution_id", "source_catalog_name", "source_catalog_url",
        "source_page_number", "source_position", "source_record_id", "source_identifier_type",
        "canonical_detail_url", "title", "listing_summary", "technology_description",
        "dbd_contacts", "investigators", "research_categories", "intellectual_property_status",
        "official_case_number", "patent_identifiers", "listing_page_url", "detail_text",
        "detail_sha256", "http_status", "harvested_at_utc", "provenance_tier", "corpus_tier",
        "canon_status", "semantic_parse_status"
    ]

    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)

    with JSONL_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({key: row.get(key, "") for key in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    checks = {
        "catalog_reported_total_is_265": len(rows) == 265,
        "unique_canonical_urls_is_265": len({row["canonical_detail_url"] for row in rows}) == 265,
        "unique_source_record_ids_is_265": len({row["source_record_id"] for row in rows}) == 265,
        "all_titles_semantically_parsed": all(row["title"] and row["title"] != "Browse Technologies" for row in rows),
        "all_detail_pages_http_200": all(str(row["http_status"]) == "200" for row in rows),
        "all_detail_text_present": all(row["detail_text"].strip() for row in rows),
        "all_detail_hashes_present": all(row["detail_sha256"].strip() for row in rows),
        "all_27_result_pages_represented": sorted({int(row["source_page_number"]) for row in rows}) == list(range(1, 28)),
        "all_core_semantic_records_passed": all(row["semantic_parse_status"] == "passed" for row in rows),
    }
    coverage = {
        "record_count": len(rows),
        "case_number_count": sum(bool(row["official_case_number"].strip()) for row in rows),
        "patent_identifier_record_count": sum(bool(row["patent_identifiers"].strip()) for row in rows),
        "dbd_contact_record_count": sum(bool(row["dbd_contacts"].strip()) for row in rows),
        "investigator_record_count": sum(bool(row["investigators"].strip()) for row in rows),
        "research_category_record_count": sum(bool(row["research_categories"].strip()) for row in rows),
        "core_semantic_pass_count": sum(row["semantic_parse_status"] == "passed" for row in rows),
    }
    validation = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "optional_field_coverage": coverage,
        "validated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "notes": "Optional fields are preserved when exposed by Harvard; they are not treated as mandatory because individual public records do not always publish every field.",
    }
    VALIDATION_PATH.write_text(json.dumps(validation, indent=2), encoding="utf-8")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["validation_status"] = validation["status"]
    manifest["optional_field_coverage"] = coverage
    manifest["semantic_parser"] = "detail-page labeled-field parser v2"
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(validation, indent=2), flush=True)
    return 0 if validation["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
