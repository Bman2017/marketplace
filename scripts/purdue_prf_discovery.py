#!/usr/bin/env python3
"""Read-only discovery of Purdue Research Foundation's public e-lucid licensing catalog."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://licensing.prf.org/"
USER_AGENT = (
    "Arns-Innovations-Purdue-Public-Catalog-Discovery/1.0 "
    "(public metadata; source receipts retained)"
)


def fetch(session: requests.Session, url: str, timeout: float = 60.0) -> dict:
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
        absolute = urljoin(url, anchor["href"])
        links.append({
            "text": " ".join(anchor.get_text(" ", strip=True).split()),
            "href": absolute,
            "class": " ".join(anchor.get("class", [])),
            "rel": " ".join(anchor.get("rel", [])),
        })
    scripts = [urljoin(url, tag.get("src")) for tag in soup.find_all("script", src=True)]
    forms = []
    for form in soup.find_all("form"):
        forms.append({
            "action": urljoin(url, form.get("action", "")),
            "method": (form.get("method") or "GET").upper(),
            "id": form.get("id", ""),
            "class": " ".join(form.get("class", [])),
            "inputs": [
                {
                    "name": input_tag.get("name", ""),
                    "type": input_tag.get("type", ""),
                    "value": input_tag.get("value", ""),
                }
                for input_tag in form.find_all(["input", "select", "button"])
            ],
        })
    product_links = sorted({
        item["href"].split("#", 1)[0].rstrip("/")
        for item in links
        if urlparse(item["href"]).netloc == urlparse(BASE).netloc
        and "/product/" in urlparse(item["href"]).path
        and not urlparse(item["href"]).path.endswith("/print")
    })
    pagination_links = [
        item for item in links
        if re.search(r"(?:page|skip|offset|start)=\d+", item["href"], re.I)
        or re.search(r"\b(next|previous|older|newer|more)\b", item["text"], re.I)
        or "pagination" in item["class"].lower()
    ]
    text_content = " ".join(soup.get_text(" ", strip=True).split())
    count_candidates = sorted({
        int(value.replace(",", ""))
        for value in re.findall(
            r"(?:showing|found|results?|products?|technologies|items?)\D{0,30}([0-9][0-9,]{2,})",
            text_content,
            flags=re.I,
        )
    })
    data_attributes = []
    for tag in soup.find_all(True):
        attrs = {
            key: value
            for key, value in tag.attrs.items()
            if str(key).startswith("data-")
        }
        if attrs:
            data_attributes.append({"tag": tag.name, "attrs": attrs})
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "product_links": product_links,
        "product_link_count": len(product_links),
        "pagination_links": pagination_links[:100],
        "scripts": scripts,
        "forms": forms,
        "count_candidates": count_candidates,
        "data_attributes": data_attributes[:300],
        "all_internal_links": [
            item for item in links
            if urlparse(item["href"]).netloc == urlparse(BASE).netloc
        ][:1000],
    }


def main() -> int:
    out = Path("purdue-discovery")
    raw = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    targets = {
        "home": BASE,
        "products": urljoin(BASE, "products"),
        "products_page_1": urljoin(BASE, "products?page=1"),
        "products_page_2": urljoin(BASE, "products?page=2"),
        "robots": urljoin(BASE, "robots.txt"),
        "sitemap": urljoin(BASE, "sitemap.xml"),
        "sitemap_index": urljoin(BASE, "sitemap_index.xml"),
        "api_products": urljoin(BASE, "api/products"),
        "products_json": urljoin(BASE, "products?format=json"),
    }

    report = {"base": BASE, "targets": {}, "script_findings": []}
    script_urls: set[str] = set()

    for name, url in targets.items():
        result = fetch(session, url)
        extension = "html"
        if "json" in result["content_type"] or result["text"].lstrip().startswith(("{", "[")):
            extension = "json"
        elif name.startswith("sitemap") or name == "robots":
            extension = "txt"
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
        if "html" in result["content_type"] or "<html" in result["text"][:500].lower():
            entry["html_summary"] = summarize_html(result["final_url"] or url, result["text"])
            script_urls.update(entry["html_summary"]["scripts"])
        else:
            entry["text_preview"] = result["text"][:3000]
        report["targets"][name] = entry

    # Fetch same-origin JavaScript and search for endpoint / pagination clues.
    for index, script_url in enumerate(sorted(script_urls)):
        if urlparse(script_url).netloc != urlparse(BASE).netloc:
            continue
        result = fetch(session, script_url)
        script_path = raw / f"script_{index:03d}.js"
        script_path.write_bytes(result["body"])
        text = result["text"]
        endpoint_candidates = sorted(set(re.findall(
            r"[\"']((?:https?://[^\"']+|/[^\"']+)(?:api|product|search|catalog|page|filter)[^\"']*)[\"']",
            text,
            flags=re.I,
        )))[:300]
        keywords = sorted(set(re.findall(
            r"\b(?:pageSize|pageNumber|currentPage|totalPages|totalCount|skip|take|offset|limit|productSearch|searchProducts)\b",
            text,
            flags=re.I,
        )))
        report["script_findings"].append({
            "url": script_url,
            "status": result["status"],
            "bytes": len(result["body"]),
            "sha256": hashlib.sha256(result["body"]).hexdigest() if result["body"] else "",
            "saved_as": str(script_path),
            "endpoint_candidates": endpoint_candidates,
            "pagination_keywords": keywords,
            "error": result["error"],
        })

    (out / "purdue_discovery_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
