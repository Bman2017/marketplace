#!/usr/bin/env python3
from __future__ import annotations
import html
import json
import re
from pathlib import Path

import requests

OUT = Path('florida-expertnet-ajax-probe')
OUT.mkdir(exist_ok=True)
url = 'https://expertnet.org/scripts/ajaxSearchData.cfc?method=loadTabData'
payload = {
    'view': 'technologies',
    'prefilter': 'true',
}
r = requests.post(
    url,
    data=payload,
    timeout=180,
    headers={
        'User-Agent': 'Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Probe/1.1)',
        'Accept': '*/*',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://expertnet.org',
        'Referer': 'https://expertnet.org/index.cfm?fuseaction=search.multiSearch&view=technologies&prefilter=true',
        'X-Requested-With': 'XMLHttpRequest',
    },
)
r.raise_for_status()
raw = r.text
(OUT / 'response.txt').write_text(raw, encoding='utf-8')

candidates = [raw, raw.lstrip('\ufeff\r\n\t ')]
# ColdFusion debugging or guards can wrap otherwise valid JSON. Preserve the raw
# response and try only reversible extraction strategies.
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
