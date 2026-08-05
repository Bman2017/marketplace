#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path
import requests

OUT=Path('florida-expertnet-ajax-probe')
OUT.mkdir(exist_ok=True)
url='https://expertnet.org/scripts/ajaxSearchData.cfc?method=loadTabData'
payload={
    'view':'technologies',
    'criteria':'',
    'university':'',
    'iris1':'',
    'cluster1':'',
    'countyList':'',
    'association':'',
    'connectID':'',
    'prefilter':'true',
    'persistFiltering':'1',
}
r=requests.post(url,data=payload,timeout=180,headers={
    'User-Agent':'Mozilla/5.0 (compatible; ArnsInnovations-FloridaExpertNet-Probe/1.0)',
    'Referer':'https://expertnet.org/index.cfm?fuseaction=search.multiSearch&view=technologies&prefilter=true',
    'X-Requested-With':'XMLHttpRequest',
})
r.raise_for_status()
(OUT/'response.txt').write_text(r.text,encoding='utf-8')
data=json.loads(r.text)
query=data.get('queryData',[])
summary={
    'http_status':r.status_code,
    'response_bytes':len(r.content),
    'top_level_keys':sorted(data.keys()),
    'query_count':len(query),
    'record_keys':sorted(query[0].keys()) if query else [],
    'sample_records':query[:3],
    'page_filter_keys':sorted((data.get('pageFilters') or {}).keys()),
}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(summary,indent=2,ensure_ascii=False)[:60000])
