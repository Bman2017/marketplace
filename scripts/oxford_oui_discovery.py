#!/usr/bin/env python3
"""Read-only discovery of Oxford University Innovation's public licensing catalog."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://innovation.ox.ac.uk"
CATALOG = f"{BASE}/licensing-opportunities/browse-innovations"
USER_AGENT = (
    "Arns-Innovations-Oxford-Public-Catalog-Discovery/1.0 "
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


def html_summary(url: str, text: str) -> dict:
    soup = BeautifulSoup(text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a.get("href", ""))
        links.append({
            "text": " ".join(a.get_text(" ", strip=True).split()),
            "href": href,
            "class": " ".join(a.get("class", [])),
            "rel": " ".join(a.get("rel", [])),
        })
    scripts = [urljoin(url, tag.get("src")) for tag in soup.find_all("script", src=True)]
    inline_scripts = [tag.get_text("\n") for tag in soup.find_all("script") if not tag.get("src")]
    forms = []
    for form in soup.find_all("form"):
        forms.append({
            "action": urljoin(url, form.get("action", "")),
            "method": (form.get("method") or "GET").upper(),
            "id": form.get("id", ""),
            "class": " ".join(form.get("class", [])),
            "inputs": [
                {
                    "name": node.get("name", ""),
                    "type": node.get("type", ""),
                    "value": node.get("value", ""),
                    "id": node.get("id", ""),
                    "class": " ".join(node.get("class", [])),
                }
                for node in form.find_all(["input", "select", "button", "option"])
            ],
        })
    data_attrs = []
    for tag in soup.find_all(True):
        attrs = {k: v for k, v in tag.attrs.items() if str(k).startswith("data-")}
        if attrs:
            data_attrs.append({"tag": tag.name, "class": " ".join(tag.get("class", [])), "attrs": attrs})
    internal_links = [item for item in links if urlparse(item["href"]).netloc == urlparse(BASE).netloc]
    innovation_links = sorted({
        item["href"].split("#", 1)[0].rstrip("/")
        for item in internal_links
        if "/licensing-opportunities/" in urlparse(item["href"]).path
        and "/browse-innovations" not in urlparse(item["href"]).path
    })
    ajax_candidates = []
    combined = "\n".join(inline_scripts)
    for pattern in [
        r"https?://[^\"'\s]+",
        r"/wp-json/[^\"'\s]+",
        r"/wp-admin/admin-ajax\.php[^\"'\s]*",
        r"ajaxurl\s*[:=]\s*[\"']([^\"']+)",
        r"rest_url\s*[:=]\s*[\"']([^\"']+)",
    ]:
        for match in re.findall(pattern, combined, flags=re.I):
            value = match if isinstance(match, str) else match[0]
            if any(token in value.lower() for token in ["ajax", "wp-json", "innovation", "license", "technology"]):
                ajax_candidates.append(value)
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "scripts": scripts,
        "forms": forms,
        "data_attributes": data_attrs[:1000],
        "innovation_links": innovation_links,
        "innovation_link_count": len(innovation_links),
        "internal_links": internal_links[:3000],
        "inline_ajax_candidates": sorted(set(ajax_candidates)),
        "inline_script_preview": combined[:30000],
    }


def main() -> int:
    out = Path("oxford-discovery")
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })

    targets = {
        "catalog": CATALOG,
        "catalog_page_2": f"{CATALOG}?page=2",
        "catalog_paged_2": f"{CATALOG}/page/2/",
        "robots": f"{BASE}/robots.txt",
        "sitemap_index": f"{BASE}/sitemap_index.xml",
        "wp_sitemap": f"{BASE}/wp-sitemap.xml",
        "wp_json": f"{BASE}/wp-json/",
        "wp_types": f"{BASE}/wp-json/wp/v2/types",
        "wp_search": f"{BASE}/wp-json/wp/v2/search?per_page=100&page=1",
        "admin_ajax": f"{BASE}/wp-admin/admin-ajax.php",
    }

    report = {"base": BASE, "catalog": CATALOG, "targets": {}, "script_findings": []}
    scripts: set[str] = set()

    for name, url in targets.items():
        result = fetch(session, url)
        extension = "html"
        if "json" in result["content_type"] or result["text"].lstrip().startswith(("{", "[")):
            extension = "json"
        elif name in {"robots", "sitemap_index", "wp_sitemap"}:
            extension = "txt"
        path = raw / f"{name}.{extension}"
        path.write_bytes(result["body"])
        entry = {
            "requested_url": result["requested_url"],
            "final_url": result["final_url"],
            "status": result["status"],
            "content_type": result["content_type"],
            "bytes": len(result["body"]),
            "sha256": hashlib.sha256(result["body"]).hexdigest() if result["body"] else "",
            "headers": result["headers"],
            "error": result["error"],
            "saved_as": str(path),
        }
        if "html" in result["content_type"] or "<html" in result["text"][:1000].lower():
            entry["html_summary"] = html_summary(result["final_url"] or url, result["text"])
            scripts.update(entry["html_summary"]["scripts"])
        else:
            entry["text_preview"] = result["text"][:30000]
        report["targets"][name] = entry

    for index, script_url in enumerate(sorted(scripts)):
        if urlparse(script_url).netloc != urlparse(BASE).netloc:
            continue
        result = fetch(session, script_url)
        path = raw / f"script_{index:03d}.js"
        path.write_bytes(result["body"])
        text = result["text"]
        endpoint_candidates = sorted(set(re.findall(
            r"[\"']((?:https?://[^\"']+|/[^\"']+)(?:ajax|wp-json|api|innovation|technology|license|filter|load|search)[^\"']*)[\"']",
            text,
            flags=re.I,
        )))[:1000]
        action_candidates = sorted(set(re.findall(
            r"(?:action|ajax_action|endpoint|route|nonce)\s*[:=]\s*[\"']([^\"']+)[\"']",
            text,
            flags=re.I,
        )))[:500]
        keyword_hits = sorted(set(re.findall(
            r"\b(?:admin-ajax|wp-json|load_more|loadMore|infiniteScroll|isotope|mixitup|filter|pagination|pageNum|currentPage|maxPages|found_posts|post_type)\b",
            text,
            flags=re.I,
        )))
        report["script_findings"].append({
            "url": script_url,
            "status": result["status"],
            "bytes": len(result["body"]),
            "sha256": hashlib.sha256(result["body"]).hexdigest() if result["body"] else "",
            "saved_as": str(path),
            "endpoint_candidates": endpoint_candidates,
            "action_candidates": action_candidates,
            "keyword_hits": keyword_hits,
            "error": result["error"],
        })

    (out / "oxford_discovery_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
