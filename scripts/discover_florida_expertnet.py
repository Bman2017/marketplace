#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://expertnet.org/"
OUT = Path("florida-expertnet-discovery")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Discovery/1.0)"})

urls = [
    BASE,
    urljoin(BASE, "index.cfm?fuseaction=about.changesWorld"),
    urljoin(BASE, "index.cfm?fuseaction=lo.search"),
    urljoin(BASE, "index.cfm?fuseaction=lo.searchResults"),
    urljoin(BASE, "index.cfm?fuseaction=lo.results"),
    urljoin(BASE, "index.cfm?fuseaction=lo.directory"),
    urljoin(BASE, "index.cfm?fuseaction=lo.browse"),
]

report = []
for url in urls:
    try:
        r = S.get(url, timeout=45, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        forms = []
        for form in soup.find_all("form"):
            forms.append({
                "action": urljoin(r.url, form.get("action", "")),
                "method": (form.get("method") or "get").lower(),
                "id": form.get("id"),
                "name": form.get("name"),
                "fields": [
                    {
                        "tag": node.name,
                        "name": node.get("name"),
                        "type": node.get("type"),
                        "value": node.get("value"),
                        "id": node.get("id"),
                    }
                    for node in form.find_all(["input", "select", "button", "textarea"])
                ],
            })
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            text = " ".join(a.get_text(" ", strip=True).split())
            if "fuseaction=lo" in href.lower() or "technology" in text.lower() or "licensing" in text.lower():
                links.append({"text": text, "href": href})
        property_ids = sorted({
            int(parse_qs(urlparse(urljoin(r.url, a["href"])).query)["propertyID"][0])
            for a in soup.find_all("a", href=True)
            if "propertyID" in parse_qs(urlparse(urljoin(r.url, a["href"])).query)
            and parse_qs(urlparse(urljoin(r.url, a["href"])).query)["propertyID"][0].isdigit()
        })
        filename = f"page_{len(report)+1}.html"
        (OUT / filename).write_text(r.text, encoding="utf-8")
        report.append({
            "requested_url": url,
            "final_url": r.url,
            "status": r.status_code,
            "bytes": len(r.content),
            "title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "forms": forms,
            "candidate_links": links,
            "property_ids": property_ids,
            "html_file": filename,
        })
    except Exception as exc:
        report.append({"requested_url": url, "error": repr(exc)})

(OUT / "discovery.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2)[:50000])
