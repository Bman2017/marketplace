#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://expertnet.org/"
SEARCH = urljoin(BASE, "index.cfm?fuseaction=search.multiSearch")
OUT = Path("florida-expertnet-search-probe")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Probe/1.0)"})

cases = [
    ("post_empty", "post", {"prefilter":"true","criteria":"","view":"technologies","university":""}),
    ("post_space", "post", {"prefilter":"true","criteria":" ","view":"technologies","university":""}),
    ("post_star", "post", {"prefilter":"true","criteria":"*","view":"technologies","university":""}),
    ("get_empty", "get", {"prefilter":"true","criteria":"","view":"technologies","university":""}),
    ("get_view", "get", {"view":"technologies","prefilter":"true"}),
]

report=[]
for name, method, payload in cases:
    try:
        r = S.request(method, SEARCH, data=payload if method=="post" else None, params=payload if method=="get" else None, timeout=60, allow_redirects=True)
        soup=BeautifulSoup(r.text,"html.parser")
        links=[]
        ids=[]
        for a in soup.find_all("a",href=True):
            href=urljoin(r.url,a["href"])
            qs=parse_qs(urlparse(href).query)
            if "propertyID" in qs and qs["propertyID"][0].isdigit():
                ids.append(int(qs["propertyID"][0]))
            text=" ".join(a.get_text(" ",strip=True).split())
            if any(k in href.lower() for k in ["propertyid", "page", "offset", "start", "search.multisearch"]):
                links.append({"text":text,"href":href})
        forms=[]
        for f in soup.find_all("form"):
            forms.append({
                "action":urljoin(r.url,f.get("action","")),
                "method":(f.get("method") or "get").lower(),
                "inputs":[{"name":x.get("name"),"type":x.get("type"),"value":x.get("value"),"id":x.get("id")} for x in f.find_all(["input","select","button"])],
            })
        text=" ".join(soup.get_text(" ",strip=True).split())
        (OUT/f"{name}.html").write_text(r.text,encoding="utf-8")
        report.append({
            "name":name,"method":method,"payload":payload,"status":r.status_code,"final_url":r.url,
            "bytes":len(r.content),"title":soup.title.get_text(" ",strip=True) if soup.title else "",
            "property_ids":sorted(set(ids)),"links":links,"forms":forms,"text_prefix":text[:2000]
        })
    except Exception as exc:
        report.append({"name":name,"error":repr(exc)})

(OUT/"probe.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
print(json.dumps(report,indent=2)[:60000])
