#!/usr/bin/env python3
from __future__ import annotations
import html
import json
import re
from pathlib import Path

import requests

OUT = Path('florida-expertnet-ajax-probe')
OUT.mkdir(exist_ok=True)
page_url = 'https://expertnet.org/index.cfm?prefilter=false&fuseaction=search.multiSearch&view=technologies'
ajax_url = 'https://expertnet.org/scripts/ajaxSearchData.cfc?method=loadTabData'

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Probe/1.3)',
    'Accept-Language': 'en-US,en;q=0.9',
})
landing = session.get(page_url, timeout=90)
landing.raise_for_status()
(OUT / 'landing.html').write_text(landing.text, encoding='utf-8')

# search.js calls parseFilterParams(new URLSearchParams(location.search)) and
# POSTs that object unchanged after setting view. Preserve every query key,
# including fuseaction, exactly as the public "All Technologies" link does.
payload = {
    'prefilter': 'false',
    'fuseaction': 'search.multiSearch',
    'view': 'technologies',
}
r = session.post(
    ajax_url,
    data=payload,
    timeout=240,
    headers={
        'Accept': '*/*',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://expertnet.org',
        'Referer': page_url,
        'X-Requested-With': 'XMLHttpRequest',
    },
)
r.raise_for_status()
raw = r.text
(OUT / 'response.txt').write_text(raw, encoding='utf-8')

candidates = [raw, raw.lstrip('\ufeff\r\n\t ')]
first_object = raw.find('{')
last_object = raw.rfind('}')
if first_object >= 0 and last_object > first_object:
    candidates.append(raw[first_object:last_object + 1])
for match in re.finditer(r'(?s)(\{.*\})', raw):
    candidates.append(match.group(1))

parsed = None
parse_errors = []
for candidate in candidates:
    for value in (candidate, html.unescape(candidate)):
        try:
            parsed = json.loads(value)
            break
        except Exception as exc:
            parse_errors.append(repr(exc))
    if parsed is not None:
        break

summary = {
    'landing_status': landing.status_code,
    'session_cookie_names': sorted(session.cookies.keys()),
    'payload': payload,
    'http_status': r.status_code,
    'response_bytes': len(r.content),
    'content_type': r.headers.get('content-type', ''),
    'raw_prefix_repr': repr(raw[:1000]),
    'raw_suffix_repr': repr(raw[-500:]),
    'decoded': parsed is not None,
    'parse_errors_tail': parse_errors[-5:],
}
if isinstance(parsed, dict):
    query = parsed.get('queryData', [])
    summary.update({
        'top_level_keys': sorted(parsed.keys()),
        'record_count_value': parsed.get('recordCount'),
        'query_count': len(query) if isinstance(query, list) else None,
        'query_type': type(query).__name__,
        'record_keys': sorted(query[0].keys()) if isinstance(query, list) and query else [],
        'sample_records': query[:3] if isinstance(query, list) else [],
        'page_filter_keys': sorted((parsed.get('pageFilters') or {}).keys()) if isinstance(parsed.get('pageFilters'), dict) else [],
    })
    (OUT / 'decoded.json').write_text(json.dumps(parsed, ensure_ascii=False), encoding='utf-8')

(OUT / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(summary, indent=2, ensure_ascii=False)[:100000])
if not isinstance(parsed, dict):
    raise SystemExit('ExpertNet response envelope could not be decoded; raw response retained for inspection')
