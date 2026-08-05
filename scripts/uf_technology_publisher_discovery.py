#!/usr/bin/env python3
"""Read-only discovery of UF Innovate's public Technology Publisher catalog."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://ufinnovate.technologypublisher.com/"
USER_AGENT = (
    "Arns-Innovations-UF-Public-Catalog-Discovery/1.0 "
    "(public metadata; source receipts retained)"
)


def fetch(session: requests.Session, url: str, timeout: float = 90.0) -> dict:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        return {
            "requested_url": url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "headers": dict(response.headers),
            "body": response.content,
            "text": response.text,
            "error": "",
        }
    except Exception as exc:
        return {
            "requested_url": url,
            "final_url": "",
            "status": 0,
            "content_type": "",
            "headers": {},
            "body": b"",
            "text": "",
            "error": repr(exc),
        }


def summarize_html(url: str, text: str) -> dict:
    soup = BeautifulSoup(text, "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, anchor.get("href", ""))
        links.append({
            "text": " ".join(anchor.get_text(" ", strip=True).split()),
            "href": href,
            "class": " ".join(anchor.get("class", [])),
            "id": anchor.get("id", ""),
        })
    text_content = " ".join(soup.get_text(" ", strip=True).split())
    result_counts = [
        int(value.replace(",", ""))
        for value in re.findall(r"\b([0-9][0-9,]*)\s+Results?\b", text_content, flags=re.I)
    ]
    tech_links = sorted({
        item["href"].split("#", 1)[0].rstrip("/")
        for item in links
        if urlparse(item["href"]).netloc == urlparse(BASE).netloc
        and re.search(r"/(?:tech|technology)/", urlparse(item["href"]).path, flags=re.I)
    })
    pagination_links = [
        item
        for item in links
        if re.search(r"(?:page|pagenumber|start|offset)=\d+", item["href"], flags=re.I)
        or re.fullmatch(r"\d+", item["text"])
        or re.search(r"\b(next|previous|last|first)\b", item["text"], flags=re.I)
    ]
    forms = []
    for form in soup.find_all("form"):
        forms.append({
            "action": urljoin(url, form.get("action", "")),
            "method": (form.get("method") or "GET").upper(),
            "id": form.get("id", ""),
            "class": " ".join(form.get("class", [])),
            "inputs": [
                {
                    "tag": node.name,
                    "name": node.get("name", ""),
                    "type": node.get("type", ""),
                    "value": node.get("value", ""),
                    "id": node.get("id", ""),
                }
                for node in form.find_all(["input", "select", "button"])
            ],
        })
    scripts = [urljoin(url, tag.get("src")) for tag in soup.find_all("script", src=True)]
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "result_counts": result_counts,
        "technology_link_count": len(tech_links),
        "technology_links": tech_links,
        "pagination_links": pagination_links[:300],
        "forms": forms,
        "scripts": scripts,
        "all_internal_links": [
            item for item in links
            if urlparse(item["href"]).netloc == urlparse(BASE).netloc
        ][:2000],
    }


def summarize_xml(text: str) -> dict:
    item_count = len(re.findall(r"<item\b", text, flags=re.I))
    case_ids = re.findall(r"<(?:\w+:)?caseId>(.*?)</(?:\w+:)?caseId>", text, flags=re.I | re.S)
    links = re.findall(r"<link>(.*?)</link>", text, flags=re.I | re.S)
    return {
        "item_count": item_count,
        "case_id_count": len(case_ids),
        "unique_case_ids": len(set(value.strip() for value in case_ids)),
        "link_count": len(links),
        "unique_links": len(set(value.strip() for value in links)),
        "first_case_ids": [value.strip() for value in case_ids[:10]],
    }


def main() -> int:
    out = Path("uf-discovery")
    raw = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    targets = {
        "home": BASE,
        "searchresults": urljoin(BASE, "searchresults.aspx"),
        "rss_aspx": urljoin(BASE, "rss.aspx"),
        "rss": urljoin(BASE, "rss"),
        "sitemap": urljoin(BASE, "sitemap.xml"),
        "robots": urljoin(BASE, "robots.txt"),
        "advanced_search": urljoin(BASE, "advancedsearch/"),
        "search_page_2_guess_1": urljoin(BASE, "searchresults.aspx?page=2"),
        "search_page_2_guess_2": urljoin(BASE, "searchresults.aspx?p=2"),
        "search_page_2_guess_3": urljoin(BASE, "searchresults.aspx?pn=2"),
        "search_page_2_guess_4": urljoin(BASE, "searchresults.aspx?PageNumber=2"),
    }

    report = {"base": BASE, "targets": {}, "script_findings": []}
    scripts: set[str] = set()
    for name, url in targets.items():
        result = fetch(session, url)
        content_type = result["content_type"].lower()
        text = result["text"]
        extension = "html"
        if "xml" in content_type or text.lstrip().startswith("<?xml") or "<rss" in text[:1000].lower():
            extension = "xml"
        elif name == "robots":
            extension = "txt"
        elif "json" in content_type or text.lstrip().startswith(("{", "[")):
            extension = "json"
        file_path = raw / f"{name}.{extension}"
        file_path.write_bytes(result["body"])
        entry = {
            "requested_url": result["requested_url"],
            "final_url": result["final_url"],
            "status": result["status"],
            "content_type": result["content_type"],
            "bytes": len(result["body"]),
            "sha256": hashlib.sha256(result["body"]).hexdigest() if result["body"] else "",
            "error": result["error"],
            "saved_as": str(file_path),
        }
        if extension == "html":
            summary = summarize_html(result["final_url"] or url, text)
            entry["html_summary"] = summary
            scripts.update(summary["scripts"])
        elif extension == "xml":
            entry["xml_summary"] = summarize_xml(text)
        else:
            entry["text_preview"] = text[:5000]
        report["targets"][name] = entry

    for index, script_url in enumerate(sorted(scripts)):
        if urlparse(script_url).netloc != urlparse(BASE).netloc:
            continue
        result = fetch(session, script_url)
        script_path = raw / f"script_{index:03d}.js"
        script_path.write_bytes(result["body"])
        text = result["text"]
        endpoint_candidates = sorted(set(re.findall(
            r"[\"']((?:https?://[^\"']+|/[^\"']+)(?:search|tech|rss|page|ajax|api)[^\"']*)[\"']",
            text,
            flags=re.I,
        )))[:500]
        keywords = sorted(set(re.findall(
            r"\b(?:pageNumber|currentPage|totalPages|pageSize|startIndex|offset|searchresults|pager|pagination|__doPostBack)\b",
            text,
            flags=re.I,
        )))
        report["script_findings"].append({
            "url": script_url,
            "status": result["status"],
            "bytes": len(result["body"]),
            "sha256": hashlib.sha256(result["body"]).hexdigest() if result["body"] else "",
            "endpoint_candidates": endpoint_candidates,
            "keywords": keywords,
            "saved_as": str(script_path),
            "error": result["error"],
        })

    (out / "uf_discovery_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
